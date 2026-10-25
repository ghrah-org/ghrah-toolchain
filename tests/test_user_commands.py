# SPDX-License-Identifier: Apache-2.0
"""用户工作流：真实 Git 与磁盘状态，构建边界使用已有隔离产物夹具。"""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

import psutil
import pytest
import test_install_packages as install_support

from ghrah_toolchain.cli import _build_parser, main
from ghrah_toolchain.errors import RuntimeRefError
from ghrah_toolchain.layout import BootToml, DistroToml
from ghrah_toolchain.runtime_ref import RuntimeRefManager, _lock_is_stale

home = install_support.home
repo = install_support.repo
fake_build = install_support.fake_build


def test_install_check_update_and_config_workflow(home, repo, fake_build, capsys):
    assert main(["distro", "install", "demo", "--from", str(repo), "--json"]) == 0
    installed = json.loads(capsys.readouterr().out)["data"]
    assert installed["started"] is False
    assert DistroToml.load(home / "demo").repo_path == str(repo)
    data = home / "demo" / "data"
    data.mkdir()
    (data / "task.txt").write_text("preserved")
    before_current = (home / "demo" / "current").readlink()
    candidate = install_support._commit(repo, "b", "2026-10-01T10:00:00 +0000")
    assert main(["update", "demo", "--check", "--json"]) == 0
    check = json.loads(capsys.readouterr().out)["data"]
    assert check["update_available"] and check["candidate"] == candidate
    assert (home / "demo" / "current").readlink() == before_current
    assert len(fake_build.calls["build"]) == 1
    assert main(["distro", "update", "demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["commit"] == candidate
    assert (data / "task.txt").read_text() == "preserved"
    snapshots = list((home / "demo" / "snapshots").iterdir())
    assert len(snapshots) == 1
    assert (snapshots[0] / "data" / "task.txt").read_text() == "preserved"
    assert main(["runtime", "update", "--distro", "demo", "--json"]) == 0
    capsys.readouterr()
    assert len(list((home / "demo" / "snapshots").iterdir())) == 1
    assert len(fake_build.calls["build"]) == 2
    assert main(["distro", "default", "demo"]) == 0
    capsys.readouterr()
    assert main(["config", "set", "runtime.subject_port", "4321"]) == 0
    capsys.readouterr()
    assert main(["distro", "config", "get", "demo", "runtime.subject_port", "--json"]) == 0
    setting = json.loads(capsys.readouterr().out)["data"]["runtime.subject_port"]
    assert setting["value"] == 4321 and setting["effective"] == "next-start"
    assert main(["distro", "status", "demo", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)["data"]
    assert status["installation"]["commit"] == candidate
    assert status["state"] == "stopped" and status["health"] == "not-probed"


@pytest.mark.parametrize("invocation", ["install", "create"])
def test_failed_first_install_leaves_no_declaration(home, repo, fake_build, invocation, capsys):
    assert main([invocation, "bad", "--repo", str(repo), "--ref", "commit:missing", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["data"] is None
    assert not (home / "bad" / "distro.toml").exists()
    assert not (home / "bad" / "current").exists()
    assert not (home / "boot.toml").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--gate", "full"],
        ["--subject-port", "0"],
        ["--version", "../escape"],
        ["--subject-port", "5173"],
    ],
)
def test_invalid_install_never_writes_config(home, repo, fake_build, args):
    assert main(["install", "demo", "--repo", str(repo), *args]) == 1
    assert not (home / "demo" / "distro.toml").exists()
    assert not fake_build.calls["build"]


def test_config_preserves_toml11_comments_unknown_keys_and_backup(home, capsys):
    root = home / "demo"
    root.mkdir(parents=True)
    path = root / "distro.toml"
    original = """# 用户维护的说明
distro = {
 scenario = "\\x41\\e",
}
secret = "do-not-display"
[runtime]
subject_port = 4112 # 本机端口
[custom]
untouched = [1, 2]
"""
    path.write_text(original)
    assert main(["config", "set", "demo", "runtime.subject_port", "4321"]) == 0
    capsys.readouterr()
    rewritten = path.read_text()
    assert "# 用户维护的说明" in rewritten and "# 本机端口" in rewritten
    parsed = tomllib.loads(rewritten)
    assert parsed["custom"] == {"untouched": [1, 2]}
    assert parsed["distro"]["scenario"] == "A\x1b"
    assert path.with_suffix(".toml.bak").read_text() == original
    assert main(["config", "show", "demo", "--json"]) == 0
    assert "do-not-display" not in capsys.readouterr().out
    before = path.read_bytes()
    for key, value in [
        ("runtime.subject_port", "true"),
        ("data.root", "relative"),
        ("distro.ref.gate", "full"),
        ("secret", "overwrite"),
    ]:
        assert main(["config", "set", "demo", key, value]) == 1
        assert path.read_bytes() == before


def test_config_does_not_move_existing_data(home, tmp_path):
    root = home / "demo"
    (root / "data").mkdir(parents=True)
    (root / "distro.toml").write_text("")
    (root / "data" / "subject.db").write_bytes(b"business-data")
    assert main(["config", "set", "demo", "data.root", str(tmp_path / "other")]) == 1
    assert (root / "distro.toml").read_text() == ""
    assert (root / "data" / "subject.db").read_bytes() == b"business-data"


def test_starting_supervisor_blocks_config_and_update(home, repo, fake_build, capsys):
    assert main(["install", "demo", "--repo", str(repo)]) == 0
    capsys.readouterr()
    root = home / "demo"
    record = {
        "schema": 1,
        "token": "a" * 32,
        "pid": os.getpid(),
        "created": psutil.Process().create_time(),
        "state": "starting",
        "generation": "2026.09",
    }
    (root / ".session.json").write_text(json.dumps(record))
    before = (root / "distro.toml").read_bytes()
    assert main(["config", "set", "demo", "runtime.subject_port", "4321"]) == 1
    assert main(["update", "demo", "--json"]) == 1
    assert "监督进程仍在运行" in capsys.readouterr().out
    assert (root / "distro.toml").read_bytes() == before
    assert len(fake_build.calls["build"]) == 1


def test_self_config_is_separate_and_preferences_are_consumed(home, capsys):
    assert main(["self", "config", "set", "self.output", "json"]) == 0
    capsys.readouterr()
    assert main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == []
    assert main(["self", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "toolchain"
    assert main(["self", "config", "set", "runtime.subject_port", "4321", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["scope"] == "toolchain"
    assert not (home / "boot.toml").exists()
    assert not list(home.glob("*/distro.toml"))


def test_boot_menu_preserves_manual_content(home):
    home.mkdir(parents=True)
    path = home / "boot.toml"
    path.write_text('# 保留\ndefault="demo/v1" # 选择\ncustom="manual"\n')
    boot = BootToml.load()
    boot.default = "demo/v2"
    boot.save()
    assert "# 保留" in path.read_text() and "# 选择" in path.read_text()
    assert tomllib.loads(path.read_text())["custom"] == "manual"


def test_failed_fetch_does_not_use_stale_local_candidate(home, repo, monkeypatch):
    import subprocess

    manager = RuntimeRefManager(root=home, repo=repo)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="offline"),
    )
    from ghrah_toolchain.runtime_ref import RefSource

    with pytest.raises(RuntimeRefError, match="fetch 失败"):
        manager._resolve_candidate(RefSource.parse("latest"), fetch=True, repo=repo)


def test_active_long_build_lock_is_not_reclaimed(home):
    home.mkdir(parents=True)
    path = home / ".bootloader.lock"
    path.write_text(str(os.getpid()))
    old = time.time() - 7200
    os.utime(path, (old, old))
    assert _lock_is_stale(path, stale_after=600) is False
    # O_EXCL 创建和 PID 写入之间的窗口也不能回收。
    path.write_text("")
    assert _lock_is_stale(path, stale_after=600) is False


def test_concurrent_manual_edit_is_not_overwritten(home, monkeypatch):
    import tomlkit

    from ghrah_toolchain.errors import LayoutError
    from ghrah_toolchain.toml_io import edit_toml

    home.mkdir(parents=True)
    path = home / "config.toml"
    path.write_text('[self]\noutput = "text"\n')
    real_dumps = tomlkit.dumps

    def change_during_render(document):
        path.write_text('# external editor\n[self]\noutput = "json"\n')
        return real_dumps(document)

    monkeypatch.setattr(tomlkit, "dumps", change_during_render)
    with pytest.raises(LayoutError, match="其他程序"):
        edit_toml(path, {"self.log_tail": 50})
    assert "# external editor" in path.read_text()


def test_replace_failure_preserves_original_and_reports_json(home, monkeypatch, capsys):
    root = home / "demo"
    root.mkdir(parents=True)
    path = root / "distro.toml"
    path.write_text("# manual\n")
    real_replace = os.replace

    def fail_target(source, destination):
        if Path(destination) == path:
            raise OSError("simulated disk failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_target)
    assert main(["config", "set", "demo", "runtime.subject_port", "4321", "--json"]) == 1
    assert "simulated disk failure" in json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert path.read_text() == "# manual\n"
    assert not list(root.glob(".distro.toml.*"))


def test_failed_rollback_keeps_recovery_artifacts(home, repo, fake_build, monkeypatch):
    assert main(["install", "demo", "--repo", str(repo)]) == 0
    candidate = install_support._commit(repo, "b", "2026-10-01T10:00:00 +0000")

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(RuntimeRefManager, "_write_boot_toml", fail)
    monkeypatch.setattr(RuntimeRefManager, "_rollback", fail)
    with pytest.raises(RuntimeRefError, match="回退未完成"):
        RuntimeRefManager().ensure(distro="demo", update=True)
    tree = (home / "demo" / "current").resolve()
    assert tree.is_dir()
    from ghrah_toolchain.materialize import read_generation

    assert read_generation(tree).base_commit == candidate


@pytest.mark.parametrize(
    "canonical, alias",
    [
        (["distro", "install", "demo"], ["create", "demo"]),
        (["distro", "start", "demo"], ["boot", "demo"]),
        (["distro", "update", "demo"], ["runtime", "update", "--distro", "demo"]),
        (["distro", "list"], ["ps"]),
    ],
)
def test_aliases_use_same_handler(canonical, alias):
    parser = _build_parser()
    assert parser.parse_args(canonical).func is parser.parse_args(alias).func
