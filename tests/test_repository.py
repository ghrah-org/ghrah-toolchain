# SPDX-License-Identifier: Apache-2.0
"""远程来源使用真实 file Git 传输，不依赖公网；构建边界另有真实集成验证。"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
import test_install_packages as support

from ghrah_toolchain.cli import main
from ghrah_toolchain.errors import RuntimeRefError
from ghrah_toolchain.layout import DistroToml
from ghrah_toolchain.repository import RemoteRepository, RepositorySource
from ghrah_toolchain.runtime_ref import RefSource, RuntimeRefManager

home = support.home
repo = support.repo
fake_build = support.fake_build


@pytest.fixture
def remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    support._git(repo, "branch", "-M", "main")
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)], check=True, capture_output=True
    )
    return bare


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/ghrah-org/ghrah-monorepo.git",
        "ssh://git@example.org:2222/org/runtime.git",
        "git@example.org:org/runtime.git",
        "file:///tmp/remote.git",
    ],
)
def test_remote_source_keeps_url(value):
    source = RepositorySource.parse(value)
    assert source.remote and source.location == value


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@example.org/repo.git",
        "https://token@example.org/repo.git",
        "https://example.org/repo.git?token=secret",
        "ext://command",
        "https://example.org",
        "ssh://user:secret@example.org/repo.git",
        "file://example.org/repo",
        "-option",
    ],
)
def test_invalid_remote_source_does_not_echo_credentials(value):
    with pytest.raises(RuntimeRefError) as exc:
        RepositorySource.parse(value)
    assert "secret" not in str(exc.value) and "token@" not in str(exc.value)


def test_remote_install_fetch_check_and_update(home, repo, remote, fake_build, capsys):
    url = remote.as_uri()
    assert main(["distro", "install", "demo", "--from", url, "--ref", "branch:main", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)["data"]
    config = DistroToml.load(home / "demo")
    assert config.repo_url == url and config.repo_path == ""
    assert config.ref_fetch_on_update is True
    assert main(["config", "get", "demo", "distro.repo.url", "--json"]) == 0
    item = json.loads(capsys.readouterr().out)["data"]["distro.repo.url"]
    assert item["value"] == url and item["read_only"] is True
    assert not (home / "demo" / "current" / ".git").exists()
    mirror = RemoteRepository(home, RepositorySource.parse(url))
    assert mirror.cached().is_dir()
    head_before = support._git(mirror.path, "rev-parse", "refs/heads/main")
    candidate = support._commit(repo, "b", "2026-10-01T10:00:00 +0000")
    support._git(repo, "push", str(remote), "main")
    assert main(["update", "demo", "--check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["update_available"] is False
    assert support._git(mirror.path, "rev-parse", "refs/heads/main") == head_before
    assert main(["update", "demo", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)["data"]
    assert second["commit"] == candidate and second["commit"] != first["commit"]
    # 已有实例保持 URL；缓存路径不是第二份配置来源。
    assert DistroToml.load(home / "demo").repo_url == url
    assert len(fake_build.calls["build"]) == 2
    # 删除远端模拟断网：--no-fetch 仍能复用缓存，默认联网更新必须报错且保留 current。
    shutil.rmtree(remote)
    assert main(["update", "demo", "--no-fetch", "--json"]) == 0
    capsys.readouterr()
    before = (home / "demo" / "current").readlink()
    assert main(["update", "demo", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["data"] is None
    assert (home / "demo" / "current").readlink() == before


def test_remote_latest_uses_default_branch_and_tags_are_unambiguous(home, repo, remote):
    first = support._git(repo, "rev-parse", "HEAD")
    support._git(repo, "checkout", "-b", "experiment")
    newest = support._commit(repo, "future", "2026-10-20T10:00:00 +0000")
    support._git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "main", newest, "-m", "release")
    support._git(repo, "push", str(remote), "experiment", "refs/tags/main")
    mirror = RemoteRepository(home, RepositorySource.parse(remote.as_uri()))
    manager = RuntimeRefManager(root=home)
    with mirror.prepare(refresh=False, allow_clone=True) as path:
        assert (
            manager._resolve_candidate(RefSource.parse("latest"), fetch=False, repo=path) == first
        )
        assert (
            manager._resolve_candidate(RefSource.parse("branch:main"), fetch=False, repo=path)
            == first
        )
        assert (
            manager._resolve_candidate(RefSource.parse("tag:main"), fetch=False, repo=path)
            == newest
        )
    # 远端默认分支改变后，refresh 同时更新 HEAD，不保留旧默认分支。
    support._git(remote, "symbolic-ref", "HEAD", "refs/heads/experiment")
    with mirror.prepare(refresh=True, allow_clone=False) as path:
        assert (
            manager._resolve_candidate(RefSource.parse("latest"), fetch=False, repo=path) == newest
        )


def test_failed_clone_leaves_no_cache_or_install_config(home, tmp_path, fake_build, capsys):
    url = (tmp_path / "missing.git").as_uri()
    assert main(["install", "demo", "--repo", url, "--json"]) == 1
    capsys.readouterr()
    mirror = RemoteRepository(home, RepositorySource.parse(url))
    assert not mirror.path.exists() and not list(mirror.root.glob(".clone-*"))
    assert not (home / "demo" / "distro.toml").exists()
    assert not (home / "boot.toml").exists()


def test_check_missing_cache_is_readonly(home, tmp_path, fake_build, capsys):
    root = home / "demo"
    root.mkdir(parents=True)
    # 无 current 时 --check 先报告未安装，不会为 URL 自动 clone。
    (root / "distro.toml").write_text('[distro.repo]\nurl="https://example.org/repo.git"\n')
    assert main(["update", "demo", "--check", "--json"]) == 1
    capsys.readouterr()
    assert not (home / ".cache").exists()


def test_git_missing_fails_before_install_writes(home, repo, monkeypatch, capsys):
    monkeypatch.setattr("ghrah_toolchain.repository.shutil.which", lambda _: None)
    assert main(["install", "demo", "--repo", str(repo), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert "需要 Git" in data["diagnostics"][0]
    assert not home.exists()


def test_cache_origin_mismatch_is_not_reused(home, remote):
    mirror = RemoteRepository(home, RepositorySource.parse(remote.as_uri()))
    with mirror.prepare(refresh=False, allow_clone=True):
        pass
    support._git(mirror.path, "remote", "set-url", "origin", "https://example.org/other.git")
    with pytest.raises(RuntimeRefError, match="来源不匹配"):
        mirror.cached()


def test_installed_status_does_not_need_git(home, repo, fake_build, monkeypatch, capsys):
    assert main(["install", "demo", "--repo", str(repo)]) == 0
    capsys.readouterr()
    monkeypatch.setattr("ghrah_toolchain.repository.shutil.which", lambda _: None)
    assert main(["status", "demo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["installation"]["state"] == "ready"


def test_remote_check_after_cache_removal_never_reclones(home, remote, fake_build, capsys):
    url = remote.as_uri()
    assert main(["install", "demo", "--repo", url]) == 0
    capsys.readouterr()
    cache = RemoteRepository(home, RepositorySource.parse(url))
    shutil.rmtree(cache.root)
    assert main(["update", "demo", "--check", "--json"]) == 1
    assert "缓存" in json.loads(capsys.readouterr().out)["diagnostics"][0]
    assert not cache.root.exists()
    assert main(["update", "demo", "--no-fetch", "--json"]) == 1
    capsys.readouterr()
    assert not cache.path.exists()
    assert main(["update", "demo", "--fetch", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "installed"
