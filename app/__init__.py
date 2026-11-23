"""Flask 路由：虚拟磁盘热换盘演练页后端。"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from .disk_manager import (
    BLOCK_COUNT,
    BLOCK_SIZE,
    DiskError,
    DiskManager,
    SimulatedFlushError,
    StaleWriteError,
)


def create_app(data_dir: str | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    data_dir = data_dir or os.environ.get("DISK_DATA_DIR", "/data")
    manager = DiskManager(data_dir)
    app.config["MANAGER"] = manager

    def err(exc: DiskError, status: int = 409):
        return jsonify({"ok": False, "error": str(exc), "type": exc.__class__.__name__}), status

    @app.errorhandler(DiskError)
    def handle_disk_error(exc: DiskError):
        return err(exc, 409)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        app.logger.exception("unexpected error")
        return jsonify({"ok": False, "error": f"服务器内部错误: {exc}", "type": "ServerError"}), 500

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/state")
    def get_state():
        return jsonify({"ok": True, "state": manager.snapshot()})

    @app.post("/api/slot/plug")
    def plug():
        disk_id = (request.get_json(silent=True) or {}).get("disk_id", "")
        result = manager.plug(disk_id)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/slot/unplug")
    def unplug():
        result = manager.unplug()
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/gate")
    def set_gate():
        body = request.get_json(silent=True) or {}
        open_gate = bool(body.get("open", True))
        result = manager.set_gate(open_gate)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/fail-simulation")
    def set_fail():
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", False))
        result = manager.set_fail_simulation(enabled)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.get("/api/blocks/<int:block>")
    def read_block(block: int):
        result = manager.read_block(block)
        return jsonify({"ok": True, "result": result})

    def _extract_payload(raw: str) -> bytes:
        # 前端约定 latin-1 透传字节，再按定长块校验
        if raw is None:
            raise DiskError("缺少 payload 字段")
        if isinstance(raw, str):
            payload = raw.encode("latin-1", errors="replace")
        else:
            raise DiskError("payload 必须是字符串")
        if len(payload) != BLOCK_SIZE:
            raise DiskError(
                f"定长块写入要求恰好 {BLOCK_SIZE} 字节，当前 {len(payload)} 字节"
            )
        return payload

    @app.post("/api/blocks/<int:block>/write")
    def write_block(block: int):
        body = request.get_json(silent=True) or {}
        payload = _extract_payload(body.get("payload", ""))
        delay = float(body.get("delay", 0) or 0)
        sync_delay = float(body.get("sync_delay", 0) or 0)
        note = str(body.get("note", ""))
        try:
            result = manager.write_block(
                block, payload, delay=delay, note=note, sync_delay=sync_delay
            )
        except StaleWriteError as exc:
            return err(exc, 409)
        except SimulatedFlushError as exc:
            return err(exc, 500)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/pending/<write_id>/release")
    def release_one(write_id: str):
        try:
            result = manager.release_one(write_id)
        except StaleWriteError as exc:
            return err(exc, 409)
        except SimulatedFlushError as exc:
            return err(exc, 500)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/pending/release-all")
    def release_all():
        result = manager.release_all()
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/pending/<write_id>/discard")
    def discard(write_id: str):
        result = manager.discard_pending(write_id)
        return jsonify({"ok": True, "result": result, "state": manager.snapshot()})

    @app.post("/api/reseed")
    def reseed():
        manager.reseed()
        return jsonify({"ok": True, "state": manager.snapshot()})

    @app.get("/api/meta")
    def meta():
        return jsonify(
            {
                "ok": True,
                "block_size": BLOCK_SIZE,
                "block_count": BLOCK_COUNT,
            }
        )

    return app
