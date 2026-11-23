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
* 每个块有独立锁，不同块的写入可以真正并行；同一把块锁保证同块写入串行、
  落盘的整块写入不会撕裂。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "disk_id": self.disk_id,
            "disk_name": self.disk_name,
            "block": self.block,
            "payload": self.payload.decode("latin-1"),
            "mount_generation": self.mount_generation,
            "delay": self.delay,
            "created_at": self.created_at,
            "note": self.note,
            "status": self.status,
            "error": self.error,
        }


def _seed_block_content(disk_name: str, block: int) -> bytes:
    text = f"[{disk_name} block {block:02d}] " + ("seed-" + str(block)) * 8
    return text.encode("utf-8")[:BLOCK_SIZE].ljust(BLOCK_SIZE, b".")


class DiskManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.meta_path = os.path.join(self.data_dir, META_FILENAME)

        self._lock = threading.RLock()           # 保护槽位/队列/元数据
        self._block_locks: Dict[int, threading.Lock] = {
            i: threading.Lock() for i in range(BLOCK_COUNT)
        }
        self.disks: Dict[str, DiskInfo] = {}
        self.mounted_id: Optional[str] = None
        self.mount_generation = 0
        self.gate_open = True                    # True=放行, False=暂停
        self.fail_simulation = False
        self.pending: Dict[str, PendingWrite] = {}
        self.event_log: List[dict] = []
        self._log_max = 200

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
            first = sorted(self.disks.values(), key=lambda d: d.filename)[0]
            self.mounted_id = first.disk_id
            self.mount_generation = 1
            self._log("reseed", "两份镜像已重新生成，槽位与写入门已重置")

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
                f"此前排队的旧写入若代次不符将在放行时失效",
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

    # ------------------------------------------------------------------ 读
    def read_block(self, block: int) -> dict:
        with self._lock:
            info = self._require_mounted()
            self._validate(block, b"x" * BLOCK_SIZE)  # 只校验块号
        # 读也拿块锁，保证不会读到正在写一半的块
        with self._block_locks[block]:
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
    ) -> dict:
        """对当前挂载盘发起定长块写入。

        门放行时直接落盘；门暂停时入队等待。慢写(delay>0)在落盘前睡眠，
        以便演练“写入尚未落盘时暂停/换盘”。
        """
        delay = max(0.0, float(delay))
        with self._lock:
            info = self._require_mounted()
            self._validate(block, payload)
            generation = self.mount_generation
            if self.gate_open:
                pw = PendingWrite(
                    id=uuid.uuid4().hex[:12],
                    disk_id=info.disk_id,
                    disk_name=info.name,
                    block=block,
                    payload=bytes(payload),
                    mount_generation=generation,
                    delay=delay,
                    created_at=time.time(),
                    note=note or "直接写入",
                )
            else:
                pw = PendingWrite(
                    id=uuid.uuid4().hex[:12],
                    disk_id=info.disk_id,
                    disk_name=info.name,
                    block=block,
                    payload=bytes(payload),
                    mount_generation=generation,
                    delay=delay,
                    created_at=time.time(),
                    note=note or "暂停期间排队",
                )
                self.pending[pw.id] = pw
                self._log(
                    "enqueue",
                    f"写入已排队: {info.name} 块{block} (代次{generation})，等待放行",
                )
                return {"queued": True, "write": pw.to_dict()}

        # 门放行：直接落盘（块锁内完成）
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

    def _flush(self, pw: PendingWrite) -> None:
        """真正落盘的唯一入口，包含慢写睡眠、失效判定与失败模拟。"""
        if pw.delay:
            time.sleep(pw.delay)

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

        # 2) 模拟落盘失败：在真正写文件之前抛错，原块内容完整保留
        if fail:
            pw.status = "failed"
            pw.error = "模拟落盘失败：写入门已拒绝本次写入，镜像未被修改"
            with self._lock:
                self._log("flush-fail", f"模拟落盘失败: {pw.disk_name} 块{pw.block}，原块内容保留")
            raise SimulatedFlushError(pw.error)

        if not current_info:
            pw.status = "stale"
            pw.error = "写入失效：槽位当前为空"
            raise StaleWriteError(pw.error)

        # 3) 真正的整块写入，块锁保证定长、原子视图、不同块可并行
        with self._block_locks[pw.block]:
            # 拿锁后二次核对，防止睡眠/等锁期间发生换盘
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

            with open(self._path_for(current_info), "r+b") as fh:
                fh.seek(pw.block * BLOCK_SIZE)
                fh.write(pw.payload)
                fh.flush()
                os.fsync(fh.fileno())

        pw.status = "done"
        with self._lock:
            self._log(
                "write",
                f"落盘成功: {current_info.name} 块{pw.block} "
                f"(代次{pw.mount_generation}, {len(pw.payload)}字节)",
            )
