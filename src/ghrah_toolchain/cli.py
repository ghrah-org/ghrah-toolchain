# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ghrah 命令入口；工具链通过子进程与数据契约管理独立的运行时。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
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
            "如需将它们迁入本实例，请在安装时加上 --adopt-legacy-data",
            file=sys.stderr,
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


def _state_text(state: str) -> str:
    return {
        "starting": "正在启动（尚未确认启动成功）",
        "running": "运行中（启动时检查已通过）",
        "stopping": "正在停止",
        "stopped": "已停止",
        "failed": "启动或运行失败",
        "legacy": "使用旧版方式启动，请先检查运行情况",
    }.get(state, state)


def _entry_text(entry) -> str:
    if entry.kind != "generation":
        return entry.version
    info = read_generation(entry.path)
    plugins = ", ".join(p.name for p in info.plugins) if info else ""
    return f"{entry.version}（源码版本: {entry.base_version or '未知'}；插件: {plugins or '无'}）"


# ── distro ───────────────────────────────────────────────────


def _cmd_distro_list(args: argparse.Namespace) -> int:
    """列出发行版与版本/组合代（读 ~/.ghrah 布局 = 读 GPT；附各条目 commit）。"""
    if args.json:
        return _cmd_list(args)
    from ghrah_toolchain.instances import instance_status

    boot = BootToml.load()
    distros = list_distros()
    if not distros:
        print("尚未安装任何实例。可用 ghrah distro install NAME --repo PATH 安装。")
        return 0
    entries = {(e.distro, e.version): e for e in RuntimeRefManager().list_entries()}
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
        print(f"[{distro}]{'（默认实例）' if marker else ''}")
        print(f"  状态: {_state_text(instance_status(distro)['state'])}")
        print(f"  当前使用: {current or '未设置'}")
        print(f"  已构建的源码版本（@ 后为提交编号）: {rendered or '无'}")
        if gens:
            gen_rows = []
            for g in gens:
                e = entries.get((distro, g))
                if e is not None:
                    gen_rows.append(_entry_text(e))
                else:
                    gen_rows.append(g)
            print(f"  可运行的安装版本: {'; '.join(gen_rows)}")
    return 0


# ── install ─────────────────────────────────────────────────


def _cmd_install(args: argparse.Namespace) -> int:
    """安装发行版版本：建 distro.toml（首装）→ 门禁 → 快照 → 重指 → 写配置。"""
    distro = _instance_name(args)
    root = ghrah_root()
    distro_root = resolve_distro_root(distro, root)
    distro_toml = DistroToml.load(distro_root)
    repo_arg = args.repo or os.environ.get("GHRAH_MONOREPO_ROOT")
    if not distro_root.is_dir() or not (distro_toml.repo_path or distro_toml.repo_url):
        if not repo_arg:
            raise ToolchainError(
                "首次安装请用 --repo/--from 指定源码仓库，或设置环境变量 GHRAH_MONOREPO_ROOT"
            )
        from ghrah_toolchain.repository import RepositorySource

        location = RepositorySource.parse(repo_arg)
        if not location.remote and not Path(location.location).is_dir():
            raise ToolchainError(f"源码仓库不存在: {location.location}")
        rendered = render_distro_toml(
            name=distro,
            repo_path="" if location.remote else location.location,
            repo_url=location.location if location.remote else "",
            fetch_on_update=location.remote,
            source=args.ref or "latest",
            gate=args.gate or "basic",
            subject_port=args.subject_port,
            webui_port=args.webui_port,
            data_root="",
            snapshot_keep=3,
        )
    else:
        rendered = None
        if args.subject_port != 4112 or args.webui_port != 5173:
            raise ToolchainError("已有实例端口请通过 distro config set 修改")
    from ghrah_toolchain.user_config import validate_value

    validate_value("runtime.subject_port", args.subject_port)
    validate_value("runtime.webui_port", args.webui_port)
    if args.subject_port == args.webui_port:
        raise ToolchainError("后端与前端端口不能相同")
    RefSource.parse(args.ref or distro_toml.ref_source)
    logging.getLogger().setLevel(logging.INFO)
    print(f"[{distro}] 开始安装，构建与依赖准备可能需要数分钟……", flush=True, file=sys.stderr)
    _warn_legacy_data(root)
    manager = RuntimeRefManager(root=root, repo=args.repo)
    entry = manager.ensure(
        distro=distro,
        update=False,
        source=args.ref,
        version=args.version,
        gate=_resolve_gate(args, distro_toml),
        initial_config=rendered,
        fetch=(location.remote if rendered else distro_toml.ref_fetch_on_update),
    )
    if args.adopt_legacy_data:
        report = adopt_legacy(
            DistroLayout(root=distro_root), DistroToml.load(distro_root), subject_port=None
        )
        if not report.noop:
            print(
                f"旧数据迁移完成：{len(report.adopted)} 项已移入实例数据目录；"
                f"归档 {len(report.archived)} 项 → {report.legacy_dir}",
                file=sys.stderr,
            )
            print(
                "备份保留在 legacy/…/backup/，确认系统正常后可手动删除 legacy/ 目录",
                file=sys.stderr,
            )
    if args.json:
        _print_result(
            {
                "name": distro,
                "generation": entry.version,
                "commit": entry.commit,
                "state": "installed",
                "started": False,
            }
        )
    else:
        print(f"已安装 {entry.label}（源码提交: {entry.commit}），尚未启动。")
        print(f"下一步: ghrah distro start {distro}")
    return 0


# ── runtime ──────────────────────────────────────────────────


def _cmd_runtime_status(args: argparse.Namespace) -> int:
    """引导菜单状态：current / 配置 source / 候选 commit（dry）/ 落后 / last_good。"""
    boot = BootToml.load()
    distro = getattr(args, "name", None) or args.distro or _default_distro(boot)
    if not distro:
        print("（尚未安装任何发行版：先 ghrah distro install NAME --repo PATH）")
        return 0
    distro_root = resolve_distro_root(distro)
    layout = DistroLayout(root=distro_root)
    distro_toml = DistroToml.load(distro_root)
    manager = RuntimeRefManager()
    try:
        current = manager.resolve(distro)
    except RuntimeRefError as e:
        print(f"[{distro}] 未找到当前使用的安装版本（{e}）")
        current = None
    if current is not None:
        print(f"[{distro}] 当前使用: {current.label}（源码提交: {current.commit}）")
    entries = [e for e in manager.list_entries() if e.distro == distro]
    print(
        f"[{distro}] 已构建的源码版本（@ 后为提交编号）: "
        + (
            ", ".join(f"{e.version}@{_short(e.commit)}" for e in entries if e.kind == "version")
            or "（无）"
        )
    )
    gens = [e for e in entries if e.kind == "generation"]
    if gens or layout.list_generations():
        print(f"[{distro}] 可运行的安装版本: " + (", ".join(_entry_text(e) for e in gens) or "无"))
    print(
        f"[{distro}] 更新来源: {distro_toml.ref_source}；检查级别: {distro_toml.ref_gate}；"
        f"更新前同步远程源码: {'是' if distro_toml.ref_fetch_on_update else '否'}"
    )
    try:
        repo = manager._resolve_repo(distro_root, distro_toml)
        candidate = manager._resolve_candidate(
            RefSource.parse(distro_toml.ref_source), fetch=False, repo=repo
        )
        if current is not None and current.commit == candidate:
            print(f"[{distro}] 所选源码提交: {candidate}（与当前相同；未同步远程源码）")
        else:
            print(
                f"[{distro}] 所选源码提交: {candidate}（与当前不同；"
                f"用 ghrah distro update {distro} 更新；未同步远程源码）"
            )
    except RuntimeRefError as e:
        print(f"[{distro}] 无法确定要使用的源码提交: {e}")
    lg = layout.last_good
    lg_ver = "-"
    if lg.is_symlink() or lg.exists():
        try:
            lg_ver = lg.resolve().name
        except OSError, RuntimeError:
            lg_ver = "-"
    print(f"[{distro}] 上次成功启动: {boot.last_good or '无记录'}（本实例记录: {lg_ver}）")
    return 0


def _cmd_runtime_update(args: argparse.Namespace) -> int:
    """前移引导菜单：``ensure(update=True)``（门禁 → 快照 → 重指 → 按需 sync）。"""
    distro = _instance_name(args)
    distro_toml = DistroToml.load(resolve_distro_root(distro))
    manager = RuntimeRefManager(repo=args.repo)
    if args.check:
        if args.fetch:
            raise ToolchainError(
                "--check 仅比较本地记录，不能同时使用 --fetch；"
                "如需同步远程源码，请单独执行 git fetch"
            )
        current = manager.resolve(distro)
        candidate = manager._resolve_candidate(
            RefSource.parse(args.source or distro_toml.ref_source),
            fetch=False,
            repo=manager._resolve_repo(resolve_distro_root(distro), distro_toml),
        )
        data = {
            "name": distro,
            "current": current.commit,
            "candidate": candidate,
            "update_available": candidate != current.commit,
        }
        if args.json:
            _print_result(data)
        else:
            print(
                f"[{distro}] 当前源码提交: {current.commit}；所选源码提交: {candidate}；"
                f"{'与当前不同，可执行更新' if data['update_available'] else '已是所选提交'}"
                "（仅比较本地记录，未同步远程源码）"
            )
        return 0
    logging.getLogger().setLevel(logging.INFO)
    print(
        f"[{distro}] 开始更新（实例需停止），将检查运行环境并备份已有数据……",
        flush=True,
        file=sys.stderr,
    )
    entry = manager.ensure(
        distro=distro,
        update=True,
        fetch=_resolve_fetch(args, distro_toml),
        source=args.source,
        gate=_resolve_gate(args, distro_toml),
    )
    if args.json:
        _print_result(
            {
                "name": distro,
                "generation": entry.version,
                "commit": entry.commit,
                "state": "installed",
                "started": False,
            }
        )
    else:
        print(f"已切换到 {entry.label}（源码提交: {entry.commit}），尚未启动。")
    return 0


# ── build ────────────────────────────────────────────────────


def _cmd_build(args: argparse.Namespace) -> int:
    """构建发行物：源仓候选 commit → wheels/tarballs/observer-web/manifest（不动安装域）。"""
    repo_arg = args.repo or os.environ.get("GHRAH_MONOREPO_ROOT")
    if not repo_arg:
        raise ToolchainError("构建请用 --repo 指定源码仓库，或设置环境变量 GHRAH_MONOREPO_ROOT")
    repo = Path(repo_arg).expanduser()
    if not repo.is_dir():
        raise ToolchainError(f"源码仓库路径不存在: {repo}")
    source = RefSource.parse(args.ref)
    commit = RuntimeRefManager()._resolve_candidate(source, fetch=False, repo=repo)  # noqa: SLF001
    out_dir = Path(args.out).expanduser() if args.out else repo / "dist" / "toolchain" / commit[:7]
    result = build_artifacts(repo, commit, out_dir=out_dir)
    print(f"构建完成: {result.out_dir}")
    print(f"源码提交: {result.source_commit}")
    print(f"Python 安装包: {', '.join(result.wheels)}")
    print(f"前端安装包: {', '.join(result.tarballs)}")
    print(f"依赖版本清单: {result.pins_file}")
    if args.no_verify:
        print("（已跳过验证）")
    else:
        verify_artifacts(result.out_dir)
        print("构建结果检查通过（Python 包可导入，ghrah-subject-server 启动程序存在）")
    return 0


# ── boot / stop / doctor ─────────────────────────────────────


def _cmd_boot(args: argparse.Namespace) -> int:
    """引导：解析目标 →（救援：恢复快照/重指）→ spawn OS（+ 前端）→ 监督。"""
    if args.restore_snapshot and args.ref != "last-good":
        raise ToolchainError("--restore-snapshot 需配合 --ref last-good（数据回滚救援）")
    if args.no_webui and (args.webui_dev or args.webui_dev_dir):
        raise ToolchainError("--no-webui 不能与 --webui-dev 或 --webui-dev-dir 同时使用")
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
    positional = getattr(args, "name", None)
    optional = getattr(args, "distro", None)
    if positional and optional and positional != optional:
        raise ToolchainError("命令中的实例名与 --distro 指定的实例名不同，请保持一致")
    name = positional or optional or _default_distro(BootToml.load())
    if not name:
        raise ToolchainError("未指定实例名称，且未配置默认实例")
    resolve_distro_root(name)
    return name


def _print_state(state: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"schema": 1, "scope": "user", "data": state}, ensure_ascii=False))
    else:
        print(
            f"[{state['name']}] {_state_text(state['state'])}（进程编号: {state.get('pid', '-')}）"
        )
        if state.get("log"):
            print(f"日志: {state['log']}；使用 ghrah logs {state['name']} 查看")


def _cmd_list(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import list_instances

    states = list_instances()
    if args.json:
        print(json.dumps({"schema": 1, "scope": "user", "data": states}, ensure_ascii=False))
    else:
        for state in states:
            _print_state(state, False)
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import read_logs

    if args.tail < 0:
        raise ToolchainError("--tail 不能为负数")
    name = _instance_name(args)
    if args.follow:
        from ghrah_toolchain.instances import follow_logs

        try:
            for chunk in follow_logs(name, tail=args.tail):
                print(chunk, end="", flush=True)
        except KeyboardInterrupt:
            pass
    else:
        print(read_logs(name, tail=args.tail), end="")
    return 0


def _print_result(data: dict, scope: str = "user") -> None:
    print(
        json.dumps(
            {"schema": 1, "scope": scope, "data": data, "diagnostics": []}, ensure_ascii=False
        )
    )


def _cmd_status(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import instance_status

    name = _instance_name(args)
    state = instance_status(name)
    layout = DistroLayout(resolve_distro_root(name))
    try:
        entry = RuntimeRefManager().resolve(name)
        state["installation"] = {
            "state": "ready",
            "generation": entry.version,
            "commit": entry.commit,
        }
    except RuntimeRefError as exc:
        state["installation"] = {"state": "incomplete", "error": str(exc)}
    config = DistroToml.load(layout.root)
    state["addresses"] = {
        "subject": f"http://127.0.0.1:{config.subject_port}",
        "webui": f"http://127.0.0.1:{config.webui_port}",
    }
    state["health"] = "not-probed"
    if args.json:
        _print_result(state)
    else:
        _print_state(state, False)
        installation = state["installation"]
        if installation["state"] == "ready":
            print(f"安装版本: {installation['generation']}（源码提交: {installation['commit']}）")
        else:
            print(f"安装不完整: {installation['error']}")
        print(f"前端页面: {state['addresses']['webui']}")
        print(f"后端接口: {state['addresses']['subject']}")
        print("以上地址来自配置，本次未检查服务是否可访问。")
        if state.get("error"):
            print(f"最近错误: {state['error']}")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import start_instance, stop_instance

    name = _instance_name(args)
    stop_instance(name, timeout=args.timeout)
    _print_state(start_instance(name, no_webui=args.no_webui), args.json)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    from ghrah_toolchain.user_config import config_view, set_config

    name = _instance_name(args) if args.config_scope == "distro" else None
    scope = "user" if name else "toolchain"
    effective_labels = {
        "next-command": "下次运行命令时生效",
        "next-install-or-update": "下次安装或更新时生效",
        "next-update": "下次更新时生效",
        "next-start": "下次启动时生效",
    }
    if args.config_action == "set":
        item = set_config(args.key, args.value, name=name)
        if args.json:
            _print_result({args.key: item}, scope)
        else:
            print(f"已设置 {args.key} = {item['value']}（{effective_labels[item['effective']]}）")
        return 0
    values = config_view(name)
    if args.config_action == "get":
        if args.key not in values:
            raise ToolchainError(f"不支持的配置键: {args.key}")
        values = {args.key: values[args.key]}
    if args.json:
        _print_result(values, scope)
    else:
        for key, item in values.items():
            print(
                f"{key} = {json.dumps(item['value'], ensure_ascii=False)} "
                f"（来源: {'默认值' if item['source'] == 'default' else item['source']}；"
                f"{effective_labels[item['effective']]}）"
            )
    return 0


def _cmd_default(args: argparse.Namespace) -> int:
    from ghrah_toolchain.instances import _locked

    name = _instance_name(args)
    with _locked(resolve_distro_root(name)):
        with _locked(ghrah_root()):
            entry = RuntimeRefManager().resolve(name)
            boot = BootToml.load()
            boot.default = entry.label
            boot.save()
    print(f"默认实例: {name}（{entry.label}）")
    return 0


def _cmd_self_status(args: argparse.Namespace) -> int:
    import importlib.metadata

    data = {
        "version": __version__,
        "python": sys.executable,
        "prefix": sys.prefix,
        "home": str(ghrah_root()),
        "config": str(ghrah_root() / "config.toml"),
        "installation": str(importlib.metadata.distribution("ghrah-toolchain").locate_file("")),
    }
    if args.json:
        _print_result(data, "toolchain")
    else:
        labels = {
            "version": "工具链版本",
            "python": "Python 解释器",
            "prefix": "Python 环境目录",
            "home": "实例安装目录",
            "config": "工具链配置文件",
            "installation": "工具链安装位置",
        }
        for key, value in data.items():
            print(f"{labels[key]}: {value}")
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
        print(f"配置文件检查通过: {manifest.path}")
        print("本次仅检查配置，未启动组件或设置组件连接，也未授予 Agent 权限。")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """诊断：安装域布局、boot.toml、各发行版 current commit / last_good / 落后。"""
    del args
    from ghrah_toolchain.commands.ability import warn_legacy_store

    warn_legacy_store()
    boot = BootToml.load()
    print(f"ghrah-toolchain {__version__}")
    import shutil

    print(
        "源码安装依赖: "
        + ", ".join(f"{tool}={shutil.which(tool) or '缺失'}" for tool in ("git", "uv", "pnpm"))
    )
    print(f"默认启动项: {boot.default or '未设置'}")
    print(f"备用启动项: {boot.fallback or '未设置'}")
    print(f"上次成功启动: {boot.last_good or '无记录'}")
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
            current_target = "未设置（缺少 current）"
        behind = "无法确定要使用的源码提交"
        try:
            repo = manager._resolve_repo(layout.root, distro_toml)
            candidate = manager._resolve_candidate(
                RefSource.parse(distro_toml.ref_source), fetch=False, repo=repo
            )
            behind = (
                "与所选源码提交相同"
                if (commit is not None and candidate == commit)
                else (f"与所选源码提交不同（{candidate[:7]}）")
            )
        except RuntimeRefError:
            pass
        # 当前代缺身份（无 BOOTSTRAP）= 异常态
        identity_note = ""
        if commit is None:
            identity_note = "（无法确定已安装版本的源码提交，建议重新安装）"
        print(
            f"[{distro}]\n"
            f"  已构建的源码版本: {', '.join(layout.list_versions()) or '无'}\n"
            f"  可运行的安装版本: {', '.join(layout.list_generations()) or '无'}\n"
            f"  当前安装位置: {current_target}\n"
            f"  源码提交: {_short(commit)}{identity_note}\n"
            f"  更新检查（未同步远程源码）: {behind}\n"
            f"  后端端口: {distro_toml.subject_port}；前端端口: {distro_toml.webui_port}"
        )
    return 0


# ── 命令行装配 ───────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghrah",
        description="ghrah 工具链：检查配置、构建源码、安装和管理实例",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="显示详细调试日志（放在子命令之前，如 ghrah -v dev run）",
    )
    subparsers = parser.add_subparsers(dest="command")

    for command in ("check", "metadata"):
        manifest = subparsers.add_parser(
            command, help="检查或查看 ghrah.toml 中的组件配置（无需联网）"
        )
        manifest.add_argument("--manifest-path", type=Path)
        manifest.add_argument("--json", action="store_true")
        manifest.set_defaults(func=_cmd_manifest)

    from ghrah_toolchain.commands.user import register_user_commands

    register_user_commands(
        subparsers,
        {
            "install": _cmd_install,
            "update": _cmd_runtime_update,
            "start": _cmd_boot,
            "stop": _cmd_stop,
            "restart": _cmd_restart,
            "status": _cmd_status,
            "list": _cmd_distro_list,
            "logs": _cmd_logs,
            "config": _cmd_config,
            "default": _cmd_default,
            "runtime_status": _cmd_runtime_status,
            "self_status": _cmd_self_status,
            "doctor": _cmd_doctor,
        },
    )

    from ghrah_toolchain.commands.ability import register_ability_commands

    register_ability_commands(subparsers)

    build = subparsers.add_parser("build", help="从源码构建 Python 和前端安装包")
    build.add_argument("--repo", help="源码仓库路径（优先于环境变量 GHRAH_MONOREPO_ROOT）")
    build.add_argument(
        "--ref",
        help="要构建的源码版本：latest、branch:<名称>、tag:<名称> 或 commit:<提交编号>；"
        "默认 latest",
    )
    build.add_argument(
        "--out", help="构建结果的保存目录（默认 <repo>/dist/toolchain/<提交编号前7位>/）"
    )
    build.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过构建后的检查（默认检查 Python 包能否导入、启动程序是否存在）",
    )
    build.set_defaults(func=_cmd_build)

    dev = subparsers.add_parser("dev", help="构建本地代码并启动开发服务")
    dev_sub = dev.add_subparsers(dest="subcommand")
    dev_status = dev_sub.add_parser("status", help="查看本地代码有哪些改动，以及开发服务是否运行")
    dev_status.add_argument("--distro", help="实例名（省略时使用 boot.toml 中的默认实例）")
    dev_status.set_defaults(func=_cmd_dev)
    dev_run = dev_sub.add_parser(
        "run", aliases=["up"], help="构建后在当前终端启动服务（已运行则先停止，等端口释放后重启）"
    )
    dev_run.add_argument("--distro", help="实例名（省略时使用 boot.toml 中的默认实例）")
    dev_run.add_argument("--no-restart", action="store_true", help="只构建/升级，不启动服务")
    dev_run.add_argument("--full", action="store_true", help="重新构建全部内容，即使代码没有改动")
    dev_run.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    dev_run.set_defaults(func=_cmd_dev)
    dev_stop = dev_sub.add_parser("stop", aliases=["down"], help="停止开发服务及其相关进程")
    dev_stop.add_argument("--distro", help="实例名（省略时使用 boot.toml 中的默认实例）")
    dev_stop.set_defaults(func=_cmd_dev)
    dev_reset = dev_sub.add_parser(
        "reset", help="删除 .dev 中的开发构建文件和运行环境，下次重新构建"
    )
    dev_reset.add_argument("--distro", help="实例名（省略时使用 boot.toml 中的默认实例）")
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
        # 显式 config 命令不依赖已有偏好，错误配置仍能逐键修正。
        if getattr(args, "config_action", None) is None:
            from ghrah_toolchain.user_config import config_view

            preferences = config_view()
            if hasattr(args, "json") and not args.json:
                args.json = preferences["self.output"]["value"] == "json"
            if hasattr(args, "tail") and args.tail is None:
                args.tail = preferences["self.log_tail"]["value"]
        return args.func(args)
    except (ToolchainError, OSError, ValueError, subprocess.SubprocessError) as e:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "schema": 1,
                        "scope": (
                            "workspace"
                            if args.command in {"check", "metadata"}
                            else "toolchain"
                            if args.command == "self"
                            else "ability"
                            if args.command == "ability"
                            else "user"
                        ),
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
