# SPDX-License-Identifier: Apache-2.0
"""离线清单校验不执行组件代码，覆盖真实接线、路径与诊断。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghrah_toolchain.cli import main
from ghrah_toolchain.manifest import ManifestError, discover_manifest, load_manifest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "contract.json").write_text('{"id":"acme.service.v1"}')
    (provider / "ability.json").write_text('{"operations": {}}')
    (provider / "component.toml").write_text("""schema = 1
id = "provider"
version = "1.0.0"
[entries.run]
kind = "process"
argv = ["program-that-must-never-run"]
[exports.service]
scheme = "ability"
contract = "acme.service.v1"
descriptor = "contract.json"
definition = "ability.json"
entry = "run"
""")
    manifest = tmp_path / "ghrah.toml"
    manifest.write_text("""schema = 1
[workspace]
name = "example"
version = "0.1.0"
default_distro = "demo"
[components.provider]
path = "provider"
[distro.demo]
components = ["provider"]
roots = ["a/run"]
[distro.demo.instances.a]
component = "provider"
host = "process"
[distro.demo.instances.b]
component = "provider"
host = "process"
[distro.demo.services.selected]
scheme = "ability"
contract = "acme.service.v1"
instance = "b"
export = "service"
[distro.demo.namespaces.agent.bindings]
local = "selected"
""")
    return manifest


def test_discovery_and_static_metadata(workspace: Path, monkeypatch, capsys) -> None:
    nested = workspace.parent / "sub" / "directory"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert discover_manifest() == workspace
    assert main(["metadata", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "workspace"
    assert result["data"]["runtime_binding_applied"] is False
    assert result["data"]["distro"]["demo"]["services"]["selected"]["instance"] == "b"
    assert not (workspace.parent / ".ghrah").exists()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("schema = 1", "schema = 9"),
        ('default_distro = "demo"', 'default_distro = "missing"'),
        ('instance = "b"', 'instance = "missing"'),
        ('local = "selected"', 'local = "missing"'),
        ('roots = ["a/run"]', 'roots = ["a/missing"]'),
        ('host = "process"', 'host = "in-process"'),
        ('path = "provider"', 'path = "../provider"'),
        ('export = "service"', 'export = "missing"'),
    ],
)
def test_invalid_composition(workspace: Path, old: str, new: str) -> None:
    workspace.write_text(workspace.read_text().replace(old, new))
    with pytest.raises(ManifestError):
        load_manifest(workspace)


def test_unknown_field_json_diagnostic(workspace: Path, capsys) -> None:
    workspace.write_text("grant = true\n" + workspace.read_text())
    assert main(["check", "--manifest-path", str(workspace), "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["data"] is None
    assert "Additional properties" in result["diagnostics"][0]


def test_contract_drift_and_file_drift(workspace: Path) -> None:
    first = load_manifest(workspace)
    asset = workspace.parent / "provider" / "ability.json"
    asset.write_text('{"operations": {"read": {}}}')
    assert load_manifest(workspace).digest != first.digest
    (asset.parent / "contract.json").write_text('{"id":"other"}')
    with pytest.raises(ManifestError, match="接口 ID"):
        load_manifest(workspace)


def test_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    descriptor = workspace.parent / "provider" / "contract.json"
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text('{"id":"acme.service.v1"}')
    descriptor.unlink()
    descriptor.symlink_to(outside)
    with pytest.raises(ManifestError, match="越界"):
        load_manifest(workspace)


def test_service_cycle_is_not_ready_cycle(workspace: Path) -> None:
    component = workspace.parent / "provider" / "component.toml"
    component.write_text(
        component.read_text()
        + """
[imports.peer]
contract = "acme.service.v1"
"""
    )
    workspace.write_text(
        workspace.read_text().replace(
            'host = "process"', 'host = "process"\nimports = { peer = "selected" }'
        )
    )
    load_manifest(workspace)  # 服务可以互相调用，没有强制就绪依赖。
    component.write_text(
        component.read_text().replace(
            'argv = ["program-that-must-never-run"]',
            'argv = ["program-that-must-never-run"]\nrequires_ready = ["peer"]',
        )
    )
    with pytest.raises(ManifestError, match="成环"):
        load_manifest(workspace)


def test_explicit_manifest_never_falls_back(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="清单不存在"):
        load_manifest(tmp_path / "missing.toml")
