#!/usr/bin/env python3
"""端到端演练：通过真实 HTTP 接口模拟网页操作，覆盖四条验收路径。

仅使用标准库，可用 `python3 -I scripts/e2e_drill.py` 运行。
"""

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
DATA_DIR = os.environ.get("DISK_DATA_DIR", "/tmp/e2e-data")
BLOCK_SIZE = 64

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def payload(text):
    return text.encode("latin-1")[:BLOCK_SIZE].ljust(BLOCK_SIZE, b".").decode("latin-1")


def image_hashes():
    out = {}
    for name in sorted(os.listdir(DATA_DIR)):
        if name.endswith(".img"):
            with open(os.path.join(DATA_DIR, name), "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()
    return out


def state():
    return call("GET", "/api/state")[1]["state"]


def main():
    # 0. 页面与重置
    with urllib.request.urlopen(BASE + "/", timeout=10) as resp:
        page = resp.read().decode()
    check("首页可访问且为演练台页面", resp.status == 200 and "热换盘" in page)

    call("POST", "/api/reseed")
    call("POST", "/api/fail-simulation", {"enabled": False})
    call("POST", "/api/gate", {"open": True})
    st = state()
    mounted = st["mounted"]["disk_id"]
    other = next(d["disk_id"] for d in st["disks"] if d["disk_id"] != mounted)
    base_hashes = image_hashes()
    print(f"  初始挂载={st['mounted']['name']} 代次={st['mount_generation']} 镜像哈希={base_hashes}")

    # 1. 慢写途中换盘 -> 失效，两份镜像都不变
    box = {}

    def slow_write():
        box["status"], box["body"] = call(
            "POST", "/api/blocks/1/write",
            {"payload": payload("SLOW-E2E-WRITE"), "delay": 3, "note": "慢写"},
        )

    t = threading.Thread(target=slow_write)
    t.start()
    time.sleep(0.8)  # 写入在服务端睡眠、尚未落盘
    call("POST", "/api/slot/plug", {"disk_id": other})
    t.join()
    check(
        "慢写落盘前换盘，写入报失效(HTTP 409 StaleWriteError)",
        box["status"] == 409 and box["body"]["type"] == "StaleWriteError",
        box["body"].get("error", ""),
    )
    check("失效后两份镜像哈希均未变化", image_hashes() == base_hashes)

    # 1b. 暂停排队后换盘再插回原盘，旧写入依旧失效
    #     此刻槽位在 other(B) 上，写入即在 B 上排队
    call("POST", "/api/gate", {"open": False})
    _, q = call("POST", "/api/blocks/4/write", {"payload": payload("QUEUED-THEN-SWAP")})
    wid = q["result"]["write"]["id"]
    call("POST", "/api/slot/plug", {"disk_id": mounted})  # 拔出 B 换到 A
    call("POST", "/api/slot/plug", {"disk_id": other})    # 再把原盘 B 插回
    status, body = call("POST", f"/api/pending/{wid}/release")
    check(
        "换过盘即使插回原盘，放行仍报失效",
        status == 409 and body["type"] == "StaleWriteError",
        body.get("error", ""),
    )
    check("插回场景下镜像哈希仍未变化", image_hashes() == base_hashes)

    # 2. 暂停期间换盘并读写新挂载盘，放行后旧失效、新成功
    call("POST", "/api/gate", {"open": False})
    _, q_old = call("POST", "/api/blocks/5/write", {"payload": payload("OLD-DISK-WRITE")})
    old_id = q_old["result"]["write"]["id"]
    call("POST", "/api/slot/plug", {"disk_id": mounted})  # 换到 A
    _, read_new = call("GET", "/api/blocks/5")
    check("暂停期间可读取新挂载盘", read_new["result"]["disk_id"] == mounted)
    _, q_new = call("POST", "/api/blocks/6/write", {"payload": payload("NEW-DISK-WRITE")})
    new_id = q_new["result"]["write"]["id"]
    check("暂停期间对新盘的写入进入排队", q_new["result"]["queued"] is True)

    _, rel = call("POST", "/api/pending/release-all")
    statuses = {r["id"]: r["status"] for r in rel["result"]["results"]}
    check(
        "全部放行：旧盘写入 stale、新盘写入 done",
        statuses.get(old_id) == "stale" and statuses.get(new_id) == "done",
        str(statuses),
    )
    _, rb = call("GET", "/api/blocks/6")
    check("新盘块6改动保留且内容正确", rb["result"]["data"].startswith("NEW-DISK-WRITE"))
    call("POST", "/api/slot/plug", {"disk_id": other})
    _, ra = call("GET", "/api/blocks/5")
    check("旧盘块5从未被旧写入改动", not ra["result"]["data"].startswith("OLD-DISK-WRITE"))

    # 3. 同盘不同块并发写入，两处改动都保留
    call("POST", "/api/gate", {"open": True})
    call("POST", "/api/slot/plug", {"disk_id": mounted})  # 确保挂 A
    barrier = threading.Barrier(2)
    conc = {}

    def conc_writer(key, block, text):
        barrier.wait()
        conc[key] = call(
            "POST", f"/api/blocks/{block}/write",
            {"payload": payload(text), "delay": 1, "note": "并发"},
        )

    t1 = threading.Thread(target=conc_writer, args=("a", 10, "CONC-AAA"))
    t2 = threading.Thread(target=conc_writer, args=("b", 11, "CONC-BBB"))
    start = time.time()
    t1.start(); t2.start(); t1.join(); t2.join()
    elapsed = time.time() - start
    ok_status = conc["a"][0] == 200 and conc["b"][0] == 200
    _, va = call("GET", "/api/blocks/10")
    _, vb = call("GET", "/api/blocks/11")
    both_kept = va["result"]["data"].startswith("CONC-AAA") and vb["result"]["data"].startswith("CONC-BBB")
    check("两个不同块并发写入均返回成功", ok_status, f"耗时 {elapsed:.2f}s（含各1s慢写，并行应明显<2s）")
    check("两处改动回读均保留", both_kept)

    # 3b. 同块并发不撕裂
    barrier4 = threading.Barrier(4)
    markers = [f"MARK-{i}-" for i in range(4)]

    def same_block_writer(marker):
        barrier4.wait()
        call("POST", "/api/blocks/12/write", {"payload": payload(marker)})

    threads = [threading.Thread(target=same_block_writer, args=(m,)) for m in markers]
    for t in threads: t.start()
    for t in threads: t.join()
    _, r12 = call("GET", "/api/blocks/12")
    data12 = r12["result"]["data"]
    longest = max(markers, key=len)
    no_tear = (
        len(data12) == BLOCK_SIZE
        and any(data12.startswith(m) for m in markers)
        and set(data12[len(longest):]) <= {"."}
    )
    check("同一块并发写入无撕裂（落盘内容为某个完整定长块）", no_tear)

    # 4. 模拟落盘失败：报错且原块完整
    _, before = call("GET", "/api/blocks/20")
    before_data = before["result"]["data"]
    hashes_before_fail = image_hashes()
    call("POST", "/api/fail-simulation", {"enabled": True})
    fstatus, fbody = call("POST", "/api/blocks/20/write", {"payload": payload("FAILED-WRITE")})
    check(
        "模拟落盘失败返回 500 SimulatedFlushError",
        fstatus == 500 and fbody["type"] == "SimulatedFlushError",
        fbody.get("error", ""),
    )
    _, after = call("GET", "/api/blocks/20")
    check("报错后重读仍是原块完整内容", after["result"]["data"] == before_data)
    check("失败写入未改动任何镜像", image_hashes() == hashes_before_fail)
    call("POST", "/api/fail-simulation", {"enabled": False})
    ok_status2, _ = call("POST", "/api/blocks/20/write", {"payload": payload("RECOVERED-WRITE")})
    _, recovered = call("GET", "/api/blocks/20")
    check("关闭故障注入后写入恢复正常", ok_status2 == 200 and recovered["result"]["data"].startswith("RECOVERED-WRITE"))

    # 汇总
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n==== 端到端演练结果：{passed}/{total} 通过 ====")
    if passed != total:
        for name, ok, detail in results:
            if not ok:
                print(f"  未通过: {name} {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
