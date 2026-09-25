# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ghrah-bootloader 命令面（distro/install/config/boot/stop/runtime/doctor）。

纯性硬约束：本模块及其 import 图**零 ``ghrah.*``、零第三方运行时依赖**
（仅 stdlib）。install / runtime update 经 ``RuntimeRefManager`` 承载
「解析候选 → 门禁 → 快照 → 重指 → sync → 写配置」全链路。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from ghrah_bootloader import __version__
from ghrah_bootloader.adopt import adopt as adopt_legacy
from ghrah_bootloader.adopt import detect_legacy
from ghrah_bootloader.boot import resolve_boot_target, run_boot, stop_boot_session
from ghrah_bootloader.build import build_artifacts, verify_artifacts
from ghrah_bootloader.errors import BootloaderError, RuntimeRefError
from ghrah_bootloader.layout import (
    BootToml,
    DistroLayout,
    DistroToml,
    ghrah_root,
    list_distros,
    render_distro_toml,
    resolve_distro_root,
)
from ghrah_bootloader.runtime_ref import RefSource, RuntimeRefManager

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
    """列出发行版与版本（读 ~/.ghrah 布局 = 读 GPT；附各版本 commit）。"""
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
        marker = ""
        if boot.default and boot.default.startswith(f"{distro}/"):
            marker = boot.default.split("/", 1)[1]
        current = ""
        try:
            current = layout.current.resolve().name
        except (OSError, RuntimeError):
            current = "-"
        rendered = ", ".join(
            f"{v}@{_short(entries.get((distro, v)).commit if entries.get((distro, v)) else None)}"
            for v in versions
        )
        print(f"{distro:<16} {marker or '-':<8} {rendered or '-'} (current: {current})")
    return 0


# ── install ─────────────────────────────────────────────────


def _cmd_install(args: argparse.Namespace) -> int:
    """安装发行版版本：建 distro.toml（首装）→ 门禁 → 快照 → 重指 → 写配置。"""
    distro = args.distro
    root = ghrah_root()
    distro_root = resolve_distro_root(distro, root)
    distro_toml = DistroToml.load(distro_root)
    repo_arg = args.repo or os.environ.get("GHRAH_MONOREPO_ROOT")
    if not distro_root.is_dir() or not distro_toml.repo_path:
        if not repo_arg:
            raise BootloaderError("首次安装需要 --repo 或 env GHRAH_MONOREPO_ROOT 指定运行时仓路径")
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
    form = "产物树（packages 模式）" if distro_toml.install_mode == "packages" else "worktree"
    print(f"已安装并重指 current → {entry.label}（commit {entry.commit}，{form}）")
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
        print("（尚未安装任何发行版：先 ghrah-bootloader install --distro <n> ...）")
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
        + (", ".join(f"{e.version}@{_short(e.commit)}" for e in entries) or "（无）")
    )
    print(
        f"[{distro}] 配置 source = {distro_toml.ref_source} gate = {distro_toml.ref_gate} "
        f"install_mode = {distro_toml.install_mode} "
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
        except (OSError, RuntimeError):
            lg_ver = "-"
    print(f"[{distro}] last_good = {boot.last_good!r}（符号链接 → {lg_ver}）")
    return 0


def _cmd_runtime_update(args: argparse.Namespace) -> int:
    """前移引导菜单：``ensure(update=True)``（门禁 → 快照 → 重指 → 按需 sync）。"""
    boot = BootToml.load()
    distro = args.distro or _default_distro(boot)
    if not distro:
        raise BootloaderError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
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
        raise BootloaderError("构建需要 --repo 或 env GHRAH_MONOREPO_ROOT 指定运行时仓路径")
    repo = Path(repo_arg).expanduser()
    if not repo.is_dir():
        raise BootloaderError(f"运行时仓路径不存在: {repo}")
    source = RefSource.parse(args.ref)
    commit = RuntimeRefManager()._resolve_candidate(source, fetch=False, repo=repo)  # noqa: SLF001
    out_dir = Path(args.out).expanduser() if args.out else repo / "dist" / "bootloader" / commit[:7]
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
        raise BootloaderError("--restore-snapshot 需配合 --ref last-good（数据回滚救援）")
    target = resolve_boot_target(args.distro, args.version, ref=args.ref)
    return asyncio.run(
        run_boot(
            target,
            no_webui=args.no_webui,
            no_browser=args.no_browser,
            restore_snapshot=args.restore_snapshot,
            yes=args.yes,
        )
    )


def _cmd_stop(args: argparse.Namespace) -> int:
    """停止：向运行中的 boot 会话转发 SIGTERM（pidfile 定位 + cmdline 护栏）。"""
    boot = BootToml.load()
    distro = args.distro or _default_distro(boot)
    if not distro:
        raise BootloaderError("未指定发行版：提供 --distro 或在 boot.toml 设置 default")
    return stop_boot_session(distro)


def _cmd_doctor(args: argparse.Namespace) -> int:
    """诊断：安装域布局、boot.toml、各发行版 current commit / last_good / 落后。"""
    del args
    boot = BootToml.load()
    print(f"ghrah-bootloader {__version__}")
    print(f"default={boot.default!r} fallback={boot.fallback!r} last_good={boot.last_good!r}")
    manager = RuntimeRefManager()
    for distro in list_distros():
        layout = DistroLayout(root=resolve_distro_root(distro))
        distro_toml = DistroToml.load(layout.root)
        commit: str | None = None
        try:
            tree = layout.current.resolve()
            current_target = str(tree)
            commit = manager._tree_commit(tree)
        except (OSError, RuntimeError):
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
        # 版本树缺身份（无 BOOTSTRAP 且非 git 树）= 异常态或旧形态残树
        identity_note = ""
        if commit is None and layout.list_versions():
            identity_note = "（版本树缺 BOOTSTRAP/git 身份：旧 worktree 形态或异常态）"
        print(
            f"[{distro}] versions={layout.list_versions() or '-'} "
            f"current={current_target} commit={_short(commit)}{identity_note} "
            f"install_mode={distro_toml.install_mode} "
            f"落后情况={behind} "
            f"subject_port={distro_toml.subject_port} webui_port={distro_toml.webui_port}"
        )
    return 0


# ── 命令行装配 ───────────────────────────────────────────────


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

    install = subparsers.add_parser("install", help="安装一个发行版版本")
    install.add_argument("--distro", required=True, help="发行版名")
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
    build.add_argument("--out", help="产物目录（缺省 <repo>/dist/bootloader/<short7>/）")
    build.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过构建后验证（缺省构建成功即跑 import 冒烟 + 入口校验）",
    )
    build.set_defaults(func=_cmd_build)

    boot = subparsers.add_parser(
        "boot", aliases=["start", "webui"], help="引导发行版（spawn OS + 前端子进程）"
    )
    boot.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    boot.add_argument("--version", help="期望版本（须与 current 一致；切换走 install/update）")
    boot.add_argument("--ref", choices=["last-good"], help="救援入口：引导已知良好版本")
    boot.add_argument(
        "--restore-snapshot",
        action="store_true",
        help="恢复最近数据快照并重指 current 到 last-good（损失快照后写入窗口）",
    )
    boot.add_argument("--yes", action="store_true", help="跳过恢复前二次确认（非交互必须）")
    boot.add_argument("--no-webui", action="store_true", help="不启动前端子进程")
    boot.add_argument("--no-browser", action="store_true", help="不打开浏览器")
    boot.set_defaults(func=_cmd_boot)

    stop = subparsers.add_parser("stop", help="停止运行中的引导栈")
    stop.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    stop.set_defaults(func=_cmd_stop)

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
