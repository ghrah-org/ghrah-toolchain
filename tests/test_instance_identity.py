# SPDX-License-Identifier: Apache-2.0
"""跨平台回归：实例记录里的 pid 必须归属真正运行监督逻辑的进程。

Windows 上 ``venv\\Scripts\\python.exe`` 是 ``venvlauncher``，console-script
入口也是 trampoline：``subprocess.Popen`` 返回的是启动器 pid，真正 import 并
执行 ``ghrah_toolchain.supervisor`` 的是它的子进程。父进程无法预知那个子进程
pid，所以记录只能由持 token 的监督进程自注册。

本模块不依赖端口监听，也不触碰 POSIX 专有语义（进程组 / SIGHUP / ``/bin/sh``），
可在 Linux、macOS、Windows 上同形运行。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from ghrah_toolchain.boot import os_server_entry
from ghrah_toolchain.errors import ToolchainError
from ghrah_toolchain.instances import (
    _claim_record,
    _process,
    _write,
    instance_status,
    start_instance,
    stop_instance,
)
from ghrah_toolchain.runtime_ref import repoint_current


def _port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _install_stub_subject(generation: Path) -> None:
    """放一个「起得来但不监听端口」的后端入口：只为让监督进程走到 ``.boot.pid``。"""
    entry = os_server_entry(generation)
    entry.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # 入口必须是真实 PE：拷一份解释器，无参启动且 stdin 已 EOF → 立即退出。
        shutil.copy(sys.executable, entry)
    else:
        entry.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
        entry.chmod(0o755)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GHRAH_HOME", str(tmp_path))
    root = tmp_path / "demo"
    generation = root / "generations" / "v1"
    generation.mkdir(parents=True)
    (root / "distro.toml").write_text(
        f"[runtime]\nsubject_port = {_port()}\nwebui_port = {_port()}\n", encoding="utf-8"
    )
    repoint_current(root, "v1")
    _install_stub_subject(generation)
    yield root
    # 合成记录可能故意指向本进程（pid 复用语义）；此时绝不能对它发停止信号。
    try:
        record = json.loads((root / ".session.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        record = None
    if record is None or record.get("pid") != os.getpid():
        with contextlib.suppress(ToolchainError):
            stop_instance("demo", timeout=10)
    # Windows 上启动器父进程在监督进程退出后才收尾，留一点时间释放日志句柄。
    time.sleep(0.5)


def _until(predicate, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("等待监督进程写入 .boot.pid 超时")
        time.sleep(0.05)


def test_recorded_pid_is_the_supervisor_process(installed: Path) -> None:
    """记录 + 状态里的 pid 必须是监督进程本身，而不是拉起它的启动器。"""
    start_instance("demo", no_webui=True)
    _until(lambda: (installed / ".boot.pid").is_file())

    # .boot.pid 由监督进程用 os.getpid() 写入，是唯一不依赖父进程猜测的锚点。
    supervisor_pid = int((installed / ".boot.pid").read_text(encoding="utf-8").strip())
    status = instance_status("demo")

    assert status["state"] == "starting"
    assert status["pid"] == supervisor_pid, (
        "实例记录跟踪的不是真正运行监督进程的 pid"
        f"（记录: {status['pid']}；监督进程: {supervisor_pid}）"
    )
    assert stop_instance("demo", timeout=10) == 0


def test_claim_moves_pid_and_create_time_to_the_owner(installed: Path) -> None:
    """自注册要同时更新 create_time：只改 pid 会让后续身份复核失败。"""
    token = "a" * 32
    _write(
        installed,
        {
            "schema": 1,
            "token": token,
            "pid": os.getpid(),
            "created": 1.0,  # 故意写成「显然不是归属进程」的时间
            "state": "starting",
            "generation": "v1",
        },
    )
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        record = _claim_record(installed, token, pid=owner.pid)
        assert record["pid"] == owner.pid
        assert record["created"] == psutil.Process(owner.pid).create_time()
        assert _process(record) is not None, "自注册后的身份复核必须能认出归属进程"
    finally:
        owner.kill()
        owner.wait(timeout=10)


def test_claim_rejects_foreign_token(installed: Path) -> None:
    """token 仍是唯一身份凭据：pid 自注册没有放宽这道门。"""
    _write(
        installed,
        {
            "schema": 1,
            "token": "a" * 32,
            "pid": os.getpid(),
            "created": psutil.Process().create_time(),
            "state": "starting",
            "generation": "v1",
        },
    )
    with pytest.raises(ToolchainError, match="不匹配"):
        _claim_record(installed, "b" * 32)


def test_claim_does_not_rewrite_when_pid_already_matches(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX 路径保持零行为变化：pid 本就归属当前进程时不得多写一次记录。"""
    token = "a" * 32
    _write(
        installed,
        {
            "schema": 1,
            "token": token,
            "pid": os.getpid(),
            "created": psutil.Process().create_time(),
            "state": "starting",
            "generation": "v1",
        },
    )
    writes: list[tuple] = []
    monkeypatch.setattr(
        "ghrah_toolchain.instances._write", lambda *args, **kwargs: writes.append(args)
    )

    record = _claim_record(installed, token)

    assert record["pid"] == os.getpid()
    assert writes == [], "pid 已一致时不应产生额外写入"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 控制台语义")
def test_supervisor_spawn_has_no_console_window(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """监督者用「不可见独立控制台」启动。

    ``DETACHED_PROCESS`` 会让 Win10+ 为脱离的 console 进程拉起 ConPTY
    （conhost 的 ``PseudoConsoleWindow``），桌面上就是一个空的命令框。
    """
    captured: dict = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("停在真正的 spawn 之前")

    monkeypatch.setattr("ghrah_toolchain.instances.subprocess.Popen", fake_popen)
    with pytest.raises(RuntimeError):
        start_instance("demo", no_webui=True)

    flags = captured["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & subprocess.DETACHED_PROCESS
