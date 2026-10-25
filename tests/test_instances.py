# SPDX-License-Identifier: Apache-2.0
"""真实子进程验证终端脱离、启动反馈、整组停止和 PID 复用保护。"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from ghrah_toolchain.errors import ToolchainError
from ghrah_toolchain.instances import (
    instance_status,
    read_logs,
    start_instance,
    stop_instance,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="真实 POSIX 终端脱离验证")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def installed(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GHRAH_HOME", str(tmp_path))
    root = tmp_path / "demo"
    generation = root / "generations" / "v1"
    bin_dir = generation / "env" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    server = bin_dir / "ghrah-subject-server"
    server.write_text(
        f"#!{sys.executable}\n"
        + """
import http.server, os
from pathlib import Path
Path("subject.pid").write_text(str(os.getpid()))
print("subject-output-visible", flush=True)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{}')
port = int(os.environ["GHRAH_SUBJECT_SERVER_PORT"])
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""
    )
    server.chmod(0o755)
    (generation / "GENERATION.toml").write_text("""[generation]
schema_version = 1
base_version = "base"
base_commit = "test"
pins_sha256 = "test"
""")
    dist = root / "versions" / "base" / "observer-web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>webui</body></html>")
    (root / "current").symlink_to(generation)
    (root / "distro.toml").write_text(
        f"[runtime]\nsubject_port = {_port()}\nwebui_port = {_port()}\n"
    )
    yield root
    stop_instance("demo", timeout=10)


def _until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("等待实例状态超时")
        time.sleep(0.05)


def test_shell_exits_without_orphaning_subject(installed: Path) -> None:
    # capture_output 的管道也必须立即关闭，不能被后台监督者／Subject 继承。
    command = f"{shlex.quote(sys.executable)} -m ghrah_toolchain start demo --json"
    completed = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=8, check=False
    )
    assert completed.returncode == 0, completed.stderr
    state = json.loads(completed.stdout)["data"]
    assert state["state"] in {"starting", "running"}
    supervisor = psutil.Process(state["pid"])
    _until(lambda: instance_status("demo")["state"] == "running")
    child = psutil.Process(int((installed / "current" / "subject.pid").read_text()))
    assert child.ppid() == supervisor.pid
    assert os.getsid(supervisor.pid) == supervisor.pid
    assert "subject-output-visible" in read_logs("demo")
    again = start_instance("demo")
    assert again["pid"] == supervisor.pid
    assert stop_instance("demo", timeout=10) == 0
    assert stop_instance("demo") == 0
    assert instance_status("demo")["state"] == "stopped"
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    assert not (installed / ".boot.pid").exists()


def test_grouped_status_restart_and_log_follow(installed: Path, tmp_path: Path) -> None:
    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "ghrah_toolchain", "distro", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["data"]

    first = cli("start", "demo", "--json")
    _until(lambda: instance_status("demo")["state"] == "running")
    status = cli("status", "demo", "--json")
    assert status["pid"] == first["pid"]
    captured = tmp_path / "follow.txt"
    with captured.open("w") as output:
        follower = subprocess.Popen(
            [sys.executable, "-m", "ghrah_toolchain", "distro", "logs", "demo", "--follow"],
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _until(lambda: "subject-output-visible" in captured.read_text())
            restarted = cli("restart", "demo", "--json")
            assert restarted["pid"] != first["pid"]
            _until(lambda: captured.read_text().count("subject-output-visible") >= 2)
            _until(lambda: instance_status("demo")["state"] == "running")
            follower.send_signal(signal.SIGINT)
            _, error = follower.communicate(timeout=5)
            assert follower.returncode == 0, error
            assert instance_status("demo")["state"] == "running"
        finally:
            if follower.poll() is None:
                follower.kill()
                follower.communicate(timeout=5)


def test_start_returns_before_health_and_can_stop_while_starting(installed: Path) -> None:
    server = installed / "current" / "env" / ".venv" / "bin" / "ghrah-subject-server"
    server.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    begin = time.monotonic()
    state = start_instance("demo", no_webui=True)
    assert time.monotonic() - begin < 5
    assert state["state"] == "starting"
    _until(lambda: (installed / ".boot.pid").exists())
    assert stop_instance("demo", timeout=10) == 0
    assert instance_status("demo")["state"] == "stopped"


def test_failed_start_has_state_and_log(installed: Path) -> None:
    server = installed / "current" / "env" / ".venv" / "bin" / "ghrah-subject-server"
    server.unlink()
    state = start_instance("demo", no_webui=True)
    _until(lambda: instance_status("demo")["state"] == "failed")
    assert "OS server 入口缺失" in read_logs("demo")
    assert state["pid"] > 0


def test_supervisor_hangup_closes_subject(installed: Path) -> None:
    state = start_instance("demo", no_webui=True)
    _until(lambda: instance_status("demo")["state"] == "running")
    child = psutil.Process(int((installed / "current" / "subject.pid").read_text()))
    os.kill(state["pid"], signal.SIGHUP)
    _until(lambda: instance_status("demo")["state"] == "stopped")
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE


def test_simultaneous_start_has_one_supervisor(installed: Path) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start_instance, "demo", no_webui=True)
        second = pool.submit(start_instance, "demo", no_webui=True)
        assert first.result()["pid"] == second.result()["pid"]
    _until(lambda: instance_status("demo")["state"] == "running")


def test_reused_pid_is_not_signalled(installed: Path) -> None:
    record = {
        "schema": 1,
        "token": "a" * 32,
        "pid": os.getpid(),
        "created": psutil.Process().create_time() - 1,
        "generation": "v1",
        "state": "running",
    }
    (installed / ".session.json").write_text(json.dumps(record))
    assert instance_status("demo")["state"] == "failed"
    assert stop_instance("demo") == 0


def test_log_token_cannot_escape(installed: Path) -> None:
    record = {
        "schema": 1,
        "token": "../../other",
        "pid": os.getpid(),
        "created": psutil.Process().create_time(),
        "generation": "v1",
        "state": "running",
    }
    (installed / ".session.json").write_text(json.dumps(record))
    with pytest.raises(ToolchainError, match="记录损坏"):
        read_logs("demo")
    (installed / ".session.json").unlink()
