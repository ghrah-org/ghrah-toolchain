# SPDX-License-Identifier: Apache-2.0
"""开发代 catalog 的全量生成、增量刷新与旧源码兼容。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from ghrah_toolchain import dev
from ghrah_toolchain.boot import BootTarget
from ghrah_toolchain.layout import DistroLayout, DistroToml
from ghrah_toolchain.materialize import CATALOG_FILE, GENERATION_TOML


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _target(tmp_path: Path, repo: Path) -> BootTarget:
    layout = DistroLayout(tmp_path / "local-instance")
    return BootTarget(
        "local-instance", "legacy", layout.current, layout, DistroToml(repo_path=str(repo))
    )


def test_dev_accepts_linked_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(
        repo,
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=test",
        "-c",
        "user.email=t@t",
        "commit",
        "--allow-empty",
        "-qm",
        "init",
    )
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    monkeypatch.setattr(dev, "resolve_boot_target", lambda *_: _target(tmp_path, linked))
    assert dev._resolve_context(argparse.Namespace(distro="local-instance")).repo == linked


def test_dev_refreshes_catalog_for_full_incremental_and_declaration_only_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    workspace = repo / "ghrah.toml"
    (repo / ".python-version").write_text("3.15.1\n", encoding="utf-8")
    legacy = """schema = 1
[workspace]
name = "example"
version = "1"
default_distro = "generic"
[components.kernel]
path = "kernel"
[distro.generic]
components = ["kernel"]
roots = ["subject/server"]
[distro.generic.instances.subject]
component = "kernel"
host = "process"
host_entry = "subject/server"
"""
    workspace.write_text(
        legacy
        + """
[distro.generic.system]
host_instance = "subject"
kernel = "kernel"
[[distro.generic.system.providers]]
id = "kernel:namespace"
principal = '["local","ns"]'
[distro.generic.identity]
grant_lifetime = {mode = "instance"}
subjects = [
  {issuer = "local", subject_id = "admin", kind = "user"},
  {issuer = "local", subject_id = "ns", kind = "service"},
]
groups = []
namespaces = []
scopes = []
access = []
""",
        encoding="utf-8",
    )
    component = repo / "kernel" / "component.toml"
    component.parent.mkdir()
    component.write_text(
        """schema = 1
id = "kernel"
version = "1"
[entries.server]
kind = "process"
argv = ["ghrah-subject-server"]
[kernel]
module = "ghrah_kernel"
entry = "_kernel"
[providers.namespace]
module = "ghrah_kernel.integration.entry"
provider = "namespace"
kind = "namespace"
configuration = "ns.json"
[providers.namespace.schemes.ns]
contract = "ghrah.namespace/v0"
actions = ["inspect"]
""",
        encoding="utf-8",
    )
    configuration = component.with_name("ns.json")
    configuration.write_text('{"option": 1}', encoding="utf-8")

    ctx = dev.DevContext(target=_target(tmp_path, repo), repo=repo)
    calls: list[str] = []

    def build_wheels(src: Path, wheels: Path) -> None:
        calls.append("full")
        wheels.mkdir(parents=True)
        (wheels / "kernel-1-py3-none-any.whl").touch()

    def install_python(distro: Path, artifacts: Path, venv: Path, *, python: str) -> None:
        assert python == "3.15.1"
        calls.append("install")
        py = dev._venv_python(venv)
        py.parent.mkdir(parents=True)
        py.touch()

    run = dev._run

    def run_with_fake_pip(command: list[str], **kwargs):
        if command[:3] == ["uv", "pip", "install"]:
            calls.append("upgrade")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return run(command, **kwargs)

    monkeypatch.setattr(dev, "PYTHON_PACKAGES", ("kernel",))
    monkeypatch.setattr(dev, "_build_wheels", build_wheels)
    monkeypatch.setattr(dev, "_export_pins", lambda _src, wheels: (wheels / "pins.txt").touch())
    monkeypatch.setattr(dev, "_build_webui", lambda *_: None)
    monkeypatch.setattr(dev, "_build_incremental", lambda *_: calls.append("incremental"))
    monkeypatch.setattr(dev, "_install_python_env", install_python)
    monkeypatch.setattr(dev, "_run", run_with_fake_pip)
    monkeypatch.setattr(dev, "_smoke", lambda _: None)

    def run_dev() -> None:
        assert dev.dev_run(ctx, no_restart=True, force_full=False) == 0

    def catalog() -> dict:
        data = (ctx.gen / CATALOG_FILE).read_bytes()
        assert data == (ctx.artifacts / CATALOG_FILE).read_bytes()
        metadata = tomllib.loads((ctx.gen / GENERATION_TOML).read_text(encoding="utf-8"))
        assert metadata["generation"]["mode"] == "dev"
        assert metadata["generation"]["catalog_sha256"] == hashlib.sha256(data).hexdigest()
        return json.loads(data)

    run_dev()
    first = catalog()
    assert first["distro"] == "generic"
    assert calls == ["full", "install"]
    assert (ctx.gen / GENERATION_TOML).exists()

    run_dev()
    assert calls == ["full", "install"]
    assert catalog() == first

    # 已有开发代缺元数据时，只补代元数据，不重编任何 wheel。
    (ctx.gen / GENERATION_TOML).unlink()
    run_dev()
    assert catalog() == first
    assert calls == ["full", "install"]

    workspace.write_text(
        workspace.read_text(encoding="utf-8").replace('version = "1"', 'version = "2"'),
        encoding="utf-8",
    )
    run_dev()
    assert calls == ["full", "install"]
    assert catalog()["workspace"]["version"] == "2"
    assert catalog()["digest"] != first["digest"]

    configuration.write_text('{"option": 2}', encoding="utf-8")
    run_dev()
    assert calls == ["full", "install", "incremental", "upgrade"]
    assert catalog()["services"][0]["configuration"] == {"option": 2}

    workspace.write_text(legacy, encoding="utf-8")
    run_dev()
    assert not (ctx.gen / CATALOG_FILE).exists()
    assert not (ctx.artifacts / CATALOG_FILE).exists()
    assert not (ctx.gen / GENERATION_TOML).exists()
    assert calls == ["full", "install", "incremental", "upgrade"]


def test_smoke_resolves_entry_from_generation_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """冒烟把代根（不是 venv）交给入口解析：入口名要读代内 catalog。"""
    captured: dict[str, object] = {}

    def fake_run(command: list[str]) -> None:
        captured["cmd"] = command

    def fake_server_command(gen: Path) -> list[str]:
        captured["gen"] = gen
        return []

    monkeypatch.setattr(dev, "_run", fake_run)
    monkeypatch.setattr(dev, "os_server_command", fake_server_command)
    dev._smoke(tmp_path)
    assert captured["gen"] == tmp_path
