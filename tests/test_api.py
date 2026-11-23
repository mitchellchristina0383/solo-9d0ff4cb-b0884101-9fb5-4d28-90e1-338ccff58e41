"""通过 Flask 测试客户端验证 HTTP 层行为与错误码。"""

from conftest import fixed


def _payload(text):
    return fixed(text).decode("latin-1")


def test_meta_and_state(client):
    rv = client.get("/api/meta")
    assert rv.status_code == 200
    assert rv.get_json()["block_size"] == 64

    rv = client.get("/api/state")
    body = rv.get_json()
    assert body["ok"] is True
    assert body["state"]["mounted"] is not None
    assert len(body["state"]["disks"]) == 2


def test_index_page_served(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"hotswap" in rv.data.lower() or "热换盘".encode() in rv.data


def test_http_write_and_read(client):
    rv = client.post("/api/blocks/3/write", json={"payload": _payload("HTTP-WRITE")})
    assert rv.status_code == 200
    assert rv.get_json()["result"]["write"]["status"] == "done"

    rv = client.get("/api/blocks/3")
    assert rv.get_json()["result"]["data"].startswith("HTTP-WRITE")


def test_http_bad_payload_length_rejected(client):
    rv = client.post("/api/blocks/0/write", json={"payload": "short"})
    assert rv.status_code == 409
    assert "字节" in rv.get_json()["error"]


def test_http_plug_swaps_generation(client):
    disks = client.get("/api/state").get_json()["state"]["disks"]
    other = next(d for d in disks if d["disk_id"] != client.get("/api/state").get_json()["state"]["mounted"]["disk_id"])
    gen_before = client.get("/api/state").get_json()["state"]["mount_generation"]

    rv = client.post("/api/slot/plug", json={"disk_id": other["disk_id"]})
    assert rv.status_code == 200
    assert rv.get_json()["state"]["mount_generation"] == gen_before + 1


def test_http_stale_write_returns_409_and_image_unchanged(client, data_dir):
    import hashlib
    import os

    state = client.get("/api/state").get_json()["state"]
    img_dir = data_dir
    def images_hash():
        h = hashlib.sha256()
        for name in sorted(os.listdir(img_dir)):
            with open(os.path.join(img_dir, name), "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()

    before = images_hash()
    disks = state["disks"]
    other = next(d for d in disks if d["disk_id"] != state["mounted"]["disk_id"])

    # 暂停 -> 排队 -> 换盘 -> 放行 -> 409 失效
    client.post("/api/gate", json={"open": False})
    q = client.post("/api/blocks/1/write", json={"payload": _payload("STALE-HTTP")})
    write_id = q.get_json()["result"]["write"]["id"]
    client.post("/api/slot/plug", json={"disk_id": other["disk_id"]})

    rv = client.post(f"/api/pending/{write_id}/release")
    assert rv.status_code == 409
    body = rv.get_json()
    assert body["type"] == "StaleWriteError"
    assert "失效" in body["error"]
    assert images_hash() == before


def test_http_simulated_failure_returns_500(client):
    original = client.get("/api/blocks/2").get_json()["result"]["data"]
    client.post("/api/fail-simulation", json={"enabled": True})
    rv = client.post("/api/blocks/2/write", json={"payload": _payload("NEVER-LANDS")})
    assert rv.status_code == 500
    assert rv.get_json()["type"] == "SimulatedFlushError"
    assert client.get("/api/blocks/2").get_json()["result"]["data"] == original


def test_http_release_all_mixed(client):
    state = client.get("/api/state").get_json()["state"]
    other = next(d for d in state["disks"] if d["disk_id"] != state["mounted"]["disk_id"])
    client.post("/api/gate", json={"open": False})
    old = client.post("/api/blocks/7/write", json={"payload": _payload("OLD-HTTP")}).get_json()
    client.post("/api/slot/plug", json={"disk_id": other["disk_id"]})
    fresh = client.post("/api/blocks/8/write", json={"payload": _payload("FRESH-HTTP")}).get_json()

    rv = client.post("/api/pending/release-all")
    results = {r["id"]: r["status"] for r in rv.get_json()["result"]["results"]}
    assert results[old["result"]["write"]["id"]] == "stale"
    assert results[fresh["result"]["write"]["id"]] == "done"


def test_http_unplug_blocks_io(client):
    client.post("/api/slot/unplug")
    rv = client.get("/api/blocks/0")
    assert rv.status_code == 409
    rv = client.post("/api/blocks/0/write", json={"payload": _payload("X")})
    assert rv.status_code == 409


def test_http_reseed(client):
    client.post("/api/blocks/0/write", json={"payload": _payload("DIRTY")})
    rv = client.post("/api/reseed")
    assert rv.status_code == 200
    state = rv.get_json()["state"]
    assert state["gate_open"] is True
    assert "DIRTY" not in client.get("/api/blocks/0").get_json()["result"]["data"]
