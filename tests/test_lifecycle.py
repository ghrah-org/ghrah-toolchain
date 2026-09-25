# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""进程生命周期测试。

覆盖：
- 端口探测（loopback 双栈）
- 进程组终止（SIGTERM→SIGKILL 升级 / 组已消失容错）
- webui env 文件生成/清理
- stop() 幂等与端口复核
- OS 子进程 spawn/收束（新增：out-of-process 语义）
- wait_for_server（stdlib urllib 轮询）
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ghrah_bootloader.lifecycle import (
    _WEBUI_ENV_FILE,
    _WEBUI_ENV_KEY,
    BootStack,
    StackConfig,
    port_in_use,
    terminate_process,
)


def test_config_default_ports() -> None:
    c = StackConfig()
    assert c.subject_port == 4112
    assert c.webui_port == 5173
    assert c.host == "127.0.0.1"


def test_config_url_helpers() -> None:
    c = StackConfig(host="127.0.0.1", subject_port=4112, webui_port=5173)
    assert c.subject_http_url() == "http://127.0.0.1:4112/health"
    assert c.subject_ws_url() == "ws://127.0.0.1:4112/ws"
    assert c.webui_url() == "http://localhost:5173"


def test_custom_port_subject_ws_url() -> None:
    c = StackConfig(host="127.0.0.1", subject_port=4222)
    assert c.subject_ws_url() == "ws://127.0.0.1:4222/ws"


# ── 端口探测（loopback 双栈） ────────────────────────────────


def test_port_in_use_detects_ipv4_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        busy_port = srv.getsockname()[1]
        assert port_in_use("127.0.0.1", busy_port) is True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert port_in_use("127.0.0.1", free_port) is False


def test_port_in_use_detects_ipv6_only_listener() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as srv:
        srv.bind(("::1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert port_in_use("localhost", port) is True
        assert port_in_use("127.0.0.1", port) is True


# ── 进程组终止（POSIX） ──────────────────────────────────────


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_posix_group_sigterm_then_sigkill(monkeypatch) -> None:
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None  # 进程仍在运行
    proc.wait.side_effect = [subprocess.TimeoutExpired("pnpm", 5), 0]
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)  # 自建组组长

    terminate_process(proc)

    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert proc.wait.call_count == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_posix_sigterm_sufficient(monkeypatch) -> None:
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    proc.wait.return_value = 0
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    terminate_process(proc)

    assert kills == [(4242, signal.SIGTERM)]
    proc.wait.assert_called_once()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_posix_group_gone_is_tolerated(monkeypatch) -> None:
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    proc.wait.return_value = 0

    def _killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr(os, "killpg", _killpg)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    terminate_process(proc)  # 不抛
    proc.wait.assert_called_once()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_already_exited_sends_no_signal(monkeypatch) -> None:
    """进程已退出（poll 有返回值）→ 不发任何信号（防 pid 回收误伤）。"""
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 0
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))

    terminate_process(proc)

    assert kills == []
    proc.wait.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_non_leader_falls_back_to_terminate(monkeypatch) -> None:
    """非组长（getpgid != pid）→ 回退单进程 terminate，不对组发信号。"""
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    proc.wait.return_value = 0
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: 1000)  # 组长另有其人

    terminate_process(proc)

    assert kills == []
    proc.terminate.assert_called_once()


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组终止路径")
def test_terminate_process_real_process_group() -> None:
    """真实子进程组终止：SIGTERM 应收敛整个组（sh 包装的 sleep 子进程）。"""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    terminate_process(proc)
    assert proc.poll() is not None
    # 进程组应已消失
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


# ── webui env 文件 ───────────────────────────────────────────


def test_write_and_cleanup_env_file(tmp_path: Path) -> None:
    stack = BootStack(StackConfig(open_browser=False), webui_dir=tmp_path)
    stack._write_webui_env_file(tmp_path)
    env_path = tmp_path / _WEBUI_ENV_FILE
    assert env_path.is_file()
    content = env_path.read_text(encoding="utf-8")
    assert _WEBUI_ENV_KEY in content
    assert "ws://127.0.0.1:4112/ws" in content
    assert stack._webui_env_file == env_path

    stack._cleanup_webui_env_file()
    assert not env_path.exists()
    assert stack._webui_env_file is None
    stack._cleanup_webui_env_file()  # 幂等


def test_cleanup_env_file_missing_is_noop(tmp_path: Path) -> None:
    stack = BootStack(StackConfig(open_browser=False))
    env_path = tmp_path / _WEBUI_ENV_FILE
    stack._webui_env_file = env_path
    env_path.unlink(missing_ok=True)
    stack._cleanup_webui_env_file()  # 不抛
    assert stack._webui_env_file is None


def test_ensure_pnpm_missing_raises() -> None:
    stack = BootStack(StackConfig(open_browser=False))
    with patch("ghrah_bootloader.lifecycle.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="pnpm"):
            stack._ensure_pnpm()


def test_webui_start_missing_dir_raises(tmp_path: Path) -> None:
    stack = BootStack(StackConfig(open_browser=False), webui_dir=tmp_path / "nope")
    with patch("ghrah_bootloader.lifecycle.shutil.which", return_value="/usr/bin/pnpm"):
        with pytest.raises(RuntimeError, match="observer-web package not found"):
            stack._start_webui()


# ── stop() 幂等与端口复核 ────────────────────────────────────


async def test_stop_without_start_is_idempotent() -> None:
    stack = BootStack(StackConfig(open_browser=False))
    await stack.stop()
    await stack.stop()
    assert stack._started is False


async def test_stop_terminates_webui_then_rechecks_port() -> None:
    from unittest.mock import AsyncMock

    stack = BootStack(StackConfig(open_browser=False), webui_dir=Path("/tmp"))
    fake_proc = MagicMock()
    stack._webui_process = fake_proc
    with (
        patch.object(BootStack, "_wait_webui_port_released", new_callable=AsyncMock) as recheck,
    ):
        await stack.stop()
    recheck.assert_awaited_once()
    assert stack._webui_process is None


async def test_stop_without_webui_skips_port_recheck() -> None:
    from unittest.mock import AsyncMock

    stack = BootStack(StackConfig(open_browser=False))
    with patch.object(BootStack, "_wait_webui_port_released", new_callable=AsyncMock) as recheck:
        await stack.stop()
    recheck.assert_not_awaited()


async def test_wait_webui_port_released_warns_when_still_busy(caplog) -> None:
    stack = BootStack(StackConfig(open_browser=False))
    with patch("ghrah_bootloader.lifecycle.port_in_use", return_value=True):
        with caplog.at_level(logging.WARNING, logger="ghrah_bootloader.lifecycle"):
            await stack._wait_webui_port_released(timeout=0.3)
    assert any("still in use" in r.message for r in caplog.records)


async def test_wait_webui_port_released_quick_when_free() -> None:
    stack = BootStack(StackConfig(open_browser=False))
    with patch("ghrah_bootloader.lifecycle.port_in_use", return_value=False):
        await asyncio.wait_for(stack._wait_webui_port_released(timeout=5.0), timeout=1.0)


# ── OS 子进程 spawn/收束（out-of-process 新增用例） ──────────


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 父类签名
        del format, args


def _start_health_server(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def test_start_os_subprocess_lifecycle() -> None:
    """完整 OS 子进程生命周期：spawn → /health 就绪 → stop() 收束。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 起一个真实 http 服务模拟 OS 健康端点（脚本内 threading）
    script = (
        "import threading,time\n"
        "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
        f"port={port}\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'{}')\n"
        "    def log_message(self,*a): pass\n"
        "srv=HTTPServer(('127.0.0.1',port),H)\n"
        "t=threading.Thread(target=srv.serve_forever,daemon=True)\n"
        "t.start()\n"
        "time.sleep(60)\n"
    )
    stack = BootStack(
        StackConfig(open_browser=False, subject_port=port),
        os_command=[sys.executable, "-c", script],
    )
    await stack.start()
    try:
        assert stack.os_process is not None
        assert stack.os_process.poll() is None  # 独立进程仍在运行
    finally:
        await stack.stop()
    assert stack.os_process is None


async def test_start_os_health_check_failure_raises() -> None:
    """OS 子进程健康检查超时应抛 TimeoutError，且进程由调用方 stop 收束。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stack = BootStack(
        StackConfig(open_browser=False, subject_port=port),
        os_command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    with pytest.raises(TimeoutError):
        await stack.start()
    assert stack._started is False
    # 失败路径仍需可收束（start 抛异常后调用方应 stop）
    await stack.stop()
    assert stack.os_process is None


async def test_start_os_requires_command() -> None:
    stack = BootStack(StackConfig(open_browser=False))
    with pytest.raises(RuntimeError, match="os_command"):
        await stack.start()


async def test_start_os_port_in_use_raises() -> None:
    server = _start_health_server(0)
    port = server.server_address[1]
    try:
        stack = BootStack(
            StackConfig(open_browser=False, subject_port=port),
            os_command=[sys.executable, "-c", "pass"],
        )
        with pytest.raises(RuntimeError, match="already in use"):
            await stack.start()
    finally:
        server.shutdown()
        server.server_close()


# ── wait_for_server（stdlib urllib 轮询） ────────────────────


async def test_wait_for_server_success() -> None:
    from ghrah_bootloader.health import wait_for_server

    server = _start_health_server(0)
    port = server.server_address[1]
    try:
        data = await wait_for_server(f"http://127.0.0.1:{port}/health", timeout=5.0, name="test")
        assert data == {"status": "ok"}
    finally:
        server.shutdown()
        server.server_close()


async def test_wait_for_server_timeout() -> None:
    from ghrah_bootloader.health import wait_for_server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TimeoutError, match="did not become ready"):
        await wait_for_server(f"http://127.0.0.1:{port}/health", timeout=0.5, name="test")


# ── C4/W4：pnpm 调用统一（平台模拟） ─────────────────────────


def test_start_webui_nt_uses_cmd_c_pnpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """nt：命令为 cmd /c pnpm dev …，creationflags 含 CREATE_NEW_PROCESS_GROUP。"""
    from ghrah_bootloader import lifecycle as lc

    class _OsProxy:
        def __init__(self) -> None:
            import os as real_os

            object.__setattr__(self, "_real", real_os)

        def __getattr__(self, item):
            return getattr(object.__getattribute__(self, "_real"), item)

        @property
        def name(self) -> str:
            return "nt"

    webui = tmp_path / "observer-web"
    webui.mkdir()
    popen_calls: list[dict] = []

    class _FakeProc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        popen_calls.append({"cmd": list(cmd), **kw})
        return _FakeProc()

    monkeypatch.setattr(lc, "os", _OsProxy(), raising=False)
    # pnpm_command() 读 materialize 模块内的 os.name——同需代理
    from ghrah_bootloader import materialize as mat

    monkeypatch.setattr(mat, "os", _OsProxy(), raising=False)
    # CREATE_NEW_PROCESS_GROUP 是 Windows-only subprocess 属性（真实 nt 恒在；
    # Linux 模拟环境注入常量值以保证代码路径可达）
    monkeypatch.setattr(lc.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(lc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(lc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lc, "port_in_use", lambda host, port: False)
    stack = lc.BootStack(lc.StackConfig(open_browser=False), webui_dir=webui)
    stack._start_webui()
    assert len(popen_calls) == 1
    cmd = popen_calls[0]["cmd"]
    assert cmd[:3] == ["cmd", "/c", "pnpm"]  # pnpm_command() 权威形态
    assert cmd[3:] == ["dev", "--port", str(stack.config.webui_port), "--strictPort"]
    assert popen_calls[0]["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP


def test_start_webui_posix_uses_bare_pnpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """posix：命令为裸 pnpm dev …，start_new_session=True。"""
    from ghrah_bootloader import lifecycle as lc

    webui = tmp_path / "observer-web"
    webui.mkdir()
    popen_calls: list[dict] = []

    class _FakeProc:
        pid = 4322

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        popen_calls.append({"cmd": list(cmd), **kw})
        return _FakeProc()

    monkeypatch.setattr(lc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(lc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lc, "port_in_use", lambda host, port: False)
    stack = lc.BootStack(lc.StackConfig(open_browser=False), webui_dir=webui)
    stack._start_webui()
    assert len(popen_calls) == 1
    cmd = popen_calls[0]["cmd"]
    assert cmd == ["pnpm", "dev", "--port", str(stack.config.webui_port), "--strictPort"]
    assert popen_calls[0]["start_new_session"] is True
