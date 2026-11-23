import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import create_app
from app.disk_manager import BLOCK_SIZE, DiskManager


@pytest.fixture()
def data_dir(tmp_path):
    return str(tmp_path / "data")


@pytest.fixture()
def manager(data_dir):
    return DiskManager(data_dir)


@pytest.fixture()
def client(data_dir):
    app = create_app(data_dir=data_dir)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture()
def disk_files(manager):
    """返回两份镜像文件的路径。"""
    return {
        info.disk_id: os.path.join(manager.data_dir, info.filename)
        for info in manager.disks.values()
    }


def sha(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def all_hashes(disk_files):
    return {k: sha(v) for k, v in disk_files.items()}


def fixed(text):
    """生成恰好 BLOCK_SIZE 字节的定长 payload。"""
    return text.encode("latin-1")[:BLOCK_SIZE].ljust(BLOCK_SIZE, b".")
