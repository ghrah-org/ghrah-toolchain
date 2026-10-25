# SPDX-License-Identifier: Apache-2.0
"""工具链内部后台监督进程入口，不属于公开命令面。"""

from __future__ import annotations

import argparse
import logging

from ghrah_toolchain.instances import serve_instance


def main() -> int:
    """解析受管启动参数并接管具名实例生命周期。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("token")
    parser.add_argument("--no-webui", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    return serve_instance(args.name, args.token, no_webui=args.no_webui)


if __name__ == "__main__":
    raise SystemExit(main())
