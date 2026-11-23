#!/usr/bin/env python3
"""重新生成两份盘镜像并重置演练状态（容器内或本地均可运行）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.disk_manager import DiskManager  # noqa: E402


def main() -> None:
    data_dir = os.environ.get("DISK_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    manager = DiskManager(data_dir)
    manager.reseed()
    print(f"reseed done in {data_dir}: {sorted(os.listdir(data_dir))}")


if __name__ == "__main__":
    main()
