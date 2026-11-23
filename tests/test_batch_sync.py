"""合批同步（一次 fsync 覆盖多块）的专项测试。

覆盖验收点：
* 同盘、同挂载代次、不同块的两条并发写入合用一次 fsync；单条写入也能落盘。
* 批次成员等同步成功才返回；参与批次的块在同步结束前回读会阻塞，结束后才读到新内容。
* 同步进行中换盘：旧批次只在原盘完成；新盘同号块可立即读写（另一把块锁）。
* 仍在等批次的旧挂载代次写入报 stale，任何镜像不被它改动。
* 故障开关打开时，尚未动盘的批次整批报 SimulatedFlushError，原块保持不变。
"""

import threading
import time

import pytest

from app.disk_manager import SimulatedFlushError, StaleWriteError

from conftest import all_hashes, fixed


def _wait_for_open_batch(manager, min_members=1, timeout=2.0):
    """等到开放批次建立且成员数达到 min_members（避免合流窗口内提前取样）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with manager._batch_cond:
            if manager._open_batch is not None and len(manager._open_batch.members) >= min_members:
                return manager._open_batch
        time.sleep(0.01)
    raise AssertionError("等待同步批次建立超时")


# --------------------------------------------------------------- 合批：1 次 fsync
def test_concurrent_different_blocks_share_single_fsync(manager):
    manager.batch_join_window = 0.5
    before_fsync = manager.fsync_total
    barrier = threading.Barrier(2)
    outcomes = []

    def writer(block, text):
        barrier.wait()
        try:
            res = manager.write_block(block, fixed(text), note="合批测试")
            outcomes.append(res["write"])
        except Exception as exc:  # pragma: no cover - 失败即暴露问题
            outcomes.append(exc)

    t1 = threading.Thread(target=writer, args=(11, "BATCH-AAA"))
    t2 = threading.Thread(target=writer, args=(12, "BATCH-BBB"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(outcomes) == 2
    assert all(w["status"] == "done" for w in outcomes)
    # 两条写入同属一个批次
    assert outcomes[0]["batch_id"] == outcomes[1]["batch_id"]
    # 关键验收：两条写入只新增 1 次 fsync
    assert manager.fsync_total - before_fsync == 1

    assert manager.read_block(11)["data"].startswith("BATCH-AAA")
    assert manager.read_block(12)["data"].startswith("BATCH-BBB")

    batch = manager.batch_history[-1]
    assert batch["size"] == 2
    assert sorted(batch["blocks"]) == [11, 12]
    assert batch["status"] == "done"


def test_single_write_still_flushes_on_its_own(manager):
    before = manager.fsync_total
    manager.write_block(2, fixed("SOLO-WRITE"))
    assert manager.fsync_total - before == 1
    assert manager.read_block(2)["data"].startswith("SOLO-WRITE")


def test_same_block_writes_cannot_share_batch(manager):
    """同块并发受块锁串行，必须分属两个批次、两次 fsync，且不撕裂。"""
    manager.batch_join_window = 0.3
    before = manager.fsync_total
    barrier = threading.Barrier(2)

    def writer(text):
        barrier.wait()
        manager.write_block(13, fixed(text))

    t1 = threading.Thread(target=writer, args=("SAME-X-",))
    t2 = threading.Thread(target=writer, args=("SAME-Y-",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert manager.fsync_total - before == 2
    data = manager.read_block(13)["data"]
    assert data.startswith(("SAME-X-", "SAME-Y-"))


# --------------------------------------------------------------- 回读等同步结束
def test_read_of_batched_block_waits_until_sync_done(manager):
    manager.batch_join_window = 0.2
    read_out = {}

    t = threading.Thread(
        target=lambda: manager.write_block(9, fixed("WAIT-READ"), sync_delay=1.5)
    )
    t.start()
    _wait_for_open_batch(manager)

    def reader():
        start = time.time()
        read_out["data"] = manager.read_block(9)["data"]
        read_out["elapsed"] = time.time() - start

    rt = threading.Thread(target=reader)
    rt.start()
    # 同步未结束时，回读应被块锁挡住（给 300ms 证明它确实在等）
    time.sleep(0.3)
    assert "data" not in read_out
    rt.join()
    t.join()

    assert read_out["elapsed"] >= 1.0  # 确实等到了 fsync 结束
    assert read_out["data"].startswith("WAIT-READ")


def test_read_of_other_block_not_blocked_by_batch(manager):
    manager.batch_join_window = 0.2
    t = threading.Thread(
        target=lambda: manager.write_block(9, fixed("LOCKED-BLOCK"), sync_delay=1.5)
    )
    t.start()
    _wait_for_open_batch(manager)

    start = time.time()
    data = manager.read_block(10)["data"]  # 不同块、不同锁
    assert time.time() - start < 0.5
    assert len(data) == 64
    t.join()


# --------------------------------------------------------------- 同步中换盘
def test_swap_during_sync_batch_completes_on_old_disk(manager, disk_files):
    manager.batch_join_window = 0.3
    a = manager.mounted_id
    b = next(d for d in manager.disks if d != a)
    barrier = threading.Barrier(2)
    errors = []

    def writer(block, text):
        barrier.wait()
        try:
            manager.write_block(block, fixed(text), sync_delay=2.0)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(3, "OLD-DISK-3"))
    t2 = threading.Thread(target=writer, args=(4, "OLD-DISK-4"))
    t1.start(); t2.start()
    batch = _wait_for_open_batch(manager, min_members=2)

    # 批次同步进行中换盘：不等待旧批次，立即生效
    manager.plug(b)
    assert manager.mounted_id == b

    # 新盘同号块立即可读（另一把 (盘,块) 锁），且是 B 的种子内容
    start = time.time()
    rb3 = manager.read_block(3)
    assert time.time() - start < 0.5
    assert rb3["disk_id"] == b
    assert "OLD-DISK-3" not in rb3["data"]

    # 新盘同号块也可立即写入，不被旧盘同步阻塞
    manager.write_block(3, fixed("NEW-DISK-3"))
    assert manager.read_block(3)["data"].startswith("NEW-DISK-3")

    t1.join(); t2.join()
    assert not errors

    # 旧批次只在原盘 A 完成
    manager.plug(a)
    assert manager.read_block(3)["data"].startswith("OLD-DISK-3")
    assert manager.read_block(4)["data"].startswith("OLD-DISK-4")

    # 旧批次 1 次 fsync + 新盘写入 1 次
    assert batch.success is True
    assert batch.size == 2


def test_old_generation_write_waiting_for_batch_is_stale(manager, disk_files):
    """批次占着块锁时，旧代次写入等到锁后二次核对，报失效且不改镜像。"""
    manager.batch_join_window = 0.2
    stale_errors = []

    # leader 长期占用 (A, 6) 块锁
    leader = threading.Thread(
        target=lambda: manager.write_block(6, fixed("LEADER-6"), sync_delay=2.0)
    )
    leader.start()
    _wait_for_open_batch(manager)

    def late_writer():
        try:
            # 发起时代次仍匹配，预检通过后阻塞在块锁上
            manager.write_block(6, fixed("LATE-STALE"))
        except StaleWriteError as exc:
            stale_errors.append(exc)

    lt = threading.Thread(target=late_writer)
    lt.start()
    time.sleep(0.3)  # 确保它已通过预检、正在等锁/等批次

    other = next(d for d in manager.disks if d != manager.mounted_id)
    manager.plug(other)  # 代次推进，旧写入注定失效

    lt.join()
    leader.join()

    assert len(stale_errors) == 1
    assert "失效" in str(stale_errors[0])
    # 原盘块 6 只有 leader 的内容，迟到写入从未落盘
    manager.plug(next(d for d in manager.disks if d != manager.mounted_id))
    assert manager.read_block(6)["data"].startswith("LEADER-6")
    assert "LATE-STALE" not in manager.read_block(6)["data"]


# --------------------------------------------------------------- 故障：整批失败、不动盘
def test_fail_switch_on_before_batch_rejects_all_members(manager, disk_files):
    before = all_hashes(disk_files)
    before_fsync = manager.fsync_total
    manager.set_fail_simulation(True)
    barrier = threading.Barrier(2)
    errors = []

    def writer(block, text):
        barrier.wait()
        try:
            manager.write_block(block, fixed(text))
        except SimulatedFlushError as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(7, "FAIL-A"))
    t2 = threading.Thread(target=writer, args=(8, "FAIL-B"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(errors) == 2
    assert manager.fsync_total == before_fsync       # 一次同步都没有发生
    assert all_hashes(disk_files) == before           # 原块全部保持不变
    assert "FAIL-A" not in manager.read_block(7)["data"]
    assert "FAIL-B" not in manager.read_block(8)["data"]


def test_fail_switch_during_join_window_aborts_whole_batch(manager, disk_files):
    """批次已建立、但尚未 open/write 时拨动故障开关：批次级守卫整批拒绝。"""
    manager.batch_join_window = 0.5
    before = all_hashes(disk_files)
    before_fsync = manager.fsync_total
    barrier = threading.Barrier(2)
    errors = []

    def writer(block, text):
        barrier.wait()
        try:
            manager.write_block(block, fixed(text))
        except SimulatedFlushError as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(17, "MIDFAIL-A"))
    t2 = threading.Thread(target=writer, args=(18, "MIDFAIL-B"))
    t1.start(); t2.start()
    _wait_for_open_batch(manager, min_members=2)
    # 合流窗口内、动盘之前打开故障开关
    manager.set_fail_simulation(True)
    t1.join(); t2.join()

    assert len(errors) == 2
    assert manager.fsync_total == before_fsync
    assert all_hashes(disk_files) == before
    failed = [b for b in manager.batch_history if b["status"] == "failed"]
    assert failed and failed[-1]["size"] == 2
    assert sorted(failed[-1]["blocks"]) == [17, 18]


def test_failed_batch_then_recovered_write_lands(manager):
    manager.set_fail_simulation(True)
    with pytest.raises(SimulatedFlushError):
        manager.write_block(19, fixed("NEVER-LANDS"))
    manager.set_fail_simulation(False)
    manager.write_block(19, fixed("NOW-LANDS"))
    assert manager.read_block(19)["data"].startswith("NOW-LANDS")
