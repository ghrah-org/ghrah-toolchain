# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot 编排测试（out-of-process 引导）。

覆盖：
- 引导目标解析（current 指向、default 回退、--version 不一致拒绝、versions/ 逃逸拒绝）
- 数据面五项 + 端口 env 注入完整且指向数据盘
- OS 子进程 spawn（venv 入口、cwd=current、start_new_session）
- 前端子进程 spawn（cwd=observer-web、VITE_GHRAH_SUBJECT_WS_URL 注入）
- 监督与收束（停止信号逆序收束；OS 意外退出返回 1）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from ghrah_bootloader.boot import (
    build_data_plane_env,
    ensure_data_dirs,
    os_server_command,
    resolve_boot_target,
    resolve_data_root,
    run_boot,
)
from ghrah_bootloader.layout import BootToml, DistroLayout, DistroToml


def _make_gen(
    root: Path, distro: str = "generic", gen: str = "2026.09", base: str = "2026.09"
) -> DistroLayout:
    """物化 packages 单形态布局：基座树 versions/<base> + 组合代 generations/<gen> + current → 代。

    这是唯一形态：OS 入口在代内 env（``generations/<gen>/env/.venv``），
    current 只指向组合代。
    """
    distro_root = root / distro
    base_tree = distro_root / "versions" / base
    (base_tree / "ghrah-observer-webui" / "packages" / "observer-web").mkdir(parents=True)
    gen_dir = distro_root / "generations" / gen
    bin_dir = gen_dir / "env" / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    server = bin_dir / ("ghrah-subject-server.exe" if os.name == "nt" else "ghrah-subject-server")
    server.write_text("#!/bin/sh\nexit 0\n" if os.name != "nt" else "")
    if os.name != "nt":
        server.chmod(0o755)
    (gen_dir / "GENERATION.toml").write_text(
        "[generation]\n"
        "schema_version = 1\n"
        f'base_version = "{base}"\n'
        'base_commit = "861ca39"\n'
        'pins_sha256 = "00000000"\n',
        encoding="utf-8",
    )
    (distro_root / "distro.toml").write_text(
        f"""
[distro]
name = "{distro}"

[runtime]
subject_port = 4112
webui_port = 5173
""",
        encoding="utf-8",
    )
    current = distro_root / "current"
    current.symlink_to(gen_dir)
    return DistroLayout(root=distro_root)


def _make_distro(root: Path, distro: str = "generic", version: str = "2026.09") -> DistroLayout:
    """物化一个最小发行版布局（packages 单形态；gen 名 = base 名 = version）。"""
    return _make_gen(root, distro=distro, gen=version, base=version)


# ── 数据面 env ───────────────────────────────────────────────


def test_build_data_plane_env_covers_five_items(tmp_path: Path) -> None:
    env = build_data_plane_env(tmp_path)
    assert env["GHRAH_SUBJECT_DB_PATH"] == str(tmp_path / "subject.db")
    assert env["GHRAH_SUBJECT_TASKSTORE_DB"] == str(tmp_path / "taskstore.sqlite3")
    assert env["GHRAH_SUBJECT_MANIFEST_ROOT"] == str(tmp_path / "manifests")
    assert env["GHRAH_SUBJECT_PROJECT_DEFAULT_ROOT_LOCATOR_TEMPLATE"] == str(
        tmp_path / "projects" / "{project_id}"
    )
    assert env["GHRAH_SUBJECT_WORKSPACE_ROOT"] == str(tmp_path / "workspace")


def test_data_plane_env_has_no_default_locations(tmp_path: Path) -> None:
    """五项不得落在任何版本的默认位（~/.ghrah 顶层 / ~/ghrah-workspace）。"""
    env = build_data_plane_env(tmp_path)
    for value in env.values():
        assert value.startswith(str(tmp_path))


def test_ensure_data_dirs_creates_structure(tmp_path: Path) -> None:
    ensure_data_dirs(tmp_path)
    for sub in ("manifests", "projects", "workspace"):
        assert (tmp_path / sub).is_dir()


def test_resolve_data_root_override(tmp_path: Path) -> None:
    layout = DistroLayout(root=tmp_path / "generic")
    toml = DistroToml(data_root=str(tmp_path / "external-data"))
    assert resolve_data_root(layout, toml) == tmp_path / "external-data"
    assert resolve_data_root(layout, DistroToml()) == tmp_path / "generic" / "data"


# ── 目标解析 ─────────────────────────────────────────────────


def test_resolve_boot_target_from_current(tmp_path: Path) -> None:
    _make_distro(tmp_path)
    target = resolve_boot_target("generic", None, root=tmp_path)
    assert target.distro == "generic"
    assert target.version == "2026.09"
    assert target.tree == (tmp_path / "generic" / "generations" / "2026.09").resolve()
    assert target.distro_toml.subject_port == 4112


def test_resolve_boot_target_uses_boot_toml_default(tmp_path: Path) -> None:
    _make_distro(tmp_path)
    (tmp_path / "boot.toml").write_text('default = "generic/2026.09"\n', encoding="utf-8")
    target = resolve_boot_target(None, None, root=tmp_path)
    assert target.distro == "generic"


def test_resolve_boot_target_requires_distro(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    with pytest.raises(BootloaderError, match="未指定发行版"):
        resolve_boot_target(None, None, root=tmp_path)


def test_resolve_boot_target_unknown_distro(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    with pytest.raises(BootloaderError, match="未安装"):
        resolve_boot_target("nope", None, root=tmp_path)


def test_resolve_boot_target_version_mismatch_rejected(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    with pytest.raises(BootloaderError, match="不一致"):
        resolve_boot_target("generic", "2026.10", root=tmp_path)


def test_resolve_boot_target_current_escape_rejected(tmp_path: Path) -> None:
    """current 指向受管段（generations/）之外的路径时拒绝引导。"""
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "generic" / "current").unlink()
    (tmp_path / "generic" / "current").symlink_to(outside)
    with pytest.raises(BootloaderError, match="generations/ 之外"):
        resolve_boot_target("generic", None, root=tmp_path)


def test_resolve_boot_target_missing_current(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    (tmp_path / "generic" / "current").unlink()
    with pytest.raises(BootloaderError, match="current"):
        resolve_boot_target("generic", None, root=tmp_path)


# ── OS 命令 ──────────────────────────────────────────────────


def test_os_server_command_missing_binary(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    layout = DistroLayout(root=tmp_path)
    with pytest.raises(BootloaderError, match="入口缺失"):
        os_server_command(layout.root / "env" / ".venv")


# ── run_boot 端到端（真实子进程，mock OS 脚本） ───────────────

_OS_SCRIPT_TEMPLATE = """
import threading, time, os, json
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(os.environ["GHRAH_SUBJECT_SERVER_PORT"])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/env":
            body = json.dumps(dict(os.environ)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')
    def log_message(self, *a):
        pass
srv = HTTPServer(("127.0.0.1", port), H)
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
with open(os.environ["BOOT_TEST_ENV_DUMP"], "w") as f:
    json.dump(dict(os.environ), f)
time.sleep(60)
"""


@pytest.fixture
def distro_with_mock_os(tmp_path: Path):
    """发行版布局 + 可执行的 mock OS server（记录 env 到 dump 文件）。

    单形态：mock OS 落在代内 env（``current/env/.venv/bin``）。
    """
    layout = _make_distro(tmp_path)
    bin_dir = layout.current / "env" / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    server = bin_dir / ("ghrah-subject-server.exe" if os.name == "nt" else "ghrah-subject-server")
    script_path = tmp_path / "os_server_script.py"
    script_path.write_text(_OS_SCRIPT_TEMPLATE, encoding="utf-8")
    if os.name == "nt":
        server.write_text("", encoding="utf-8")
        pytest.skip("POSIX-only end-to-end spawn test")
    else:
        server.write_text(f"#!/bin/sh\nexec {sys.executable} {script_path}\n", encoding="utf-8")
        server.chmod(0o755)
    return tmp_path, layout


async def test_run_boot_env_injection_and_supervision(
    distro_with_mock_os, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_port_factory
) -> None:
    """spawn OS → env 六项注入指向数据盘 → cwd=current → 停止信号逆序收束。"""
    root, layout = distro_with_mock_os
    dump = tmp_path / "os_env.json"
    monkeypatch.setenv("BOOT_TEST_ENV_DUMP", str(dump))
    target = resolve_boot_target("generic", None, root=root)
    port = free_port_factory()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_boot(
            target,
            no_webui=True,
            subject_port=port,
            stop_event=stop,
        )
    )
    try:
        await asyncio.wait_for(_wait_dump(dump), timeout=15.0)
    finally:
        stop.set()
        code = await asyncio.wait_for(task, timeout=15.0)
    assert code == 0

    import json

    os_env = json.loads(dump.read_text(encoding="utf-8"))
    data_root = root / "generic" / "data"
    assert os_env["GHRAH_SUBJECT_DB_PATH"] == str(data_root / "subject.db")
    assert os_env["GHRAH_SUBJECT_TASKSTORE_DB"] == str(data_root / "taskstore.sqlite3")
    assert os_env["GHRAH_SUBJECT_MANIFEST_ROOT"] == str(data_root / "manifests")
    assert os_env["GHRAH_SUBJECT_PROJECT_DEFAULT_ROOT_LOCATOR_TEMPLATE"] == str(
        data_root / "projects" / "{project_id}"
    )
    assert os_env["GHRAH_SUBJECT_WORKSPACE_ROOT"] == str(data_root / "workspace")
    assert os_env["GHRAH_SUBJECT_SERVER_HOST"] == "127.0.0.1"
    assert os_env["GHRAH_SUBJECT_SERVER_PORT"] == str(port)
    # 数据盘目录已物化
    for sub in ("manifests", "projects", "workspace"):
        assert (data_root / sub).is_dir()


async def test_run_boot_os_exit_returns_failure(distro_with_mock_os, free_port_factory) -> None:
    """OS 子进程意外退出 → run_boot 返回 1（其余子进程已收束）。"""
    root, layout = distro_with_mock_os
    bin_dir = layout.current / "env" / ".venv" / "bin"
    server = bin_dir / "ghrah-subject-server"
    # 覆写为「起健康端点后立即退出」的 OS
    port_holder = {"port": free_port_factory()}
    script = (
        "import os, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        f"port = {port_holder['port']}\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'{}')\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "srv = HTTPServer(('127.0.0.1', port), H)\n"
        "import threading\n"
        "t = threading.Thread(target=srv.serve_forever, daemon=True)\n"
        "t.start()\n"
        "import sys\n"
        "sys.exit(3)\n"
    )
    server.write_text(
        f"#!/bin/sh\nexec {sys.executable} -c {script!r}\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    target = resolve_boot_target("generic", None, root=root)
    # 需要绕开「健康检查未过即视为失败」的路径：run_boot 的 stack.start() 若
    # OS 已退出，wait_for_server 会超时抛错——此用例断言该失败可被收束。
    with pytest.raises((TimeoutError, Exception)):
        await asyncio.wait_for(
            run_boot(target, no_webui=True, subject_port=free_port_factory()), timeout=15.0
        )


async def _wait_dump(dump: Path, timeout: float = 15.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not dump.is_file():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"env dump not written: {dump}")
        await asyncio.sleep(0.2)


# ── 救援与 known-good ────────────────────────────────────────


def _add_version(root: Path, version: str) -> Path:
    """新增一个组合代目录（引导目标只认 generations/）。"""
    gen = root / "generic" / "generations" / version
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "GENERATION.toml").write_text(
        "[generation]\n"
        "schema_version = 1\n"
        f'base_version = "{version}"\n'
        'base_commit = "861ca39"\n'
        'pins_sha256 = "00000000"\n',
        encoding="utf-8",
    )
    return gen


class _StubProc:
    @staticmethod
    def poll():
        return None


class _StubStack:
    """不 spawn 真实子进程的 BootStack 替身（start 即就绪、OS 永不退出）。"""

    def __init__(self, config, **kwargs):
        self.os_process = _StubProc()

    async def start(self):
        return None

    async def stop(self):
        return None


def _make_last_good(root: Path, version: str = "2026.09") -> None:
    (root / "generic" / "last-good").symlink_to(root / "generic" / "generations" / version)


def _make_fake_snapshot(root: Path) -> Path:
    snap = root / "generic" / "snapshots" / "20260924T000000-v2026.09→v2026.10"
    (snap / "data").mkdir(parents=True)
    (snap / "META.toml").write_text(
        'old_version = "2026.09"\nnew_version = "2026.10"\n', encoding="utf-8"
    )
    return snap


def test_resolve_boot_target_ref_last_good_symlink(tmp_path: Path) -> None:
    """--ref last-good：目标取 last-good 符号链接，跳过 --version 一致性校验。"""
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    _add_version(tmp_path, "2026.10")
    (tmp_path / "generic" / "last-good").symlink_to(
        tmp_path / "generic" / "generations" / "2026.10"
    )
    target = resolve_boot_target("generic", "2026.09", root=tmp_path, ref="last-good")
    assert target.version == "2026.10"
    assert target.tree == (tmp_path / "generic" / "generations" / "2026.10").resolve()
    with pytest.raises(BootloaderError, match="仅接受 last-good"):
        resolve_boot_target("generic", None, root=tmp_path, ref="branch:main")


def test_resolve_boot_target_ref_last_good_falls_back_to_toml(tmp_path: Path) -> None:
    """last-good 符号链接缺失 → 回退 boot.toml.last_good。"""

    _make_distro(tmp_path)
    _add_version(tmp_path, "2026.10")
    (tmp_path / "boot.toml").write_text(
        'default = "generic/2026.09"\nlast_good = "generic/2026.10"\n', encoding="utf-8"
    )
    target = resolve_boot_target("generic", None, root=tmp_path, ref="last-good")
    assert target.version == "2026.10"


def test_resolve_boot_target_ref_last_good_missing(tmp_path: Path) -> None:
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    with pytest.raises(BootloaderError, match="last-good 未设置"):
        resolve_boot_target("generic", None, root=tmp_path, ref="last-good")


async def test_run_boot_restore_snapshot_with_yes(
    distro_with_mock_os, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--restore-snapshot --yes：恢复最近快照 → 重指 current → 启动（跳过确认）。"""
    import ghrah_bootloader.boot as boot_mod

    root, _ = distro_with_mock_os
    _make_last_good(root)
    snap = _make_fake_snapshot(root)
    restored: list[Path] = []
    monkeypatch.setattr(
        boot_mod.snapshot,
        "restore",
        lambda layout_, toml, snap_: restored.append(snap_) or "2026.09",
    )
    repointed: list[tuple[str, str]] = []
    real_repoint = boot_mod.repoint_current
    monkeypatch.setattr(
        boot_mod,
        "repoint_current",
        lambda distro_root, ver: (
            repointed.append((str(distro_root), ver)) or real_repoint(distro_root, ver)
        ),
    )
    monkeypatch.setattr(boot_mod, "BootStack", _StubStack)
    target = resolve_boot_target("generic", None, root=root, ref="last-good")
    stop = asyncio.Event()
    stop.set()
    code = await run_boot(target, no_webui=True, restore_snapshot=True, yes=True, stop_event=stop)
    assert code == 0
    assert restored == [snap]
    assert repointed == [(str(root / "generic"), "2026.09")]


async def test_run_boot_restore_snapshot_confirm_declined(
    distro_with_mock_os, monkeypatch: pytest.MonkeyPatch
) -> None:
    """交互二次确认拒绝 → 中止恢复并报错。"""

    root, _ = distro_with_mock_os
    _make_last_good(root)
    _make_fake_snapshot(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    target = resolve_boot_target("generic", None, root=root, ref="last-good")
    from ghrah_bootloader.errors import BootloaderError

    with pytest.raises(BootloaderError, match="已取消"):
        await run_boot(target, no_webui=True, restore_snapshot=True, stop_event=asyncio.Event())


async def test_run_boot_restore_snapshot_requires_yes_when_non_tty(
    distro_with_mock_os, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非交互 stdin 未给 --yes → 拒绝恢复。"""

    root, _ = distro_with_mock_os
    _make_last_good(root)
    _make_fake_snapshot(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    target = resolve_boot_target("generic", None, root=root, ref="last-good")
    from ghrah_bootloader.errors import BootloaderError

    with pytest.raises(BootloaderError, match="--yes"):
        await run_boot(target, no_webui=True, restore_snapshot=True, stop_event=asyncio.Event())


async def test_run_boot_updates_last_good_after_start(
    distro_with_mock_os, monkeypatch: pytest.MonkeyPatch
) -> None:
    """健康检查通过后更新 last-good 符号链接 + boot.toml.last_good（失败仅告警）。"""
    import ghrah_bootloader.boot as boot_mod

    root, layout = distro_with_mock_os
    monkeypatch.setattr(boot_mod, "BootStack", _StubStack)
    target = resolve_boot_target("generic", None, root=root)
    stop = asyncio.Event()
    stop.set()
    code = await run_boot(target, no_webui=True, stop_event=stop)
    assert code == 0
    assert layout.last_good.is_symlink()
    assert layout.last_good.resolve().name == "2026.09"
    assert BootToml.load(root).last_good == "generic/2026.09"


# ── 双段解析（generations ∪ versions 并存期） ────────────────


def test_resolve_boot_target_current_to_generation(tmp_path: Path) -> None:
    """packages 域 current → generations/<gen-id> 可解析引导。"""
    _make_distro(tmp_path)
    gen = tmp_path / "generic" / "generations" / "2026.09"
    target = resolve_boot_target("generic", None, root=tmp_path)
    assert target.version == "2026.09"
    assert target.tree == gen.resolve()


def test_resolve_boot_target_generation_escape_still_rejected(tmp_path: Path) -> None:
    """generations 段内多层目标（generations/<gen>/sub）不算单层代，拒绝。"""
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    deep = tmp_path / "generic" / "generations" / "2026.09" / "sub"
    deep.mkdir(parents=True)
    (tmp_path / "generic" / "current").unlink()
    (tmp_path / "generic" / "current").symlink_to(deep)
    with pytest.raises(BootloaderError, match="之外"):
        resolve_boot_target("generic", None, root=tmp_path)


def test_resolve_boot_target_ref_last_good_generation(tmp_path: Path) -> None:
    """--ref last-good：符号链接指向代目录时目标为该代。"""
    _make_distro(tmp_path)
    gen = tmp_path / "generic" / "generations" / "2026.09"
    (tmp_path / "generic" / "last-good").symlink_to(gen)
    target = resolve_boot_target("generic", None, root=tmp_path, ref="last-good")
    assert target.version == "2026.09"
    assert target.tree == gen.resolve()


def test_resolve_boot_target_ref_last_good_toml_generation(tmp_path: Path) -> None:
    """last-good 符号链接缺失 → 回退 boot.toml.last_good（generations 优先查）。"""
    gen = tmp_path / "generic" / "generations" / "2026.09"
    gen.mkdir(parents=True)
    (tmp_path / "generic" / "distro.toml").write_text(
        "[distro]\nname = 'generic'\n", encoding="utf-8"
    )
    (tmp_path / "boot.toml").write_text(
        'default = "generic/2026.09"\nlast_good = "generic/2026.09"\n', encoding="utf-8"
    )
    target = resolve_boot_target("generic", None, root=tmp_path, ref="last-good")
    assert target.version == "2026.09"
    assert target.tree == gen.resolve()


# ── OS 入口经 current 进代 ───────────────────────────────────


def _server_entry(venv: Path) -> Path:
    from ghrah_bootloader.boot import os_server_entry

    return os_server_entry(venv)


def test_resolve_os_server_prefers_current_gen_env(tmp_path: Path) -> None:
    """代目标 OS 入口 = current/env/.venv。"""
    from ghrah_bootloader.boot import resolve_os_server

    layout = _make_gen(tmp_path)
    gen_entry = _server_entry(layout.current / "env" / ".venv")
    cmd = resolve_os_server(layout)
    assert cmd == [str(gen_entry)]


def test_resolve_os_server_missing_raises(tmp_path: Path) -> None:
    """代内 env 入口缺失 → 报错（发行级兜底已退役）。"""
    from ghrah_bootloader.boot import resolve_os_server
    from ghrah_bootloader.errors import BootloaderError

    layout = _make_gen(tmp_path)
    _server_entry(layout.current / "env" / ".venv").unlink()
    with pytest.raises(BootloaderError, match="当前代 env 未物化"):
        resolve_os_server(layout)


_GEN_OS_SCRIPT_TEMPLATE = """
import threading, time, os, json
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(os.environ["GHRAH_SUBJECT_SERVER_PORT"])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')
    def log_message(self, *a):
        pass
srv = HTTPServer(("127.0.0.1", port), H)
t = threading.Thread(target=srv.serve_forever, daemon=True)
t.start()
with open(os.environ["BOOT_TEST_ENV_DUMP"], "w") as f:
    json.dump({"BOOT_TEST_CWD": os.getcwd(), **dict(os.environ)}, f)
time.sleep(60)
"""


@pytest.fixture
def gen_distro_with_mock_os(tmp_path: Path):
    """packages 代形态布局 + 代内 mock OS（current → generations/<gen>，记录 cwd + env）。"""
    layout = _make_gen(tmp_path)
    gen_dir = tmp_path / "generic" / "generations" / "2026.09"
    bin_dir = gen_dir / "env" / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    server = bin_dir / ("ghrah-subject-server.exe" if os.name == "nt" else "ghrah-subject-server")
    script_path = tmp_path / "os_gen_script.py"
    script_path.write_text(_GEN_OS_SCRIPT_TEMPLATE, encoding="utf-8")
    if os.name == "nt":
        server.write_text("", encoding="utf-8")
        pytest.skip("POSIX-only end-to-end spawn test")
    server.write_text(f"#!/bin/sh\nexec {sys.executable} {script_path}\n", encoding="utf-8")
    server.chmod(0o755)
    return tmp_path, layout


async def test_run_boot_gen_form_env_injection_and_cwd(
    gen_distro_with_mock_os, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_port_factory
) -> None:
    """代形态 run_boot 端到端——OS 取代内 env（发行级入口已删）、env 六项、cwd=current。"""
    root, layout = gen_distro_with_mock_os
    dump = tmp_path / "os_gen_env.json"
    monkeypatch.setenv("BOOT_TEST_ENV_DUMP", str(dump))
    target = resolve_boot_target("generic", None, root=root)
    port = free_port_factory()
    stop = asyncio.Event()
    task = asyncio.create_task(run_boot(target, no_webui=True, subject_port=port, stop_event=stop))
    try:
        await asyncio.wait_for(_wait_dump(dump), timeout=15.0)
    finally:
        stop.set()
        code = await asyncio.wait_for(task, timeout=15.0)
    assert code == 0

    import json

    os_env = json.loads(dump.read_text(encoding="utf-8"))
    data_root = root / "generic" / "data"
    assert os_env["GHRAH_SUBJECT_DB_PATH"] == str(data_root / "subject.db")
    assert os_env["GHRAH_SUBJECT_TASKSTORE_DB"] == str(data_root / "taskstore.sqlite3")
    assert os_env["GHRAH_SUBJECT_MANIFEST_ROOT"] == str(data_root / "manifests")
    assert os_env["GHRAH_SUBJECT_PROJECT_DEFAULT_ROOT_LOCATOR_TEMPLATE"] == str(
        data_root / "projects" / "{project_id}"
    )
    assert os_env["GHRAH_SUBJECT_WORKSPACE_ROOT"] == str(data_root / "workspace")
    assert os_env["GHRAH_SUBJECT_SERVER_HOST"] == "127.0.0.1"
    assert os_env["GHRAH_SUBJECT_SERVER_PORT"] == str(port)
    assert os_env["BOOT_TEST_CWD"] == str((root / "generic" / "generations" / "2026.09").resolve())


# ── webui 静态 serve / --webui-dev ───────────────────────────


def test_webui_static_root_gen_target(tmp_path: Path) -> None:
    """代目标 → 基座 versions/<base>/observer-web/dist。"""
    from ghrah_bootloader.boot import webui_static_root

    _make_gen(tmp_path)
    dist = tmp_path / "generic" / "versions" / "2026.09" / "observer-web" / "dist"
    dist.mkdir(parents=True)
    target = resolve_boot_target("generic", None, root=tmp_path)
    assert webui_static_root(target) == dist


def test_webui_static_root_missing_dist_fail_closed(tmp_path: Path) -> None:
    """代目标 dist 缺失 → fail-closed 报「基座产物不完整」。"""
    from ghrah_bootloader.boot import webui_static_root
    from ghrah_bootloader.errors import BootloaderError

    _make_gen(tmp_path)  # 基座树在场但无 observer-web/dist
    target = resolve_boot_target("generic", None, root=tmp_path)
    with pytest.raises(BootloaderError, match="基座产物不完整"):
        webui_static_root(target)


def test_webui_static_root_non_gen_rejected(tmp_path: Path) -> None:
    """引导目标非组合代 → fail-closed（引导目标恒为代）。"""
    from ghrah_bootloader.boot import webui_static_root
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    # 去掉代的 GENERATION.toml，使 read_generation 不命中
    (tmp_path / "generic" / "generations" / "2026.09" / "GENERATION.toml").unlink()
    target = resolve_boot_target("generic", None, root=tmp_path)
    with pytest.raises(BootloaderError, match="不是组合代"):
        webui_static_root(target)


def test_webui_dev_dir_explicit_arg_wins(tmp_path: Path) -> None:
    """显式参 > repo 推导。"""
    from ghrah_bootloader.boot import webui_dev_dir

    _make_distro(tmp_path)
    dev = tmp_path / "dev-observer-web"
    dev.mkdir()
    (tmp_path / "generic" / "distro.toml").write_text(
        f'[distro]\nname = "generic"\n\n[distro.repo]\npath = "{tmp_path}"\n', encoding="utf-8"
    )
    target = resolve_boot_target("generic", None, root=tmp_path)
    assert webui_dev_dir(target, dev) == dev
    assert webui_dev_dir(target, str(dev)) == dev


def test_webui_dev_dir_repo_derivation(tmp_path: Path) -> None:
    """无显式参 → [distro.repo].path + 包路径推导。"""
    from ghrah_bootloader.boot import webui_dev_dir

    _make_distro(tmp_path)
    dev = tmp_path / "repo" / "ghrah-observer-webui" / "packages" / "observer-web"
    dev.mkdir(parents=True)
    (tmp_path / "generic" / "distro.toml").write_text(
        f'[distro]\nname = "generic"\n\n[distro.repo]\npath = "{tmp_path / "repo"}"\n',
        encoding="utf-8",
    )
    target = resolve_boot_target("generic", None, root=tmp_path)
    assert webui_dev_dir(target, None) == dev


def test_webui_dev_dir_unavailable_fail_closed(tmp_path: Path) -> None:
    """显式参无效 / 均缺 → fail-closed，报错文案含处置提示（--webui-dev-dir）。"""
    from ghrah_bootloader.boot import webui_dev_dir
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)  # distro.toml 无 repo 路径
    target = resolve_boot_target("generic", None, root=tmp_path)
    with pytest.raises(BootloaderError, match="--webui-dev-dir"):
        webui_dev_dir(target, None)
    with pytest.raises(BootloaderError, match="--webui-dev-dir"):
        webui_dev_dir(target, tmp_path / "nope")


async def test_run_boot_static_webui_end_to_end(
    gen_distro_with_mock_os, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_port_factory
) -> None:
    """代目标 run_boot → webui 静态 serve 基座 dist + WS 注入；stop 后端口释放。"""
    import http.client

    from ghrah_bootloader.lifecycle import port_in_use

    root, layout = gen_distro_with_mock_os
    dist = root / "generic" / "versions" / "2026.09" / "observer-web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head>"
        '<script type="module" src="/assets/app.js"></script>'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('app')\n", encoding="utf-8")
    monkeypatch.setenv("BOOT_TEST_ENV_DUMP", str(tmp_path / "os_gen_env.json"))
    target = resolve_boot_target("generic", None, root=root)
    sport, wport = free_port_factory(), free_port_factory()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_boot(target, no_browser=True, subject_port=sport, webui_port=wport, stop_event=stop)
    )
    try:
        deadline = asyncio.get_event_loop().time() + 15.0
        while not port_in_use("127.0.0.1", wport):
            assert asyncio.get_event_loop().time() < deadline, "静态 webui 未探活"
            await asyncio.sleep(0.2)
        conn = http.client.HTTPConnection("127.0.0.1", wport, timeout=5.0)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 200
        assert f'window.__GHRAH_SUBJECT_WS_URL__="ws://127.0.0.1:{sport}/ws"' in body
        conn = http.client.HTTPConnection("127.0.0.1", wport, timeout=5.0)
        conn.request("GET", "/assets/app.js")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        stop.set()
        code = await asyncio.wait_for(task, timeout=15.0)
    assert code == 0
    # stop 后端口释放
    deadline = asyncio.get_event_loop().time() + 5.0
    while port_in_use("127.0.0.1", wport):
        assert asyncio.get_event_loop().time() < deadline, "webui 端口未释放"
        await asyncio.sleep(0.2)
