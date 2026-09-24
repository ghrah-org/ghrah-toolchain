# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ghrah-bootloader 命令面（distro/install/config/boot/stop/runtime/doctor）。

纯性硬约束：本模块及其 import 图**零 ``ghrah.*``、零第三方运行时依赖**
（仅 stdlib）。提供骨架与 distro/list、stop、doctor 基础实现；
install / boot / runtime 的完整引导链路在后续批次补齐。
"""

from __future__ import annotations

import argparse
import logging
import sys

from ghrah_bootloader import __version__
from ghrah_bootloader.errors import BootloaderError
from ghrah_bootloader.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    list_distros,
    resolve_distro_root,
)

logger = logging.getLogger(__name__)


def _cmd_distro_list(args: argparse.Namespace) -> int:
    """列出发行版与版本（读 ~/.ghrah 布局 = 读 GPT）。"""
    del args  # 未用（预留 --json 等输出控制）
    boot = BootToml.load()
    distros = list_distros()
    if not distros:
        print("（空安装域：尚无发行版）")
        return 0
    print(f"{'distro':<16} {'default':<8} {'versions'}")
    for distro in distros:
        layout = DistroLayout(root=resolve_distro_root(distro))
        versions = layout.list_versions()
        marker = ""
        if boot.default and boot.default.startswith(f"{distro}/"):
            marker = boot.default.split("/", 1)[1]
        current = ""
        try:
            current = layout.current.resolve().name
        except (OSError, RuntimeError):
            current = "-"
        print(f"{distro:<16} {marker or '-':<8} {', '.join(versions) or '-'} (current: {current})")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """诊断：安装域布局、boot.toml、各发行版 ref/端口。"""
    del args
    boot = BootToml.load()
    print(f"ghrah-bootloader {__version__}")
    print(f"default={boot.default!r} fallback={boot.fallback!r} last_good={boot.last_good!r}")
    for distro in list_distros():
        layout = DistroLayout(root=resolve_distro_root(distro))
        distro_toml = DistroToml.load(layout.root)
        try:
            commit = "(TODO:git rev-parse HEAD 未实现)"
            current_target = str(layout.current.resolve())
        except (OSError, RuntimeError):
            commit, current_target = "-", "(current 缺失)"
        print(
            f"[{distro}] versions={layout.list_versions() or '-'} "
            f"current={current_target} {commit} "
            f"subject_port={distro_toml.subject_port} webui_port={distro_toml.webui_port}"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghrah-bootloader",
        description="ghrah AgentOS 安装/配置工具与引导器",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    distro = subparsers.add_parser("distro", help="发行版管理")
    distro_sub = distro.add_subparsers(dest="subcommand")
    distro_list = distro_sub.add_parser("list", help="列出发行版与版本")
    distro_list.set_defaults(func=_cmd_distro_list)

    doctor = subparsers.add_parser("doctor", help="诊断安装域布局与配置")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except BootloaderError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
