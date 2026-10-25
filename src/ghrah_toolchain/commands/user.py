# SPDX-License-Identifier: Apache-2.0
"""已安装实例和 CLI 自身的用户入口。"""

from __future__ import annotations


def _target(parser):
    parser.add_argument("name", nargs="?", help="实例名（省略使用明确配置的默认实例）")
    parser.add_argument("--distro", help="实例名（兼容参数）")


def _json(parser):
    parser.add_argument("--json", action="store_true")


def _install(parser):
    _target(parser)
    _json(parser)
    parser.add_argument("--repo", "--from", dest="repo", help="本地运行时 Git 源仓路径")
    parser.add_argument("--ref", help="latest | branch:<name> | commit:<sha>")
    parser.add_argument("--version", help="版本名（默认取候选提交月份）")
    parser.add_argument("--gate", help="目前只支持 basic")
    parser.add_argument("--subject-port", type=int, default=4112, help="首装后端端口")
    parser.add_argument("--webui-port", type=int, default=5173, help="首装前端端口")
    parser.add_argument("--adopt-legacy-data", action="store_true", help="显式认领顶层遗留数据")


def _update(parser):
    _target(parser)
    parser.add_argument("--repo", help="本次更新使用的本地源仓路径")
    parser.add_argument("--source", "--ref", dest="source", help="本次候选来源，不改变跟踪配置")
    parser.add_argument("--gate", help="目前只支持 basic")
    parser.add_argument("--check", action="store_true", help="只读比较本地候选，不构建或切换")
    _json(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fetch", action="store_true", help="先 git fetch --all；失败终止更新")
    group.add_argument("--no-fetch", action="store_true", help="本次不 fetch")


def _start(parser):
    _target(parser)
    _json(parser)
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--version", help="期望版本，必须与 current 一致")
    parser.add_argument("--ref", choices=["last-good"], help="显式前台救援入口")
    parser.add_argument("--restore-snapshot", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-webui", action="store_true")
    parser.add_argument("--webui-dev", action="store_true")
    parser.add_argument("--webui-dev-dir")
    parser.add_argument("--no-browser", action="store_true")


def _stop(parser):
    _target(parser)
    parser.add_argument("--timeout", type=float, default=15.0)


def _restart(parser):
    _stop(parser)
    _json(parser)
    parser.add_argument("--no-webui", action="store_true")


def _status(parser):
    _target(parser)
    _json(parser)


def _logs(parser):
    _target(parser)
    parser.add_argument("--tail", type=int, help="默认使用 self.log_tail（初值 100）")
    parser.add_argument("--follow", "-f", action="store_true", help="持续读取；Ctrl+C 仅退出读取")


def _config(parser, *, scope, handler):
    subs = parser.add_subparsers(dest="config_action", required=True)
    for action in ("get", "set", "show"):
        sub = subs.add_parser(action)
        if scope == "distro":
            _target(sub)
        if action != "show":
            sub.add_argument("key", help="受支持的点分配置键")
        if action == "set":
            sub.add_argument("value", help="字符串原值，或 TOML 整数／布尔值")
        _json(sub)
        sub.set_defaults(func=handler, config_scope=scope)


def register_user_commands(subparsers, handlers):
    distro = subparsers.add_parser("distro", help="已安装实例：安装、更新、启停、配置、状态和日志")
    grouped = distro.add_subparsers(dest="subcommand", required=True)
    commands = (
        ("install", ["create"], _install, "install", "安装具名实例，不自动启动"),
        ("update", [], _update, "update", "更新停止的实例；自动执行门禁和快照"),
        ("start", ["boot"], _start, "start", "后台启动并立即返回"),
        ("stop", [], _stop, "stop", "幂等停止实例及其子进程"),
        ("restart", [], _restart, "restart", "停止成功后重新后台启动"),
        ("status", [], _status, "status", "只读查询安装、进程和配置地址"),
        ("logs", [], _logs, "logs", "读取或跟随最近会话日志"),
        ("list", ["ps"], _json, "list", "枚举安装版本和运行状态"),
    )
    for name, aliases, configure, handler, help_text in commands:
        for parent in (grouped, subparsers):
            parser = parent.add_parser(name, aliases=aliases, help=help_text)
            configure(parser)
            parser.set_defaults(func=handlers[handler])
    for parent in (grouped, subparsers):
        _config(
            parent.add_parser("config", help="实例启动设置（不管理 Agent 业务配置）"),
            scope="distro",
            handler=handlers["config"],
        )
    default = grouped.add_parser("default", help="显式选择默认已安装实例")
    _target(default)
    default.set_defaults(func=handlers["default"])

    runtime = subparsers.add_parser("runtime", help="版本管理兼容入口")
    runtime_subs = runtime.add_subparsers(dest="subcommand", required=True)
    status = runtime_subs.add_parser("status")
    _target(status)
    status.set_defaults(func=handlers["runtime_status"])
    update = runtime_subs.add_parser("update")
    _update(update)
    update.set_defaults(func=handlers["update"])

    own = subparsers.add_parser("self", help="CLI 自身身份、诊断与偏好")
    own_subs = own.add_subparsers(dest="self_command", required=True)
    status = own_subs.add_parser("status")
    _json(status)
    status.set_defaults(func=handlers["self_status"])
    _config(own_subs.add_parser("config"), scope="self", handler=handlers["config"])
    own_subs.add_parser("doctor").set_defaults(func=handlers["doctor"])
    subparsers.add_parser("doctor", help="诊断安装域与配置（兼容入口）").set_defaults(
        func=handlers["doctor"]
    )
