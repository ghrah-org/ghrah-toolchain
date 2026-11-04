# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""``ghrah ability`` 命令对象：参数面、存储根与目标运行时的薄桥转调。

工具链**不 import ``ghrah.*``**：Ability 的生成、安装、校验等领域实现全部在目标
发行版的 ``ghrah-core`` 里。本模块只负责：

- 注册 ``ability`` 命令组与七个子命令（build/install/list/show/verify/refresh/uninstall）；
- 用 ``ability_bridge`` 解析作者侧/目标侧解释器与安装存储根；
- 把参数拼成目标运行时的命令行并原样透传退出码，不解析身份与版本字符串；
- 检测已作废的用户级存储 ``<GHRAH_HOME>/ability`` 并提示"未使用"（写 stderr）。

命令面与参数映射见 README「管理 Ability 包」一节；本模块自身不实现任何包逻辑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ghrah_toolchain.ability_bridge import (
    TOOLS_CLI,
    AbilityRuntime,
    legacy_user_store_path,
    resolve_build_module,
    resolve_build_runtime,
    resolve_store_runtime,
    run_module,
)
from ghrah_toolchain.errors import AbilityError


class AbilityUsageError(AbilityError):
    """``ability`` 命令的参数用法错误（如 ``--all`` 与 ``@version`` 同时给出）。

    参数错误按约定以退出码 2 结束；领域/校验失败仍走 ``AbilityError`` 的默认退出码 1。
    """

    exit_code = 2


def warn_legacy_store() -> None:
    """检测旧用户级存储并提示"未使用"；只写 stderr，不改变退出码、不污染 stdout。

    存储根已改为发行版级（``<GHRAH_HOME>/<distro>/ability``），旧的
    ``<GHRAH_HOME>/ability`` 不再读写；路径检测由薄桥的
    ``legacy_user_store_path`` 提供（``GHRAH_HOME`` 生效，测试不碰真实用户目录）。
    """
    path = legacy_user_store_path()
    if path is None:
        return
    print(
        f"[提示] 检测到旧的用户级 Ability 存储: {path}；"
        "新工具链不读取、不迁移该目录，如仍需要请按源包重新安装到 <发行版>/ability。",
        file=sys.stderr,
    )


def _store_root(runtime: AbilityRuntime) -> Path:
    """store 命令的目标存储根（薄桥契约保证非 None，这里显式收窄并防御）。"""
    root = runtime.ability_root
    if root is None:
        raise AbilityError("目标运行时解析结果缺少 Ability 存储根，无法定位安装位置")
    return root


# ── handler ──────────────────────────────────────────────────


def _cmd_build(args: argparse.Namespace) -> int:
    """作者侧构建/校验 Ability 包：``<dir> [--check]``（不落安装，无 ``--root``）。"""
    warn_legacy_store()
    runtime = resolve_build_runtime(args.python)
    module = resolve_build_module(runtime.python)
    argv = [str(args.dir)]
    if args.check:
        argv.append("--check")
    # P0 入口 tools.build 只承诺 ``<dir> [--check]``；回退到统一入口时要补 verb。
    if module == TOOLS_CLI:
        argv.insert(0, "build")
    return run_module(runtime.python, module, argv)


def _cmd_store(args: argparse.Namespace) -> int:
    """store 命令统一转调：``tools.cli <verb> ... --root <发行版存储根>``。

    完全透传：不在本地切分/校验 ``<full-name>[@<version>]``，不"猜最新版本"，
    也不改写 core 的退出码。唯一例外是 ``uninstall`` 的 ``--all`` 与 ``@version``
    互斥——这是命令行语义冲突，在本地按参数错误（退出码 2）拒绝。
    """
    warn_legacy_store()
    target = getattr(args, "target", None)
    if getattr(args, "all_versions", False) and target and "@" in target:
        raise AbilityUsageError(
            "参数错误：--all 表示删除该身份的全部版本，不能再给 @version；"
            "请二选一（如 acme.reporting 或 acme.reporting@1.4.0）"
        )
    runtime = resolve_store_runtime(args.python, args.distro)
    root = _store_root(runtime)
    argv = [args.subcommand]
    if target is not None:
        argv.append(target)
    if args.json:
        # 工具链不自己包 JSON：--json 原样交给 core，由 core 在 stdout 输出单个对象。
        argv.append("--json")
    if getattr(args, "all_versions", False):
        argv.append("--all")
    if getattr(args, "yes", False):
        argv.append("--yes")
    argv.extend(["--root", str(root)])
    return run_module(runtime.python, TOOLS_CLI, argv)


# ── 命令行装配 ───────────────────────────────────────────────


def _add_store_options(parser: argparse.ArgumentParser) -> None:
    """store 命令共用的目标运行时选项（``build`` 不接这些选项）。"""
    parser.add_argument(
        "--distro",
        help="目标发行版名（省略时取 boot.toml 默认项的发行版段；不接受 <发行版>/<版本>）",
    )
    parser.add_argument(
        "--python",
        help="显式指定目标解释器；给定时不再回退，缺少所需模块直接报错",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="由目标运行时在 stdout 输出单个 JSON 对象，工具链原样透传",
    )


def register_ability_commands(subparsers) -> None:
    """注册顶层 ``ability`` 命令组及七个子命令。"""
    ability = subparsers.add_parser(
        "ability",
        help="构建、安装、查看与校验 Ability 包（转调目标发行版的 ghrah-core 工具）",
    )
    group = ability.add_subparsers(dest="subcommand", required=True)

    build = group.add_parser("build", help="生成并校验 Ability 包的索引（作者侧操作，不落安装）")
    build.add_argument("dir", type=Path, help="Ability 源包根目录")
    build.add_argument("--check", action="store_true", help="只校验，不写入任何文件")
    build.add_argument(
        "--python", help="显式指定解释器；省略时按作者环境（VIRTUAL_ENV、当前目录 .venv 等）探测"
    )
    build.set_defaults(func=_cmd_build)

    install = group.add_parser("install", help="把 Ability 包安装到目标发行版的存储")
    install.add_argument("target", metavar="<dir|archive>", help="源包目录或包归档文件")
    _add_store_options(install)
    install.set_defaults(func=_cmd_store)

    listing = group.add_parser("list", help="列出目标发行版已安装的 Ability 包")
    _add_store_options(listing)
    listing.set_defaults(func=_cmd_store)

    for name, help_text in (
        ("show", "查看某个 Ability 包的详情（省略版本时列出该身份的已安装版本）"),
        ("verify", "校验某个 Ability 包及安装记录（省略版本时校验该身份的全部版本）"),
        ("refresh", "刷新某个 Ability 包（由 MCP 快照递增 patch；不接受版本号）"),
    ):
        sub = group.add_parser(name, help=help_text)
        sub.add_argument(
            "target", metavar="<full-name>[@<version>]", help="包身份（namespace.name）与可选版本"
        )
        _add_store_options(sub)
        sub.set_defaults(func=_cmd_store)

    uninstall = group.add_parser("uninstall", help="卸载 Ability 包（必须给 @version 或 --all）")
    uninstall.add_argument(
        "target", metavar="<full-name>[@<version>]", help="包身份（namespace.name）与可选版本"
    )
    uninstall.add_argument(
        "--all",
        dest="all_versions",
        action="store_true",
        help="删除该身份的全部版本（与 @version 互斥）",
    )
    uninstall.add_argument(
        "--yes", action="store_true", help="跳过确认；目标运行时未实现确认时该参数无副作用"
    )
    _add_store_options(uninstall)
    uninstall.set_defaults(func=_cmd_store)
