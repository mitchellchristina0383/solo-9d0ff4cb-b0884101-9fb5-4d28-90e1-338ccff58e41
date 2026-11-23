"""虚拟磁盘热换盘演练的核心引擎。

模型说明
--------
* 两份本地盘镜像(``disk_a.img`` / ``disk_b.img``)，每份由固定数量的定长块组成。
* 一个“槽位”同一时刻最多挂载一份盘，运维可在槽位上拔盘、换盘。
* 每次插拔都会令槽位的 ``mount_generation``(挂载代次) +1；一次写入在入队/校验时
  捕获当前代次，真正落盘前再次比对，代次不一致即判定为“失效写入”。
  因此换盘后再放行的旧写入一律拒绝，即使把原盘插回也不会改动任何镜像。
* 写入门(open/paused)控制写入是直接落盘还是先进入 pending 队列。暂停期间仍可
  换盘、读盘，并对“当前挂载的盘”发起新写入(同样进入队列)。
* 每个(盘, 块)有独立锁，不同块的写入可以真正并行；同一把块锁保证同块写入串行、
  落盘的整块写入不会撕裂。注意块锁按 **(盘ID, 块号)** 区分：旧盘的批次正在等
  同步时，换上新盘后对同号块的读写使用另一把锁，不会被旧盘同步阻塞。

合批同步(一次 fsync 覆盖多块)
-----------------------------
门放行后的直接写入(以及排队放行的写入)在通过失效/故障预检、拿到自己的块锁后，
进入一个按 **(盘ID, 挂载代次)** 归属的同步批次：

* 第一个到达的写入作为批次“值班员(leader)”建批，并留出一个很短的合流窗口
  (``BATCH_JOIN_WINDOW``，默认 25ms)；同盘、同挂载代次、写**不同块**的就绪
  写入在窗口内并入同一批次。同一块的写入因块锁互斥，只会进入后续批次。
* 合流窗口关闭后，leader 依次 ``seek/write`` 批次内各块，随后只调用 **一次**
  ``flush + fsync``；批次内其它写入在自己的块锁上等待同步结束。
* 批次成员(含 leader)都要等同步成功后才把状态置为 ``done`` 并返回；参与批次的
  块因块锁一直 held 到同步结束，期间的回读会自然阻塞到同步完成，不会读到
  “已 write 尚未 fsync”的中间视图。
* 即使只有一条写入也照常建批落盘(单成员批次 = 一次 fsync，行为与改造前等价)。
* 批次一旦开始就只针对它所属的那份镜像：等待同步期间换盘，批次仍在原盘完成；
  旧挂载代次的其它写入在预检处判定 ``stale``，不会混进新盘的批次。
* 故障开关在真正动盘(open/write)之前检查：开关打开时，尚未动盘的批次整批
  报 :class:`SimulatedFlushError`，所有成员的原块内容保持不变。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

BLOCK_SIZE = 64
BLOCK_COUNT = 32
META_FILENAME = "disks.json"


class DiskError(Exception):
    """可展示给前端的业务错误。"""


class StaleWriteError(DiskError):
    """写入落盘前槽位发生过换盘，写入失效。"""


class SimulatedFlushError(DiskError):
    """模拟落盘失败。"""


@dataclass
class DiskInfo:
    disk_id: str
    name: str
    filename: str

    def to_dict(self) -> dict:
        return {"disk_id": self.disk_id, "name": self.name, "filename": self.filename}


@dataclass
class PendingWrite:
    id: str
    disk_id: str            # 发起写入时挂载的盘
    disk_name: str
    block: int
    payload: bytes
    mount_generation: int   # 发起时捕获的槽位代次
    delay: float
    created_at: float
    note: str = ""
    status: str = "pending"  # pending / done / stale / failed
    error: str = ""
    sync_delay: float = 0.0   # 批次 fsync 前的额外睡眠（演练“同步中换盘”）
    batch_id: str = ""        # 实际归属的同步批次

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "disk_id": self.disk_id,
            "disk_name": self.disk_name,
            "block": self.block,
            "payload": self.payload.decode("latin-1"),
            "mount_generation": self.mount_generation,
            "delay": self.delay,
            "sync_delay": self.sync_delay,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "note": self.note,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class SyncBatch:
    """一次合批同步：同盘同代次、不同块的若干写入共用一次 fsync。"""

    batch_id: str
    disk_id: str
    disk_name: str
    mount_generation: int
    members: List[PendingWrite]
    joined_blocks: Set[int]
    event: threading.Event
    sync_delay: float = 0.0
    success: bool = False
    fsync_index: int = 0          # 本批次使用的是全局第几次 fsync（1 起）
    error: Optional[Exception] = None

    @property
    def size(self) -> int:
        return len(self.members)


def _seed_block_content(disk_name: str, block: int) -> bytes:
    text = f"[{disk_name} block {block:02d}] " + ("seed-" + str(block)) * 8
    return text.encode("utf-8")[:BLOCK_SIZE].ljust(BLOCK_SIZE, b".")


class DiskManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.meta_path = os.path.join(data_dir, META_FILENAME)

        self._lock = threading.RLock()           # 保护槽位/队列/元数据/计数
        # 块锁按 (盘ID, 块号) 区分：换盘后新盘同号块不被旧盘同步阻塞
        self._block_locks_guard = threading.Lock()
        self._block_locks: Dict[Tuple[str, int], threading.Lock] = {}

        # 合批协调：同一时刻每个 (盘, 代次) 最多一个开放批次
        self._batch_cond = threading.Condition()
        self._open_batch: Optional[SyncBatch] = None

        self.disks: Dict[str, DiskInfo] = {}
        self.mounted_id: Optional[str] = None
        self.mount_generation = 0
        self.gate_open = True                    # True=放行, False=暂停
        self.fail_simulation = False
        self.pending: Dict[str, PendingWrite] = {}
        self.event_log: List[dict] = []
        self._log_max = 200

        # 实际 fsync 次数统计与近期批次摘要（供页面/测试核对合批效果）
        self.fsync_total = 0
        self.batch_history: List[dict] = []
        self._batch_history_max = 50
        # 合流窗口：leader 建批后等待其它就绪写入并入的时间
        self.batch_join_window = float(os.environ.get("BATCH_JOIN_WINDOW", "0.025"))

        self._load_or_seed()

    # ------------------------------------------------------------------ 初始化
    def _load_or_seed(self) -> None:
        if os.path.exists(self.meta_path):
            with open(self.meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            for item in meta["disks"]:
                info = DiskInfo(**item)
                if not os.path.exists(self._path_for(info)):
                    raise DiskError(f"镜像文件缺失: {info.filename}")
                self.disks[info.disk_id] = info
        else:
            seeds = [
                ("disk-A", "disk_a.img"),
                ("disk-B", "disk_b.img"),
            ]
            for name, filename in seeds:
                disk_id = str(uuid.uuid4())
                self.disks[disk_id] = DiskInfo(disk_id, name, filename)
                self._seed_image(self.disks[disk_id])
            self._write_meta()
        # 默认挂载第一块盘(A)，记为第 1 代
        first = sorted(self.disks.values(), key=lambda d: d.filename)[0]
        self.mounted_id = first.disk_id
        self.mount_generation = 1
        self._log("init", f"初始化完成，默认挂载 {first.name}，挂载代次=1")

    def _seed_image(self, info: DiskInfo) -> None:
        path = self._path_for(info)
        with open(path, "wb") as fh:
            for block in range(BLOCK_COUNT):
                fh.write(_seed_block_content(info.name, block))

    def reseed(self) -> None:
        """重新生成两份镜像并恢复初始状态（演练重置）。"""
        with self._lock:
            for info in self.disks.values():
                self._seed_image(info)
            self.pending.clear()
            self.gate_open = True
            self.fail_simulation = False
            self.fsync_total = 0
            self.batch_history.clear()
            first = sorted(self.disks.values(), key=lambda d: d.filename)[0]
            self.mounted_id = first.disk_id
            self.mount_generation = 1
            self._log("reseed", "两份镜像已重新生成，槽位、写入门与同步计数已重置")

    def _write_meta(self) -> None:
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"disks": [d.to_dict() for d in self.disks.values()]},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, self.meta_path)

    def _path_for(self, info: DiskInfo) -> str:
        return os.path.join(self.data_dir, info.filename)

    # ------------------------------------------------------------------ 日志
    def _log(self, kind: str, message: str) -> None:
        self.event_log.append({"ts": time.time(), "kind": kind, "message": message})
        if len(self.event_log) > self._log_max:
            del self.event_log[: len(self.event_log) - self._log_max]

    def _record_batch(self, batch: SyncBatch, status: str, error: str) -> None:
        self.batch_history.append(
            {
                "batch_id": batch.batch_id,
                "disk_id": batch.disk_id,
                "disk_name": batch.disk_name,
                "mount_generation": batch.mount_generation,
                "blocks": [m.block for m in batch.members],
                "size": batch.size,
                "status": status,
                "error": error,
                "fsync_index": batch.fsync_index,
                "ts": time.time(),
            }
        )
        if len(self.batch_history) > self._batch_history_max:
            del self.batch_history[: len(self.batch_history) - self._batch_history_max]

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict:
        with self._lock:
            mounted = self.disks.get(self.mounted_id) if self.mounted_id else None
            return {
                "block_size": BLOCK_SIZE,
                "block_count": BLOCK_COUNT,
                "gate_open": self.gate_open,
                "fail_simulation": self.fail_simulation,
                "mount_generation": self.mount_generation,
                "mounted": mounted.to_dict() if mounted else None,
                "disks": [d.to_dict() for d in self.disks.values()],
                "pending": [w.to_dict() for w in self.pending.values()],
                "event_log": list(reversed(self.event_log)),
                "fsync_total": self.fsync_total,
                "batches": list(reversed(self.batch_history)),
            }

    # ------------------------------------------------------------------ 槽位
    def plug(self, disk_id: str) -> dict:
        with self._lock:
            if disk_id not in self.disks:
                raise DiskError("目标盘不存在")
            if self.mounted_id == disk_id:
                raise DiskError("该盘已经在槽位上")
            info = self.disks[disk_id]
            self.mounted_id = disk_id
            self.mount_generation += 1
            self._log(
                "plug",
                f"挂载 {info.name}，挂载代次推进到 {self.mount_generation}；"
                f"此前排队的旧写入若代次不符将在放行时失效；"
                f"已在原盘开跑的同步批次仍只在原盘完成",
            )
            return {"mount_generation": self.mount_generation, "mounted": info.to_dict()}

    def unplug(self) -> dict:
        with self._lock:
            if not self.mounted_id:
                raise DiskError("槽位当前没有挂载盘")
            info = self.disks[self.mounted_id]
            self.mounted_id = None
            self.mount_generation += 1
            self._log("unplug", f"拔除 {info.name}，挂载代次推进到 {self.mount_generation}")
            return {"mount_generation": self.mount_generation}

    def set_gate(self, open_gate: bool) -> dict:
        with self._lock:
            self.gate_open = open_gate
            state = "放行(open)" if open_gate else "暂停(paused)"
            self._log("gate", f"写入门切换为 {state}")
            return {"gate_open": self.gate_open}

    def set_fail_simulation(self, enabled: bool) -> dict:
        with self._lock:
            self.fail_simulation = enabled
            self._log("fail-sim", f"模拟落盘失败 {'开启' if enabled else '关闭'}")
            return {"fail_simulation": self.fail_simulation}

    # ------------------------------------------------------------------ 校验
    def _validate(self, block: int, payload: bytes) -> None:
        if not isinstance(block, int) or not (0 <= block < BLOCK_COUNT):
            raise DiskError(f"块号必须在 0..{BLOCK_COUNT - 1} 之间")
        if not isinstance(payload, (bytes, bytearray)):
            raise DiskError("payload 必须是字节串")
        if len(payload) != BLOCK_SIZE:
            raise DiskError(f"定长块写入要求恰好 {BLOCK_SIZE} 字节，收到 {len(payload)} 字节")

    def _require_mounted(self) -> DiskInfo:
        if not self.mounted_id or self.mounted_id not in self.disks:
            raise DiskError("槽位未挂载任何盘，无法读写")
        return self.disks[self.mounted_id]

    def _block_lock(self, disk_id: str, block: int) -> threading.Lock:
        """按 (盘ID, 块号) 取锁：不同盘的同号块互不阻塞。"""
        key = (disk_id, block)
        with self._block_locks_guard:
            lock = self._block_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._block_locks[key] = lock
            return lock

    # ------------------------------------------------------------------ 读
    def read_block(self, block: int) -> dict:
        with self._lock:
            info = self._require_mounted()
            self._validate(block, b"x" * BLOCK_SIZE)  # 只校验块号
        # 读也拿(盘,块)锁，保证不会读到正在写一半/尚未同步完的块；
        # 换盘后读新盘同号块用的是另一把锁，旧盘同步不会挡住它。
        with self._block_lock(info.disk_id, block):
            with open(self._path_for(info), "rb") as fh:
                fh.seek(block * BLOCK_SIZE)
                data = fh.read(BLOCK_SIZE)
        if len(data) != BLOCK_SIZE:
            raise DiskError("镜像读取长度异常，镜像可能已损坏")
        return {
            "disk_id": info.disk_id,
            "disk_name": info.name,
            "block": block,
            "data": data.decode("latin-1"),
        }

    # ------------------------------------------------------------------ 写
    def write_block(
        self,
        block: int,
        payload: bytes,
        delay: float = 0.0,
        note: str = "",
        sync_delay: float = 0.0,
    ) -> dict:
        """对当前挂载盘发起定长块写入。

        门放行时直接落盘；门暂停时入队等待。慢写(delay>0)在落盘前睡眠，
        以便演练“写入尚未落盘时暂停/换盘”。``sync_delay`` 让所属批次在
        真正 ``fsync`` 前再睡一会，便于演练“同步进行中换盘”。
        """
        delay = max(0.0, float(delay))
        sync_delay = max(0.0, float(sync_delay))
        with self._lock:
            info = self._require_mounted()
            self._validate(block, payload)
            generation = self.mount_generation
            pw = PendingWrite(
                id=uuid.uuid4().hex[:12],
                disk_id=info.disk_id,
                disk_name=info.name,
                block=block,
                payload=bytes(payload),
                mount_generation=generation,
                delay=delay,
                sync_delay=sync_delay,
                created_at=time.time(),
                note=note or ("直接写入" if self.gate_open else "暂停期间排队"),
            )
            if not self.gate_open:
                self.pending[pw.id] = pw
                self._log(
                    "enqueue",
                    f"写入已排队: {info.name} 块{block} (代次{generation})，等待放行",
                )
                return {"queued": True, "write": pw.to_dict()}

        # 门放行：直接落盘（块锁 + 合批同步）
        self._flush(pw)
        return {"queued": False, "write": pw.to_dict()}

    def release_one(self, write_id: str) -> dict:
        with self._lock:
            pw = self.pending.get(write_id)
            if not pw:
                raise DiskError("排队写入不存在或已处理")
        return self._release(pw)

    def release_all(self) -> dict:
        results = []
        # 快照后逐个处理，处理过程中新入队的不会被误放行
        with self._lock:
            queued = list(self.pending.values())
        for pw in queued:
            if pw.status != "pending":
                continue
            try:
                self._release(pw)
            except DiskError as exc:
                results.append({"id": pw.id, "status": pw.status, "error": str(exc)})
            else:
                results.append({"id": pw.id, "status": pw.status, "error": pw.error})
        return {"results": results}

    def discard_pending(self, write_id: str) -> dict:
        with self._lock:
            pw = self.pending.pop(write_id, None)
            if not pw:
                raise DiskError("排队写入不存在或已处理")
            pw.status = "discarded"
            self._log("discard", f"已丢弃排队写入: {pw.disk_name} 块{pw.block}")
            return {"write": pw.to_dict()}

    def _release(self, pw: PendingWrite) -> dict:
        try:
            self._flush(pw)
        finally:
            # 无论成功、失效还是模拟失败，放行后都离开排队队列；
            # 结果通过 HTTP 响应与事件日志留存
            with self._lock:
                self.pending.pop(pw.id, None)
        return {"write": pw.to_dict()}

    # ------------------------------------------------------ 失效/故障预检
    def _precheck(self, pw: PendingWrite) -> DiskInfo:
        """动盘之前的判定：失效(stale)优先，其次模拟落盘失败。

        在拿块锁之前调用，避免慢写/换盘场景下无谓地占用块锁。
        """
        with self._lock:
            current_info = self.disks.get(self.mounted_id) if self.mounted_id else None
            current_generation = self.mount_generation
            fail = self.fail_simulation

        # 1) 失效判定：槽位代次变化（拔盘/换盘/插回原盘都会 +1）即拒绝
        if current_generation != pw.mount_generation or (
            current_info and current_info.disk_id != pw.disk_id
        ):
            pw.status = "stale"
            pw.error = (
                f"写入失效：发起时代次{pw.mount_generation}，当前代次{current_generation}，"
                "槽位期间发生过换盘/拔盘，拒绝落盘（镜像保持不变）"
            )
            with self._lock:
                self._log("stale", f"写入失效: {pw.disk_name} 块{pw.block}，未改动任何镜像")
            raise StaleWriteError(pw.error)

        if not current_info:
            pw.status = "stale"
            pw.error = "写入失效：槽位当前为空"
            raise StaleWriteError(pw.error)

        # 2) 模拟落盘失败：在真正写文件之前抛错，原块内容完整保留
        if fail:
            pw.status = "failed"
            pw.error = "模拟落盘失败：写入门已拒绝本次写入，镜像未被修改"
            with self._lock:
                self._log(
                    "flush-fail",
                    f"模拟落盘失败: {pw.disk_name} 块{pw.block}，原块内容保留",
                )
            raise SimulatedFlushError(pw.error)

        return current_info

    # ------------------------------------------------------ 合批协调
    def _join_batch(
        self, pw: PendingWrite, info: DiskInfo
    ) -> Tuple[SyncBatch, bool]:
        """把一条已通过预检、已持有自身块锁的写入并入同步批次。

        返回 ``(批次, 是否leader)``。同盘、同挂载代次、不同块的写入并入开放
        批次；其它情况（无开放批次/代次不符/同块）由本写入新开批次并当 leader。
        """
        with self._batch_cond:
            batch = self._open_batch
            compatible = (
                batch is not None
                and batch.disk_id == pw.disk_id
                and batch.mount_generation == pw.mount_generation
                and pw.block not in batch.joined_blocks
            )
            if not compatible:
                batch = SyncBatch(
                    batch_id=uuid.uuid4().hex[:12],
                    disk_id=pw.disk_id,
                    disk_name=info.name,
                    mount_generation=pw.mount_generation,
                    members=[pw],
                    joined_blocks={pw.block},
                    event=threading.Event(),
                    sync_delay=pw.sync_delay,
                )
                self._open_batch = batch
                is_leader = True
            else:
                batch.members.append(pw)
                batch.joined_blocks.add(pw.block)
                # 批次内任一成员要求的同步延时都生效（取最大值）
                if pw.sync_delay > batch.sync_delay:
                    batch.sync_delay = pw.sync_delay
                is_leader = False

        if is_leader:
            # 合流窗口：等待同盘同代次、其它不同块的就绪写入并入
            try:
                with self._batch_cond:
                    self._batch_cond.wait(self.batch_join_window)
            except BaseException as exc:  # 等待被中断：收尾并唤醒已并入的成员
                with self._batch_cond:
                    if self._open_batch is batch:
                        self._open_batch = None
                if not batch.event.is_set():
                    batch.success = False
                    batch.error = exc
                    for member in batch.members:
                        member.status = "failed"
                        member.error = f"批次同步被中断：{exc}"
                    batch.event.set()
                raise
            else:
                with self._batch_cond:
                    if self._open_batch is batch:
                        self._open_batch = None
        return batch, is_leader

    def _run_batch(self, batch: SyncBatch) -> None:
        """leader 专用：写完整批块后只做一次 fsync，并唤醒全部成员。"""
        try:
            info = self.disks[batch.disk_id]

            # 动盘前的批次级权威判定：
            # - 槽位仍指向本批次所属盘/代次时，故障开关生效 → 整批失败、不动盘；
            # - 槽位已换走（代次不符）时按演练约定，批次仍在原盘完成，
            #   旧代次的其它写入由各自预检判 stale，不允许混入本批次。
            with self._lock:
                fail_now = self.fail_simulation
                gen_now = self.mount_generation
                mounted_now = self.mounted_id
            if gen_now == batch.mount_generation and mounted_now == batch.disk_id and fail_now:
                raise SimulatedFlushError(
                    "模拟落盘失败：故障开关已开启，整批写入在动盘前被拒绝，镜像未被修改"
                )

            with open(self._path_for(info), "r+b") as fh:
                for member in batch.members:
                    fh.seek(member.block * BLOCK_SIZE)
                    fh.write(member.payload)
                fh.flush()
                if batch.sync_delay:
                    # 演练用：模拟慢速同步，方便在 fsync 前换盘/读新盘
                    time.sleep(batch.sync_delay)
                os.fsync(fh.fileno())  # 整批仅这一次同步

            with self._lock:
                self.fsync_total += 1
                batch.fsync_index = self.fsync_total

            for member in batch.members:
                member.status = "done"
                member.batch_id = batch.batch_id

            batch.success = True
            with self._lock:
                blocks = ",".join(str(m.block) for m in batch.members)
                self._log(
                    "write",
                    f"批次 {batch.batch_id}：{info.name} 块[{blocks}] 共 {batch.size} 条写入"
                    f"合用 1 次 fsync 落盘（全局第 {batch.fsync_index} 次同步，"
                    f"代次{batch.mount_generation}）",
                )
                self._record_batch(batch, "done", "")
        except Exception as exc:  # 含 SimulatedFlushError / OSError
            batch.success = False
            batch.error = exc
            message = str(exc)
            for member in batch.members:
                member.status = "failed"
                member.error = message
                member.batch_id = batch.batch_id
            with self._lock:
                self._log(
                    "flush-fail",
                    f"批次 {batch.batch_id} 整批失败（{batch.size} 条写入，"
                    f"块{sorted(batch.joined_blocks)}），镜像未被修改：{message}",
                )
                self._record_batch(batch, "failed", message)
            raise
        finally:
            # 无论成败都放行等待中的成员，避免挂死
            batch.event.set()

    def _flush(self, pw: PendingWrite) -> None:
        """真正落盘的唯一入口：慢写睡眠、失效判定、失败模拟、合批 fsync。"""
        if pw.delay:
            time.sleep(pw.delay)

        # 1) 动盘前预检：失效 / 故障（不持块锁）
        info = self._precheck(pw)

        # 2) 整块写入受 (盘,块) 锁保护：同块串行不撕裂；参与批次的块在同步
        #    结束前一直持锁，回读必须等同步完成
        with self._block_lock(pw.disk_id, pw.block):
            # 拿锁后二次核对，防止睡眠/等锁期间发生换盘或拨动故障开关
            with self._lock:
                gen2 = self.mount_generation
                mounted2 = self.mounted_id
                fail2 = self.fail_simulation
            if gen2 != pw.mount_generation or mounted2 != pw.disk_id:
                pw.status = "stale"
                pw.error = f"写入失效：落盘前槽位代次已变为 {gen2}"
                with self._lock:
                    self._log("stale", f"写入失效(二次核对): {pw.disk_name} 块{pw.block}")
                raise StaleWriteError(pw.error)
            if fail2:
                pw.status = "failed"
                pw.error = "模拟落盘失败：镜像未被修改"
                with self._lock:
                    self._log("flush-fail", f"模拟落盘失败(二次核对): 块{pw.block}")
                raise SimulatedFlushError(pw.error)

            batch, is_leader = self._join_batch(pw, info)
            pw.batch_id = batch.batch_id

            if not is_leader:
                # 成员：块锁仍持有，阻塞到 leader 完成整批唯一一次 fsync
                batch.event.wait()
                if not batch.success:
                    # 状态/错误已由 _run_batch（或 leader 的中断收尾）统一写好
                    if batch.error is not None:
                        raise batch.error
                    raise DiskError("同步批次异常结束，镜像未确认落盘")
                return

            # leader：执行合批写入与单次同步
            self._run_batch(batch)
