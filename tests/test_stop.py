# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""boot pidfile 与 stop 命令测试：双启守卫、优雅停止、陈旧回收、误杀护栏。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ghrah_bootloader.boot import (
    _clear_boot_pid,
    _write_boot_pid,
    boot_pid_path,
    stop_boot_session,
)
from ghrah_bootloader.errors import BootloaderError

_SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "ghrah-home"
    monkeypatch.setenv("GHRAH_HOME", str(h))
    h.mkdir(parents=True)
    return h


def _distro_root(home: Path) -> Path:
    d = home / "generic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_pidfile_write_and_clear(home: Path) -> None:
    root = _distro_root(home)
    _write_boot_pid(root)
    pidfile = boot_pid_path(root)
    assert pidfile.is_file()
    assert int(pidfile.read_text(encoding="utf-8").strip()) == os.getpid()
    _clear_boot_pid(root)
    assert not pidfile.exists()


def test_pidfile_rejects_double_boot(home: Path) -> None:
    root = _distro_root(home)
    _write_boot_pid(root)  # 本进程即"活 pid"
    with pytest.raises(BootloaderError, match="已有 boot 会话"):
        _write_boot_pid(root)
    _clear_boot_pid(root)


def test_pidfile_recycles_stale(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")  # 几乎不可能存活的 pid
    _write_boot_pid(root)
    assert int(pidfile.read_text(encoding="utf-8").strip()) == os.getpid()
    _clear_boot_pid(root)


def test_clear_pidfile_ignores_foreign_pid(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")
    _clear_boot_pid(root)  # 非 本进程 pid：不动
    assert pidfile.read_text(encoding="utf-8").strip() == "999999999"


# ── stop_boot_session ────────────────────────────────────────

_FAKE_BOOT_CODE = """
import signal, sys, time
stopped = {"v": False}
def on_term(sig, frame):
    stopped["v"] = True
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while not stopped["v"]:
    time.sleep(0.05)
"""


def _spawn_fake_boot(home: Path, name: str = "ghrah-bootloader") -> subprocess.Popen:
    """spawn 一个 argv[0] 伪装为 ghrah-bootloader 的子进程（装 SIGTERM handler）。

    经 home 下的符号链接 <name> → sys.executable 实现（execv 需要绝对路径，
    且 argv[0] 由调用方指定）。
    """
    if os.name == "posix":
        exe = home / name
        exe.symlink_to(sys.executable)
        argv = [str(exe), "-c", _FAKE_BOOT_CODE]
    else:
        argv = [sys.executable, "-c", _FAKE_BOOT_CODE]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "GHRAH_HOME": str(home)},
        cwd=home,
    )
    assert proc.stdout is not None
    proc.stdout.readline()  # 等 ready
    return proc


def test_stop_terminates_fake_boot(home: Path) -> None:
    root = _distro_root(home)
    proc = _spawn_fake_boot(home)
    boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
    rc = stop_boot_session("generic", timeout=10)
    proc.wait(timeout=10)
    assert rc == 0
    assert proc.returncode == 0


def test_stop_dead_pid_cleans_pidfile(home: Path) -> None:
    root = _distro_root(home)
    pidfile = boot_pid_path(root)
    pidfile.write_text("999999999\n", encoding="utf-8")
    rc = stop_boot_session("generic")
    assert rc == 1
    assert not pidfile.exists()


def test_stop_without_pidfile(home: Path) -> None:
    _distro_root(home)
    rc = stop_boot_session("generic")
    assert rc == 1


@pytest.mark.skipif(os.name != "posix", reason="cmdline 校验仅 POSIX")
def test_stop_refuses_foreign_process(home: Path) -> None:
    root = _distro_root(home)
    proc = _spawn_fake_boot(home, name="definitely-not-bootloader")
    try:
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=5)
        assert rc == 1
        assert proc.poll() is None  # 未被杀
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="无 /proc 的 posix 模拟需 posix 平台")
def test_stop_without_proc_skips_guard(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """macOS 回归位：/proc 不存在时护栏跳过、stop 不崩溃、继续 SIGTERM。"""
    from ghrah_bootloader import boot as boot_mod

    monkeypatch.setattr(boot_mod.Path, "is_dir", lambda self: False)
    proc = _spawn_fake_boot(home)
    try:
        root = _distro_root(home)
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=10)
        proc.wait(timeout=10)
        assert rc == 0
        assert proc.returncode == 0
        out = capsys.readouterr().out
        assert "跳过 cmdline 护栏" in out
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_stop_timeout_reports_stuck(home: Path) -> None:
    root = _distro_root(home)
    # spawn 一个忽略 SIGTERM 的伪装进程 → 超时路径
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    if os.name == "posix":
        exe = home / "ghrah-bootloader"
        exe.symlink_to(sys.executable)
        argv = [str(exe), "-c", code]
    else:
        argv = [sys.executable, "-c", code]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "GHRAH_HOME": str(home)},
        cwd=home,
    )
    assert proc.stdout is not None
    proc.stdout.readline()
    try:
        boot_pid_path(root).write_text(f"{proc.pid}\n", encoding="utf-8")
        rc = stop_boot_session("generic", timeout=1.0)
        assert rc == 2
        assert proc.poll() is None  # 不强杀
    finally:
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.1)
