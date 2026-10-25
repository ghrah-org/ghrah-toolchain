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
            "本次安装不会改动它们（认领: install --adopt-legacy-data）",
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


# ── distro ───────────────────────────────────────────────────


def _cmd_distro_list(args: argparse.Namespace) -> int:
    """列出发行版与版本/组合代（读 ~/.ghrah 布局 = 读 GPT；附各条目 commit）。"""
    if args.json:
        return _cmd_list(args)
    from ghrah_toolchain.instances import instance_status

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
        print(f"{'':<16} state: {instance_status(distro)['state']}")
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
        repo_path = Path(repo_arg).expanduser().resolve()
        if not repo_path.is_dir():
            raise ToolchainError(f"运行时仓不存在: {repo_path}")
        rendered = render_distro_toml(
            name=distro,
            repo_path=str(repo_path),
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
        raise ToolchainError("Subject 与 WebUI 端口不能相同")
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
    )
    if args.adopt_legacy_data:
        report = adopt_legacy(
            DistroLayout(root=distro_root), DistroToml.load(distro_root), subject_port=None
        )
        if not report.noop:
            print(
                f"认领完成：数据面 {len(report.adopted)} 项 → 数据盘；"
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
        print(f"已安装 {entry.label}（commit {entry.commit}），尚未启动。")
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
    distro = _instance_name(args)
    distro_toml = DistroToml.load(resolve_distro_root(distro))
    manager = RuntimeRefManager(repo=args.repo)
    if args.check:
        if args.fetch:
            raise ToolchainError("--check 不执行 fetch，请先显式同步源仓")
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
                f"[{distro}] current={current.commit} candidate={candidate} "
                f"{'有可用更新' if data['update_available'] else '已是所选提交'}（未 fetch）"
            )
        return 0
    logging.getLogger().setLevel(logging.INFO)
    print(f"[{distro}] 开始更新（实例需停止），准备门禁与快照……", flush=True, file=sys.stderr)
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
        print(f"current → {entry.label}（commit {entry.commit}），尚未启动。")
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
    positional = getattr(args, "name", None)
    optional = getattr(args, "distro", None)
    if positional and optional and positional != optional:
        raise ToolchainError("实例位置参数与 --distro 冲突")
    name = positional or optional or _default_distro(BootToml.load())
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
        print(f"安装: {state['installation']}")
        print(f"地址（配置值，未探测健康）: {state['addresses']}")
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
    if args.config_action == "set":
        item = set_config(args.key, args.value, name=name)
        if args.json:
            _print_result({args.key: item}, scope)
        else:
            print(f"已设置 {args.key} = {item['value']}（{item['effective']}）")
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
                f"（来源: {item['source']}；生效: {item['effective']}）"
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
        for key, value in data.items():
            print(f"{key}: {value}")
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

    dev = subparsers.add_parser("dev", help="本地开发快环（工作树增量构建 + 本地起服）")
    dev_sub = dev.add_subparsers(dest="subcommand")
    dev_status = dev_sub.add_parser("status", help="对比工作树与上次 dev 构建，报告变更与会话状态")
    dev_status.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_status.set_defaults(func=_cmd_dev)
    dev_run = dev_sub.add_parser(
        "run", aliases=["up"], help="构建后前台起服（已在跑则先停、回收端口，再重启）"
    )
    dev_run.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_run.add_argument("--no-restart", action="store_true", help="只构建/升级，不启动服务")
    dev_run.add_argument("--full", action="store_true", help="强制全量重建（忽略增量指纹）")
    dev_run.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    dev_run.set_defaults(func=_cmd_dev)
    dev_stop = dev_sub.add_parser("stop", aliases=["down"], help="停止运行中的 dev 会话")
    dev_stop.add_argument("--distro", help="发行版名（默认取 boot.toml default）")
    dev_stop.set_defaults(func=_cmd_dev)
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
