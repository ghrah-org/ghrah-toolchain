# SPDX-License-Identifier: Apache-2.0
"""通过 python -m ghrah_toolchain 启动工具链。"""

from ghrah_toolchain.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
