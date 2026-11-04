# SPDX-License-Identifier: Apache-2.0
"""已安装实例和 CLI 自身的用户入口。"""

from __future__ import annotations


def _target(parser):
    parser.add_argument("name", nargs="?", help="实例名（省略时使用已配置的默认实例）")
    parser.add_argument("--distro", help="实例名（兼容参数）")


def _json(parser):
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出，便于脚本读取")


def _install(parser):
    _target(parser)
    _json(parser)
    parser.add_argument("--repo", "--from", dest="repo", help="本地 Git 路径或 HTTPS/SSH 仓库 URL")
    parser.add_argument(
        "--ref", help="源码版本：latest、branch:<名称>、tag:<名称> 或 commit:<提交编号>"
    )
    parser.add_argument("--version", help="安装版本的名称（默认使用所选源码提交的月份）")
    parser.add_argument("--gate", help="运行环境检查级别（目前仅支持 basic，检查环境是否完整）")
    parser.add_argument("--subject-port", type=int, default=4112, help="首装后端端口")
    parser.add_argument("--webui-port", type=int, default=5173, help="首装前端端口")
    parser.add_argument(
        "--adopt-legacy-data", action="store_true", help="将安装目录顶层的旧数据迁入本实例"
    )


def _update(parser):
    _target(parser)
    parser.add_argument("--repo", help="本次更新使用的本地 Git 路径或远程 URL")
    parser.add_argument(
        "--source", "--ref", dest="source", help="本次更新使用的源码版本，不改变已保存的更新来源"
    )
    parser.add_argument("--gate", help="运行环境检查级别（目前仅支持 basic，检查环境是否完整）")
    parser.add_argument(
        "--check", action="store_true", help="只比较当前版本与本地记录中的所选源码提交，不执行更新"
    )
    _json(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--fetch", action="store_true", help="先同步远程源码（git fetch）；同步失败则停止更新"
    )
    group.add_argument("--no-fetch", action="store_true", help="本次不联网同步源码，使用本地记录")


def _start(parser):
    _target(parser)
    _json(parser)
    parser.add_argument("--foreground", action="store_true", help="在当前终端运行并显示日志")
    parser.add_argument("--version", help="要启动的版本，必须与当前已选用的安装版本一致")
    parser.add_argument(
        "--ref", choices=["last-good"], help="使用上次成功启动的版本（需加 --foreground）"
    )
    parser.add_argument(
        "--restore-snapshot", action="store_true", help="恢复最近的数据备份，会丢失备份之后的改动"
    )
    parser.add_argument("--yes", action="store_true", help="确认恢复数据备份，不再询问")
    parser.add_argument("--no-webui", action="store_true", help="只启动后端，不提供前端页面")
    parser.add_argument(
        "--webui-dev", action="store_true", help="使用前端开发服务器（需加 --foreground）"
    )
    parser.add_argument("--webui-dev-dir", help="前端 observer-web 源码目录（需加 --foreground）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")


def _stop(parser):
    _target(parser)
    parser.add_argument("--timeout", type=float, default=15.0, help="等待停止的秒数（默认 15）")


def _restart(parser):
    _stop(parser)
    _json(parser)
    parser.add_argument("--no-webui", action="store_true", help="只启动后端，不提供前端页面")


def _status(parser):
    _target(parser)
    _json(parser)


def _logs(parser):
    _target(parser)
    parser.add_argument(
        "--tail", type=int, help="显示日志末尾的行数（默认读取 self.log_tail，初始为 100）"
    )
    parser.add_argument(
        "--follow", "-f", action="store_true", help="持续显示新日志；Ctrl+C 结束查看，服务继续运行"
    )


def _config(parser, *, scope, handler):
    subs = parser.add_subparsers(dest="config_action", required=True)
    for action in ("get", "set", "show"):
        sub = subs.add_parser(action)
        if scope == "distro":
            _target(sub)
        if action != "show":
            sub.add_argument("key", help="配置项名称，例如 runtime.subject_port 或 self.output")
        if action == "set":
            sub.add_argument("value", help="要保存的值；整数直接填写，布尔值使用 true/false")
        _json(sub)
        sub.set_defaults(func=handler, config_scope=scope)


def register_user_commands(subparsers, handlers):
    distro = subparsers.add_parser("distro", help="已安装实例：安装、更新、启停、配置、状态和日志")
    grouped = distro.add_subparsers(dest="subcommand", required=True)
    commands = (
        ("install", ["create"], _install, "install", "按指定名称安装实例，完成后不自动启动"),
        ("update", [], _update, "update", "更新已停止的实例；先检查运行环境并备份已有数据"),
        ("start", ["boot"], _start, "start", "后台启动并立即返回"),
        ("stop", [], _stop, "stop", "停止实例及其相关进程；已停止时可重复执行"),
        ("restart", [], _restart, "restart", "停止成功后重新后台启动"),
        ("status", [], _status, "status", "查看安装版本、运行状态和访问地址"),
        ("logs", [], _logs, "logs", "查看最近一次启动的日志，或持续显示新日志"),
        ("list", ["ps"], _json, "list", "列出已安装实例的版本和运行状态"),
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
    default = grouped.add_parser("default", help="设置省略实例名时使用的默认实例")
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

    own = subparsers.add_parser("self", help="查看工具链信息、检查安装情况和修改默认设置")
    own_subs = own.add_subparsers(dest="self_command", required=True)
    status = own_subs.add_parser("status")
    _json(status)
    status.set_defaults(func=handlers["self_status"])
    _config(own_subs.add_parser("config"), scope="self", handler=handlers["config"])
    own_subs.add_parser("doctor").set_defaults(func=handlers["doctor"])
    subparsers.add_parser("doctor", help="检查安装目录和配置（兼容旧命令）").set_defaults(
        func=handlers["doctor"]
    )
