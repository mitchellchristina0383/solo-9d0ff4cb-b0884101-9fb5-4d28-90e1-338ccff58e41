"""功能与验收测试：热换盘失效、暂停放行、失败模拟、并发写入、镜像完整性。"""

import os
import threading

import pytest

from app.disk_manager import (
    BLOCK_COUNT,
    BLOCK_SIZE,
    SimulatedFlushError,
    StaleWriteError,
)

from conftest import all_hashes, fixed, sha


# --------------------------------------------------------------- 基础播种
def test_seed_two_images_with_fixed_blocks(manager, disk_files):
    assert len(manager.disks) == 2
    for path in disk_files.values():
        assert os.path.getsize(path) == BLOCK_SIZE * BLOCK_COUNT
    # 两份盘的种子内容不同
    paths = list(disk_files.values())
    assert sha(paths[0]) != sha(paths[1])


def test_read_block_roundtrip(manager):
    res = manager.read_block(0)
    assert len(res["data"]) == BLOCK_SIZE
    assert "block 00" in res["data"]


def test_write_requires_exact_block_size(manager):
    with pytest.raises(Exception):
        manager.write_block(0, b"too short")
    with pytest.raises(Exception):
        manager.write_block(0, b"x" * (BLOCK_SIZE + 1))


def test_invalid_block_index(manager):
    with pytest.raises(Exception):
        manager.read_block(BLOCK_COUNT)
    with pytest.raises(Exception):
        manager.write_block(-1, fixed("x"))


# --------------------------------------------------------------- 直接写入
def test_direct_write_persists(manager):
    manager.write_block(2, fixed("HELLO-DIRECT"))
    data = manager.read_block(2)["data"]
    assert data.startswith("HELLO-DIRECT")


# --------------------------------------------------------------- 慢写途中换盘 -> 失效，镜像不动
def test_slow_write_then_swap_disk_is_stale_and_image_untouched(manager, disk_files):
    before = all_hashes(disk_files)
    other_disk = next(d for d in manager.disks if d != manager.mounted_id)

    errors = []

    def slow_write():
        try:
            manager.write_block(1, fixed("SLOW-WRITE"), delay=1.0)
        except StaleWriteError as exc:
            errors.append(exc)

    t = threading.Thread(target=slow_write)
    t.start()
    # 写入正在睡眠、尚未落盘时换盘
    threading.Event().wait(0.3)
    manager.plug(other_disk)
    t.join()

    assert len(errors) == 1
    assert "失效" in str(errors[0])
    # 关键验收：两份镜像哈希与写入前完全一致
    assert all_hashes(disk_files) == before


def test_stale_even_when_original_disk_plugged_back(manager, disk_files):
    """换过盘再插回原盘，旧写入仍失效，且镜像不变。"""
    original = manager.mounted_id
    other = next(d for d in manager.disks if d != original)
    before = all_hashes(disk_files)

    manager.set_gate(False)
    queued = manager.write_block(4, fixed("QUEUED-OLD"))
    assert queued["queued"] is True
    write_id = queued["write"]["id"]

    manager.plug(other)       # 换到 B
    manager.plug(original)    # 再插回 A（代次又 +1）

    with pytest.raises(StaleWriteError):
        manager.release_one(write_id)

    assert all_hashes(disk_files) == before
    assert write_id not in manager.pending


# --------------------------------------------------------------- 暂停期间：换盘 + 读写新盘
def test_paused_writes_queue_and_new_disk_is_readable_writable(manager, disk_files):
    a = manager.mounted_id
    b = next(d for d in manager.disks if d != a)

    manager.set_gate(False)
    # A 盘上排队一个写入
    q = manager.write_block(5, fixed("ON-A"))
    assert q["queued"] is True

    # 暂停期间换盘到 B
    manager.plug(b)
    # 新挂载的盘可读
    readable = manager.read_block(5)
    assert readable["disk_id"] == b
    # 新盘写入也进入队列
    q2 = manager.write_block(6, fixed("ON-B"))
    assert q2["queued"] is True
    assert q2["write"]["disk_id"] == b

    # 放行：A 的旧写入失效；B 的新写入（代次匹配）成功落盘
    with pytest.raises(StaleWriteError):
        manager.release_one(q["write"]["id"])
    manager.release_one(q2["write"]["id"])

    assert manager.read_block(6)["data"].startswith("ON-B")
    # A 盘块 5 从未被修改
    manager.plug(a)
    assert "ON-A" not in manager.read_block(5)["data"]


def test_release_all_reports_mixed_results(manager):
    a = manager.mounted_id
    b = next(d for d in manager.disks if d != a)
    manager.set_gate(False)
    old = manager.write_block(7, fixed("OLD"))
    manager.plug(b)
    fresh = manager.write_block(8, fixed("FRESH"))

    out = manager.release_all()
    statuses = {r["id"]: r["status"] for r in out["results"]}
    assert statuses[old["write"]["id"]] == "stale"
    assert statuses[fresh["write"]["id"]] == "done"
    assert manager.pending == {}


# --------------------------------------------------------------- 模拟落盘失败：原块完整
def test_simulated_flush_failure_keeps_original_block(manager, disk_files):
    original = manager.read_block(9)["data"]
    before = all_hashes(disk_files)

    manager.set_fail_simulation(True)
    with pytest.raises(SimulatedFlushError):
        manager.write_block(9, fixed("SHOULD-NOT-LAND"))

    assert manager.read_block(9)["data"] == original
    assert all_hashes(disk_files) == before

    # 关闭失败模拟后同内容可以正常写入
    manager.set_fail_simulation(False)
    manager.write_block(9, fixed("NOW-IT-LANDS"))
    assert manager.read_block(9)["data"].startswith("NOW-IT-LANDS")


def test_fail_simulation_inside_pending_release(manager, disk_files):
    before = all_hashes(disk_files)
    manager.set_gate(False)
    q = manager.write_block(10, fixed("PENDING-FAIL"))
    manager.set_fail_simulation(True)
    with pytest.raises(SimulatedFlushError):
        manager.release_one(q["write"]["id"])
    assert all_hashes(disk_files) == before


# --------------------------------------------------------------- 并发：同盘不同块，两处都保留
def test_concurrent_writes_different_blocks_both_kept(manager):
    barrier = threading.Barrier(2)
    results = []

    def writer(block, text):
        barrier.wait()
        try:
            manager.write_block(block, fixed(text), delay=0.2)
            results.append((block, "ok"))
        except Exception as exc:  # pragma: no cover - 失败即暴露问题
            results.append((block, repr(exc)))

    t1 = threading.Thread(target=writer, args=(11, "CONC-AAA"))
    t2 = threading.Thread(target=writer, args=(12, "CONC-BBB"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [(11, "ok"), (12, "ok")]
    assert manager.read_block(11)["data"].startswith("CONC-AAA")
    assert manager.read_block(12)["data"].startswith("CONC-BBB")


def test_concurrent_writes_same_block_no_torn_write(manager):
    """同块并发写入：串行化落盘，每次都是完整定长块，不出现撕裂。"""
    barrier = threading.Barrier(4)
    errors = []

    def writer(text):
        barrier.wait()
        try:
            manager.write_block(13, fixed(text))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"VAL-{i}-",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    data = manager.read_block(13)["data"]
    assert len(data) == BLOCK_SIZE
    assert any(data.startswith(f"VAL-{i}-") for i in range(4))


# --------------------------------------------------------------- 拔盘/空槽
def test_unplug_then_write_requires_disk(manager):
    manager.unplug()
    with pytest.raises(Exception):
        manager.write_block(0, fixed("X"))
    with pytest.raises(Exception):
        manager.read_block(0)


def test_discard_pending_does_not_touch_image(manager, disk_files):
    before = all_hashes(disk_files)
    manager.set_gate(False)
    q = manager.write_block(14, fixed("DISCARD-ME"))
    manager.discard_pending(q["write"]["id"])
    assert all_hashes(disk_files) == before
    assert manager.pending == {}


def test_reseed_restores_seed_content(manager):
    manager.write_block(0, fixed("DIRTY"))
    manager.reseed()
    assert "DIRTY" not in manager.read_block(0)["data"]
    assert manager.gate_open is True
    assert manager.fail_simulation is False
    assert manager.pending == {}
