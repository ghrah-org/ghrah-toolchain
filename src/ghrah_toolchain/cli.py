# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ghrah-toolchain 命令面（distro/install/config/boot/stop/runtime/doctor）。

纯性硬约束：本模块及其 import 图**零 ``ghrah.*``、零第三方运行时依赖**
（仅 stdlib）。install / runtime update 经 ``RuntimeRefManager`` 承载
「解析候选 → 门禁 → 快照 → 重指 → sync → 写配置」全链路。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from ghrah_toolchain import __version__
from ghrah_toolchain.adopt import adopt as adopt_legacy
from ghrah_toolchain.adopt import detect_legacy
from ghrah_toolchain.boot import resolve_boot_target, run_boot
from ghrah_toolchain.build import build_artifacts, verify_artifacts
from ghrah_toolchain.errors import RuntimeRefError, ToolchainError
from ghrah_toolchain.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    render_distro_toml,
    resolve_distro_root,
)
from ghrah_toolchain.materialize import read_generation
from ghrah_toolchain.runtime_ref import RefSource, RuntimeRefManager

logger = logging.getLogger(__name__)


def _default_distro(boot: BootToml) -> str:
    if boot.default and "/" in boot.default:
        return boot.default.split("/", 1)[0]
    return ""


def _warn_legacy_data(root) -> None:
    """检测顶层遗留数据 → 仅提示（认领走 ``install --adopt-legacy-data``）。"""
    found = detect_legacy(root)
    if found:
        print(
            f"[提示] 检测到 ~/.ghrah 顶层遗留数据: {', '.join(found)}；"
            "本次安装不会改动它们（认领: install --adopt-legacy-data）"
        )


def _resolve_gate(args: argparse.Namespace, distro_toml: DistroToml) -> str:
    """gate 优先级：``--gate`` > ``[distro.ref].gate`` > basic。"""
    return args.gate or distro_toml.ref_gate or "basic"


def _resolve_fetch(args: argparse.Namespace, distro_toml: DistroToml) -> bool:
    """fetch 优先级：``--fetch``/``--no-fetch`` > ``[distro.ref].fetch_on_update`` > False。"""
    if getattr(args, "fetch", False):
        return True
    if getattr(args, "no_fetch", False):
        return False
    return distro_toml.ref_fetch_on_update


def _short(commit: str | None) -> str:
    return commit[:7] if commit else "-"


# ── distro ───────────────────────────────────────────────────


def _cmd_distro_list(args: argparse.Namespace) -> int:
    """列出发行版与版本/组合代（读 ~/.ghrah 布局 = 读 GPT；附各条目 commit）。"""
    del args  # 未用（预留 --json 等输出控制）
    boot = BootToml.load()
    distros = list_distros()
    if not distros:
        print("（空安装域：尚无发行版）")
        return 0
    entries = {(e.distro, e.version): e for e in RuntimeRefManager().list_entries()}
    print(f"{'distro':<16} {'default':<8} versions")
    for distro in distros:
        layout = DistroLayout(root=resolve_distro_root(distro))
        versions = layout.list_versions()
        gens = layout.list_generations()
        marker = ""
        if boot.default and boot.default.startswith(f"{distro}/"):
            marker = boot.default.split("/", 1)[1]
        current = ""
        try:
            current = layout.current.resolve().name
        except OSError, RuntimeError:
            current = "-"
        rendered = ", ".join(
            f"{v}@{_short(entries.get((distro, v)).commit if entries.get((distro, v)) else None)}"
            for v in versions
        )
        print(f"{distro:<16} {marker or '-':<8} {rendered or '-'} (current: {current})")
        if gens:
            gen_rows = []
            for g in gens:
                e = entries.get((distro, g))
                if e is not None:
                    gen_rows.append(e.detail)
                else:
                    gen_rows.append(g)
            print(f"{'':<16} {'':<8} generations: {'; '.join(gen_rows)}")
    return 0


# ── install ─────────────────────────────────────────────────


def _cmd_install(args: argparse.Namespace) -> int:
    """安装发行版版本：建 distro.toml（首装）→ 门禁 → 快照 → 重指 → 写配置。"""
    distro = _instance_name(args)
    root = ghrah_root()
    distro_root = resolve_distro_root(distro, root)
    distro_toml = DistroToml.load(distro_root)
    repo_arg = args.repo or os.environ.get("GHRAH_MONOREPO_ROOT")
    if not distro_root.is_dir() or not distro_toml.repo_path:
        if not repo_arg:
            raise ToolchainError("首次安装需要 --repo 或 env GHRAH_MONOREPO_ROOT 指定运行时仓路径")
        distro_root.mkdir(parents=True, exist_ok=True)
        rendered = render_distro_toml(
            name=distro,
            repo_path=repo_arg,
            source=args.ref or "latest",
            gate=args.gate or "basic",
            subject_port=4112,
            webui_port=5173,
            data_root="",
            snapshot_keep=3,
        )
        (distro_root / "distro.toml").write_text(rendered, encoding="utf-8")
        distro_toml = DistroToml.load(distro_root)
        print(f"已创建发行版声明: {distro_root / 'distro.toml'}")
    _warn_legacy_data(root)
    manager = RuntimeRefManager(root=root, repo=args.repo)
    entry = manager.ensure(
        distro=distro,
        update=False,
        source=args.ref,
        version=args.version,
        gate=_resolve_gate(args, distro_toml),
    )
    print(
        f"已安装并重指 current → {entry.label}（commit {entry.commit}，"
        f"组合代 {entry.version} 已物化，env 就位）"
    )
    if args.adopt_legacy_data:
        report = adopt_legacy(DistroLayout(root=distro_root), distro_toml, subject_port=None)
        if not report.noop:
            print(
                f"认领完成：数据面 {len(report.adopted)} 项 → 数据盘；"
                f"归档 {len(report.archived)} 项 → {report.legacy_dir}"
            )
            print("备份保留在 legacy/…/backup/，确认系统正常后可手动删除 legacy/ 目录")
    return 0


# ── runtime ──────────────────────────────────────────────────


def _cmd_runtime_status(args: argparse.Namespace) -> int:
    """引导菜单状态：current / 配置 source / 候选 commit（dry）/ 落后 / last_good。"""
    boot = BootToml.load()
    distro = args.distro or _default_distro(boot)
    if not distro:
        print("（尚未安装任何发行版：先 ghrah-toolchain install --distro <n> ...）")
        return 0
    distro_root = resolve_distro_root(distro)
    layout = DistroLayout(root=distro_root)
    distro_toml = DistroToml.load(distro_root)
    manager = RuntimeRefManager()
    try:
        current = manager.resolve(distro)
    except RuntimeRefError as e:
        print(f"[{distro}] 无 current（{e}）")
        current = None
    if current is not None:
        print(f"[{distro}] current = {current.label}（commit {current.commit}）")
    entries = [e for e in manager.list_entries() if e.distro == distro]
    print(
        f"[{distro}] versions = "
        + (
            ", ".join(f"{e.version}@{_short(e.commit)}" for e in entries if e.kind == "version")
            or "（无）"
        )
    )
    gens = [e for e in entries if e.kind == "generation"]
    if gens or layout.list_generations():
        print(f"[{distro}] generations = " + (", ".join(e.detail for e in gens) or "（无）"))
    print(
        f"[{distro}] 配置 source = {distro_toml.ref_source} gate = {distro_toml.ref_gate} "
        f"fetch_on_update = {distro_toml.ref_fetch_on_update}"
    )
    try:
        repo = manager._resolve_repo(distro_root, distro_toml)
        candidate = manager._resolve_candidate(
            RefSource.parse(distro_toml.ref_source), fetch=False, repo=repo
        )
        if current is not None and current.commit == candidate:
            print(f"[{distro}] 候选 commit = {candidate}（已是最新，无 fetch）")
        else:
            print(f"[{distro}] 候选 commit = {candidate}（落后；runtime update 前移）")
    except RuntimeRefError as e:
        print(f"[{distro}] 候选解析失败: {e}")
    lg = layout.last_good
    lg_ver = "-"
    if lg.is_symlink() or lg.exists():
        try:
            lg_ver = lg.resolve().name
        except OSError, RuntimeError:
            lg_ver = "-"
    print(f"[{distro}] last_good = {boot.last_good!r}（符号链接 → {lg_ver}）")
    return 0


def _cmd_runtime_update(args: argparse.Namespace) -> int:
    """前移引导菜单：``ensure(update=True)``（门禁 → 快照 → 重指 → 按需 sync）。"""
    boot = BootToml.load()
    distro = args.distro or _default_distro(boot)
    if not distro:
        raise ToolchainError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
    distro_toml = DistroToml.load(resolve_distro_root(distro))
    manager = RuntimeRefManager(repo=args.repo)
    entry = manager.ensure(
        distro=distro,
        update=True,
        fetch=_resolve_fetch(args, distro_toml),
        source=args.source,
        gate=_resolve_gate(args, distro_toml),
    )
    print(f"已前移 current → {entry.label}（commit {entry.commit}）")
    return 0


# ── build ────────────────────────────────────────────────────


def _cmd_build(args: argparse.Namespace) -> int:
    """构建发行物：源仓候选 commit → wheels/tarballs/observer-web/manifest（不动安装域）。"""
    repo_arg = args.repo or os.environ.get("GHRAH_MONOREPO_ROOT")
    if not repo_arg:
        raise ToolchainError("构建需要 --repo 或 env GHRAH_MONOREPO_ROOT 指定运行时仓路径")
    repo = Path(repo_arg).expanduser()
    if not repo.is_dir():
        raise ToolchainError(f"运行时仓路径不存在: {repo}")
    source = RefSource.parse(args.ref)
    commit = RuntimeRefManager()._resolve_candidate(source, fetch=False, repo=repo)  # noqa: SLF001
    out_dir = Path(args.out).expanduser() if args.out else repo / "dist" / "toolchain" / commit[:7]
    result = build_artifacts(repo, commit, out_dir=out_dir)
    print(f"构建完成: {result.out_dir}")
    print(f"source_commit = {result.source_commit}")
    print(f"wheels: {', '.join(result.wheels)}")
    print(f"tarballs: {', '.join(result.tarballs)}")
    print(f"pins: {result.pins_file}")
    if args.no_verify:
        print("（已跳过验证）")
    else:
        verify_artifacts(result.out_dir)
        print("产物验证通过（import 冒烟 + ghrah-subject-server 入口）")
    return 0


# ── boot / stop / doctor ─────────────────────────────────────


def _cmd_boot(args: argparse.Namespace) -> int:
    """引导：解析目标 →（救援：恢复快照/重指）→ spawn OS（+ 前端）→ 监督。"""
    if args.restore_snapshot and args.ref != "last-good":
        raise ToolchainError("--restore-snapshot 需配合 --ref last-good（数据回滚救援）")
    if args.no_webui and (args.webui_dev or args.webui_dev_dir):
        raise ToolchainError("--no-webui 与 --webui-dev/--webui-dev-dir 互斥")
    from ghrah_toolchain.instances import start_instance

    name = _instance_name(args)
    if not args.foreground:
        if args.ref or args.restore_snapshot or args.webui_dev or args.webui_dev_dir:
            raise ToolchainError("救援或前端开发模式需显式使用 --foreground")
        resolve_boot_target(name, args.version)
        state = start_instance(name, no_webui=args.no_webui)
        _print_state(state, args.json)
        return 0
    if not (args.ref or args.restore_snapshot or args.webui_dev or args.webui_dev_dir):
        result = start_instance(name, no_webui=args.no_webui, foreground=True)
        if isinstance(result, dict):
            _print_state(result, args.json)
            return 0
        return result
    target = resolve_boot_target(name, args.version, ref=args.ref)
    return asyncio.run(
        run_boot(
            target,
            no_webui=args.no_webui,
            no_browser=args.no_browser,
            restore_snapshot=args.restore_snapshot,
            yes=args.yes,
            webui_dev=args.webui_dev,
            webui_dev_dir_arg=args.webui_dev_dir,
        )
    )


def _cmd_stop(args: argparse.Namespace) -> int:
    """停止：向运行中的 boot 会话转发 SIGTERM（pidfile 定位 + cmdline 护栏）。"""
    from ghrah_toolchain.instances import stop_instance

    name = _instance_name(args)
    code = stop_instance(name, timeout=args.timeout)
    if code == 0:
        print(f"[{name}] 已停止")
    return code


def _cmd_dev(args: argparse.Namespace) -> int:
    """本地开发快环：status / up / down / reset（只读写 ``<distro>/.dev/``）。"""
    from ghrah_toolchain.dev import run_dev_command

    return run_dev_command(args)


def _instance_name(args: argparse.Namespace) -> str:
    if args.name and args.distro and args.name != args.distro:
        raise ToolchainError("实例位置参数与 --distro 冲突")
    name = args.name or args.distro or _default_distro(BootToml.load())
    if not name:
        raise ToolchainError("未指定实例名称，且未配置默认实例")
    resolve_distro_root(name)
    return name


def _print_state(state: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"schema": 1, "scope": "user", "data": state}, ensure_ascii=False))
    else:
        print(f"[{state['name']}] {state['state']} (pid={state.get('pid', '-')})")
        if state.get("log"):
            print(f"日志: {state['log']}；使用 ghrah logs {state['name']} 查看")


def _cmd_list(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import list_instances

    states = list_instances()
    if args.json:
        print(json.dumps({"schema": 1, "scope": "user", "data": states}, ensure_ascii=False))
    else:
        for state in states:
            print(f"{state['name']}\t{state['state']}\t{state.get('pid', '-')}")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import read_logs

    if args.tail < 0:
        raise ToolchainError("--tail 不能为负数")
    print(read_logs(_instance_name(args), tail=args.tail), end="")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    from ghrah_toolchain.manifest import load_manifest

    manifest = load_manifest(args.manifest_path)
    if args.json or args.command == "metadata":
        print(
            json.dumps(
                {
                    "schema": 1,
                    "scope": "workspace",
                    "data": manifest.metadata(),
                    "diagnostics": [],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"静态校验通过: {manifest.path}")
        print("未启动组件，未应用运行时绑定或 Agent 授权。")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """诊断：安装域布局、boot.toml、各发行版 current commit / last_good / 落后。"""
    del args
    boot = BootToml.load()
    print(f"ghrah-toolchain {__version__}")
    print(f"default={boot.default!r} fallback={boot.fallback!r} last_good={boot.last_good!r}")
    manager = RuntimeRefManager()
    for distro in list_distros():
        layout = DistroLayout(root=resolve_distro_root(distro))
        distro_toml = DistroToml.load(layout.root)
        commit: str | None = None
        try:
            tree = layout.current.resolve()
            current_target = str(tree)
            info = read_generation(tree)
            commit = info.base_commit if info is not None else manager._tree_commit(tree)
        except OSError, RuntimeError:
            current_target = "(current 缺失)"
        behind = "候选不可解析"
        try:
            repo = manager._resolve_repo(layout.root, distro_toml)
            candidate = manager._resolve_candidate(
                RefSource.parse(distro_toml.ref_source), fetch=False, repo=repo
            )
            behind = (
                "最新"
                if (commit is not None and candidate == commit)
                else (f"落后（候选 {candidate[:7]}）")
            )
        except RuntimeRefError:
            pass
        # 当前代缺身份（无 BOOTSTRAP）= 异常态
        identity_note = ""
        if commit is None:
            identity_note = "（当前代缺 BOOTSTRAP 身份：异常态，建议重新 install）"
        print(
            f"[{distro}] versions={layout.list_versions() or '-'} "
            f"generations={layout.list_generations() or '-'} "
            f"current={current_target} commit={_short(commit)}{identity_note} "
            f"落后情况={behind} "
            f"subject_port={distro_toml.subject_port} webui_port={distro_toml.webui_port}"
        )
    return 0


# ── 命令行装配 ───────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghrah",
        description="ghrah 开发、组合校验与具名实例管理工具链",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG 级日志（默认 WARNING；置于子命令之前，如 ghrah -v dev up）",
    )
    subparsers = parser.add_subparsers(dest="command")

    for command in ("check", "metadata"):
        manifest = subparsers.add_parser(command, help="离线校验或查询源码组合清单")
        manifest.add_argument("--manifest-path", type=Path)
        manifest.add_argument("--json", action="store_true")
        manifest.set_defaults(func=_cmd_manifest)

    listing = subparsers.add_parser("list", aliases=["ps"], help="枚举已安装具名实例及状态")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=_cmd_list)

    logs = subparsers.add_parser("logs", help="查看实例最近一次启动的日志")
    logs.add_argument("name", nargs="?")
    logs.add_argument("--distro")
    logs.add_argument("--tail", type=int, default=100)
    logs.set_defaults(func=_cmd_logs)

    distro = subparsers.add_parser("distro", help="发行版管理")
    distro_sub = distro.add_subparsers(dest="subcommand")
    distro_list = distro_sub.add_parser("list", help="列出发行版与版本")
    distro_list.set_defaults(func=_cmd_distro_list)

    install = subparsers.add_parser(
        "install", aliases=["create"], help="创建已安装具名实例，不启动"
    )
    install.add_argument("name", nargs="?", help="实例名称")
    install.add_argument("--distro", help="实例名称（兼容参数）")
    install.add_argument("--version", help="版本名（缺省取候选提交日期 %%Y.%%m）")
    install.add_argument("--ref", help="签出源（latest | branch:<n> | commit:<sha>）")
    install.add_argument("--gate", help="门禁级别（basic | full | data）")
    install.add_argument("--repo", help="运行时仓路径（覆盖 env 与 distro.toml）")
    install.add_argument(
        "--adopt-legacy-data",
        action="store_true",
        help="认领 ~/.ghrah 顶层遗留数据（一次性：移动五项进数据盘，其余归档 legacy/）",
    )
    install.set_defaults(func=_cmd_install)

    runtime = subparsers.add_parser("runtime", help="引导菜单管理（status/update）")
    runtime_sub = runtime.add_subparsers(dest="subcommand")
    runtime_status = runtime_sub.add_parser("status", help="显示 current/候选/落后/last_good")
    runtime_status.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    runtime_status.set_defaults(func=_cmd_runtime_status)
    runtime_update = runtime_sub.add_parser("update", help="前移到候选（门禁→快照→重指）")
    runtime_update.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    runtime_update.add_argument("--source", help="签出源（latest | branch:<n> | commit:<sha>）")
    runtime_update.add_argument("--gate", help="门禁级别（basic | full | data）")
    runtime_update.add_argument("--repo", help="运行时仓路径（覆盖 env 与 distro.toml）")
    fetch_group = runtime_update.add_mutually_exclusive_group()
    fetch_group.add_argument("--fetch", action="store_true", help="解析候选前 git fetch --all")
    fetch_group.add_argument("--no-fetch", action="store_true", help="禁止 fetch（默认）")
    runtime_update.set_defaults(func=_cmd_runtime_update)

    build = subparsers.add_parser("build", help="从源仓构建发行物（wheels/tarballs/manifest）")
    build.add_argument("--repo", help="运行时仓路径（覆盖 env GHRAH_MONOREPO_ROOT）")
    build.add_argument("--ref", help="签出源（latest | branch:<n> | commit:<sha>），缺省 latest")
    build.add_argument("--out", help="产物目录（缺省 <repo>/dist/toolchain/<short7>/）")
    build.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过构建后验证（缺省构建成功即跑 import 冒烟 + 入口校验）",
    )
    build.set_defaults(func=_cmd_build)

    boot = subparsers.add_parser(
        "start", aliases=["boot"], help="启动已安装实例并立即返回，输出写入实例日志"
    )
    boot.add_argument("name", nargs="?", help="已安装实例名")
    boot.add_argument("--foreground", action="store_true", help="显式在当前终端运行和监督")
    boot.add_argument("--json", action="store_true")
    boot.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    boot.add_argument("--version", help="期望版本（须与 current 一致；切换走 install/update）")
    boot.add_argument("--ref", choices=["last-good"], help="救援入口：引导已知良好版本")
    boot.add_argument(
        "--restore-snapshot",
        action="store_true",
        help="恢复最近数据快照并重指 current 到 last-good（损失快照后写入窗口）",
    )
    boot.add_argument("--yes", action="store_true", help="跳过恢复前二次确认（非交互必须）")
    boot.add_argument("--no-webui", action="store_true", help="不启动前端（也无浏览器）")
    boot.add_argument(
        "--webui-dev",
        action="store_true",
        help="前端走 pnpm dev 开发域源码树（默认静态 serve 基座产物 dist）",
    )
    boot.add_argument(
        "--webui-dev-dir",
        help="开发域源码树的 observer-web 包目录（缺省经 distro.toml [distro.repo].path 推导）",
    )
    boot.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    boot.set_defaults(func=_cmd_boot)

    stop = subparsers.add_parser("stop", help="幂等停止实例及其受管子进程")
    stop.add_argument("name", nargs="?")
    stop.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    stop.add_argument("--timeout", type=float, default=15.0)
    stop.set_defaults(func=_cmd_stop)

    doctor = subparsers.add_parser("doctor", help="诊断安装域布局与配置")
    doctor.set_defaults(func=_cmd_doctor)

    dev = subparsers.add_parser("dev", help="本地开发快环（工作树增量构建 + 本地起服）")
    dev_sub = dev.add_subparsers(dest="subcommand")
    dev_status = dev_sub.add_parser("status", help="对比工作树与上次 dev 构建，报告变更")
    dev_status.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_status.set_defaults(func=_cmd_dev)
    dev_up = dev_sub.add_parser("up", help="增量构建并本地起服（接管正式端口）")
    dev_up.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_up.add_argument("--no-restart", action="store_true", help="只构建/升级，不启动服务")
    dev_up.add_argument("--full", action="store_true", help="强制全量重建（忽略增量指纹）")
    dev_up.set_defaults(func=_cmd_dev)
    dev_down = dev_sub.add_parser("down", help="停止运行中的 dev 会话")
    dev_down.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_down.set_defaults(func=_cmd_dev)
    dev_reset = dev_sub.add_parser("reset", help="清空 .dev 命名空间，回到未 dev 状态")
    dev_reset.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_reset.set_defaults(func=_cmd_dev)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except ToolchainError as e:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema": 1,
                        "scope": "workspace" if args.command in {"check", "metadata"} else "user",
                        "data": None,
                        "diagnostics": [str(e)],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"[错误] {e}", file=sys.stderr)
        return getattr(e, "exit_code", 1)


if __name__ == "__main__":
    sys.exit(main())
