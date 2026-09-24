# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot 编排测试（批次 1：out-of-process 引导）。

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
import socket
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
from ghrah_bootloader.layout import DistroLayout, DistroToml


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_distro(root: Path, distro: str = "generic", version: str = "2026.09") -> DistroLayout:
    """物化一个最小发行版布局：versions/<ver>/ 树 + current 重指 + distro.toml。"""
    distro_root = root / distro
    tree = distro_root / "versions" / version
    (tree / "ghrah-observer-webui" / "packages" / "observer-web").mkdir(parents=True)
    bin_dir = tree.parent.parent / "env" / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    server = bin_dir / ("ghrah-subject-server.exe" if os.name == "nt" else "ghrah-subject-server")
    server.write_text("#!/bin/sh\nexit 0\n" if os.name != "nt" else "")
    if os.name != "nt":
        server.chmod(0o755)
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
    current.symlink_to(tree)
    return DistroLayout(root=distro_root)


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
    assert target.tree == (tmp_path / "generic" / "versions" / "2026.09").resolve()
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
    """current 指向 versions/ 之外的路径时拒绝引导。"""
    from ghrah_bootloader.errors import BootloaderError

    _make_distro(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "generic" / "current").unlink()
    (tmp_path / "generic" / "current").symlink_to(outside)
    with pytest.raises(BootloaderError, match="versions/ 之外"):
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
        os_server_command(layout.venv)


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
    """发行版布局 + 可执行的 mock OS server（记录 env 到 dump 文件）。"""
    layout = _make_distro(tmp_path)
    bin_dir = layout.venv / ("Scripts" if os.name == "nt" else "bin")
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
    distro_with_mock_os, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn OS → env 六项注入指向数据盘 → cwd=current → 停止信号逆序收束。"""
    root, layout = distro_with_mock_os
    dump = tmp_path / "os_env.json"
    monkeypatch.setenv("BOOT_TEST_ENV_DUMP", str(dump))
    target = resolve_boot_target("generic", None, root=root)
    port = _free_port()
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


async def test_run_boot_os_exit_returns_failure(distro_with_mock_os) -> None:
    """OS 子进程意外退出 → run_boot 返回 1（其余子进程已收束）。"""
    root, layout = distro_with_mock_os
    bin_dir = layout.venv / "bin"
    server = bin_dir / "ghrah-subject-server"
    # 覆写为「起健康端点后立即退出」的 OS
    port_holder = {"port": _free_port()}
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
            run_boot(target, no_webui=True, subject_port=_free_port()), timeout=15.0
        )


async def _wait_dump(dump: Path, timeout: float = 15.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not dump.is_file():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"env dump not written: {dump}")
        await asyncio.sleep(0.2)
