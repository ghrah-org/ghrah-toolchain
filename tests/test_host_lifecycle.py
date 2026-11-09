# SPDX-License-Identifier: Apache-2.0
"""无控制台 Windows 宿主通过受信文件和会话记录优雅收束。"""

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ghrah_toolchain import dev, instances, lifecycle
from ghrah_toolchain.boot import BootTarget
from ghrah_toolchain.layout import DistroLayout, DistroToml


async def test_host_control_file_is_private_and_removed_after_stop(tmp_path, monkeypatch):
    control = {}
    process = Mock()
    process.poll.return_value = None

    def popen(command, **options):
        control.update(options)
        return process

    def wait(**_):
        assert Path(control["env"]["GHRAH_HOST_STOP_FILE"]).is_file()
        process.poll.return_value = 0
        return 0

    process.wait.side_effect = wait
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)
    monkeypatch.setattr(lifecycle, "wait_for_server", AsyncMock())
    monkeypatch.setattr(lifecycle, "port_in_use", lambda *_: False)
    stack = lifecycle.BootStack(
        lifecycle.StackConfig(open_browser=False),
        os_command=[str(tmp_path / "ghrah-subject-host.exe")],
        os_cwd=tmp_path,
    )
    await stack.start()
    stop_file = Path(control["env"]["GHRAH_HOST_STOP_FILE"])
    assert stop_file.parent.is_dir() and not stop_file.exists()
    if os.name == "nt":
        assert control["creationflags"] == lifecycle.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert control["start_new_session"] is True
    await stack.stop()
    await stack.stop()
    assert not stop_file.parent.exists()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_background_supervisor_observes_stopping_record_without_console(tmp_path, monkeypatch):
    record = {"token": "test", "pid": os.getpid(), "generation": "g1", "state": "running"}
    monkeypatch.setattr(instances, "resolve_distro_root", lambda _: tmp_path)
    monkeypatch.setattr(instances, "_locked", lambda _: contextlib.nullcontext())
    monkeypatch.setattr(instances, "_read", lambda _: dict(record))
    monkeypatch.setattr(instances, "resolve_boot_target", lambda *_: object())
    transitions = []
    monkeypatch.setattr(instances, "_transition", lambda *args: transitions.append(args))

    async def boot(_target, *, stop_event, **_):
        record["state"] = "stopping"
        await asyncio.wait_for(stop_event.wait(), 1)
        return 0

    monkeypatch.setattr(instances, "run_boot", boot)
    assert instances.serve_instance("test", "test", no_webui=True) == 0
    assert transitions[-1][2] == "stopped"


def test_windows_stop_waits_for_supervisor_instead_of_taskkill(tmp_path, monkeypatch):
    record = {"token": "test", "pid": 123, "state": "running"}
    process = Mock()
    monkeypatch.setattr(instances, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(instances, "resolve_distro_root", lambda _: tmp_path)
    monkeypatch.setattr(instances, "_locked", lambda _: contextlib.nullcontext())
    monkeypatch.setattr(instances, "_read", lambda _: dict(record))
    monkeypatch.setattr(instances, "_write", lambda _, value: record.update(value))
    monkeypatch.setattr(
        instances, "_process", lambda _: process if record["state"] == "running" else None
    )
    monkeypatch.setattr(instances, "_busy_ports", lambda *_: [])
    assert instances.stop_instance("test") == 0
    assert record["state"] == "stopped"
    process.send_signal.assert_not_called()


async def test_dev_injects_trusted_roots_and_runtime_environment(tmp_path, monkeypatch):
    layout = DistroLayout(tmp_path / "instance")
    configuration = DistroToml(environment={"GHRAH_SUBJECT_LOG_LEVEL": "DEBUG"})
    target = BootTarget("instance", "g1", layout.current, layout, configuration)
    context = dev.DevContext(target, tmp_path / "repo")
    options = {}

    class Stack:
        os_process = SimpleNamespace(poll=lambda: 0, returncode=0)

        def __init__(self, config, **kwargs):
            options.update(kwargs)

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(dev, "BootStack", Stack)
    monkeypatch.setattr(dev, "os_server_command", lambda _: ["ghrah-subject-host"])
    monkeypatch.setattr(dev, "_dev_webui_root", lambda _: tmp_path)
    assert await dev._serve_dev(context, open_browser=False) == 1
    environment = options["os_env"]
    assert environment["GHRAH_DATA_ROOT"] == str(layout.data)
    assert environment["GHRAH_DISTRO_ROOT"] == str(layout.root)
    assert environment["GHRAH_GENERATION_ID"] == ".dev/gen"
    assert environment["GHRAH_SUBJECT_LOG_LEVEL"] == "DEBUG"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 子进程等待回收状态")
def test_dev_stop_treats_exited_child_awaiting_reap_as_stopped(tmp_path):
    layout = DistroLayout(tmp_path / "instance")
    target = BootTarget("instance", "g1", layout.current, layout, DistroToml())
    context = dev.DevContext(target, tmp_path / "repo")
    context.dev_root.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (context.dev_root / dev.DEV_PID).write_text(str(process.pid), encoding="utf-8")
    try:
        assert dev._stop_dev_session(context)
        # 直到这里才 wait：stop 必须能识别进程已退出，而不依赖父进程先 reap。
        assert process.wait(timeout=3) == -15
        assert not (context.dev_root / dev.DEV_PID).exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
