# SPDX-License-Identifier: Apache-2.0
"""契约 v0：组件 Provider 声明与发行版系统/身份声明的静态校验。

这些用例覆盖 P0 冻结的形状，而不是服务是否真的能启动：清单通过校验只说明
声明符合契约，不代表代内目录生产链或宿主已经落地。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghrah_toolchain.manifest import ManifestError, load_manifest

_COMPONENT = """schema = 1
id = "kernel"
version = "0.1.0a0"

[entries.host]
kind = "process"
argv = ["ghrah-subject-host"]

[providers.namespace]
module = "ghrah_kernel.integration.entry"
provider = "namespace"
kind = "namespace"
order = 10
restart = "never"
ready = { type = "handshake", contract = "ghrah.namespace/v0" }
health = { type = "protocol", contract = "ghrah.namespace/v0" }

[providers.namespace.schemes.ns]
contract = "ghrah.namespace/v0"
actions = ["bind", "unbind", "inspect"]

[providers.namespace.bindings.main]
path = "/ns/main"
scheme = "ns"
scope = "main"
namespace = "main"

[providers.agent]
module = "ghrah_kernel.integration.entry"
provider = "agent"
kind = "agent"
order = 20
after = ["kernel:namespace"]
requires_ready = ["kernel:namespace"]
restart = "on_failure"
max_restarts = 3
window_seconds = 5
drain_ms = 50
close_timeout_ms = 1500
close_action = ["agent", "close_resources"]
configuration = "providers/agent.config.json"

[providers.agent.schemes.agent]
contract = "ghrah.agent-chat/v0"
actions = ["receive", "status", "close_resources"]
"""

_MANIFEST = """schema = 1

[workspace]
name = "example"
version = "0.2.0"
default_distro = "demo"

[components.kernel]
path = "kernel"

[distro.demo]
components = ["kernel"]
roots = ["subject/host"]

[distro.demo.system]
kernel = "kernel"

[[distro.demo.system.providers]]
id = "kernel:namespace"
principal = '["local","ns"]'

[[distro.demo.system.providers]]
id = "kernel:agent"
principal = '["local","agent"]'
resolution_scope = "main"

[distro.demo.instances.subject]
component = "kernel"
host = "process"
host_entry = "subject/host"

[distro.demo.identity]
grant_lifetime = { mode = "instance" }
subjects = [
    { issuer = "local", subject_id = "admin", kind = "user" },
    { issuer = "local", subject_id = "ns", kind = "service" },
    { issuer = "local", subject_id = "agent", kind = "agent" },
]
groups = []
namespaces = [{ namespace_id = "main", owner = '["local","admin"]' }]
scopes = [{
    scope_id = "main",
    namespaces = ["main"],
    subjects = ['["local","admin"]', '["local","ns"]', '["local","agent"]'],
}]
[[distro.demo.identity.access]]
rule_id = "entry"
namespace_id = "main"
resource = "agent-entry"
actions = ["receive"]
subject = '["local","admin"]'
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "component.toml").write_text(_COMPONENT, encoding="utf-8")
    manifest = tmp_path / "ghrah.toml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    return manifest


def test_provider_and_system_declarations_load(workspace: Path) -> None:
    """契约示例必须能通过静态校验；未知字段的拒绝由 schema 的 additionalProperties 保证。"""
    loaded = load_manifest(workspace)
    assert loaded.digest


def test_host_entry_is_a_real_startup_node(workspace: Path) -> None:
    workspace.write_text(
        workspace.read_text(encoding="utf-8").replace(
            'host_entry = "subject/host"', 'host_entry = "subject/missing"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(workspace)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # 形状：必填字段
        ('module = "ghrah_kernel.integration.entry"', 'module = ""'),
        ('kind = "namespace"', 'kind = "Namespace"'),
        ('contract = "ghrah.namespace/v0"\nactions', "actions"),
        # 取值域
        ('restart = "on_failure"', 'restart = "sometimes"'),
        ("max_restarts = 3", "max_restarts = 9"),
        ("drain_ms = 50", "drain_ms = 900"),
        ('close_action = ["agent", "close_resources"]', 'close_action = ["agent"]'),
        # 命名接线
        ('path = "/ns/main"', 'path = "ns/main"'),
        # 就绪探针只支持 handshake / protocol
        (
            'ready = { type = "handshake", contract = "ghrah.namespace/v0" }',
            'ready = { type = "http" }',
        ),
    ],
)
def test_invalid_provider_declaration(workspace: Path, old: str, new: str) -> None:
    component = workspace.parent / "kernel" / "component.toml"
    body = component.read_text(encoding="utf-8")
    assert old in body, old
    component.write_text(body.replace(old, new), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(workspace)


def test_unknown_provider_field_is_rejected(workspace: Path) -> None:
    component = workspace.parent / "kernel" / "component.toml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            'provider = "namespace"', 'provider = "namespace"\nunknown = true'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(workspace)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # 系统声明形状
        ('kernel = "kernel"', 'kernel = ""'),
        ("principal = '[\"local\",\"ns\"]'", 'principal = "local|ns"'),
        ("principal = '[\"local\",\"ns\"]'", 'principal = ""'),
        # 身份声明形状：字段名必须与 StaticIdentityConfig 的输入一致
        ('subject_id = "admin"', 'id = "admin"'),
        ('namespace_id = "main"', 'namespace = "main"'),
        ("[[distro.demo.identity.access]]", "[[distro.demo.identity.rules]]"),
        ("grant_lifetime = { mode = \"instance\" }", "grant_lifetime = { ttl_seconds = 5 }"),
    ],
)
def test_invalid_system_or_identity_declaration(workspace: Path, old: str, new: str) -> None:
    body = workspace.read_text(encoding="utf-8")
    assert old in body, old
    workspace.write_text(body.replace(old, new), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(workspace)


_SYSTEM_BLOCK = '[distro.demo.system]\nkernel = "kernel"\n'


def _replace_system(workspace: Path, block: str) -> None:
    body = workspace.read_text(encoding="utf-8")
    start = body.index("[distro.demo.system]")
    end = body.index("[distro.demo.identity]")
    workspace.write_text(body[:start] + block + "\n" + body[end:], encoding="utf-8")


@pytest.mark.parametrize(
    "block",
    [
        # 启用清单必须是表数组：字符串数组是已撤回的旧形状
        _SYSTEM_BLOCK + 'providers = ["kernel:namespace"]\n',
        # 缺 principal
        _SYSTEM_BLOCK + '\n[[distro.demo.system.providers]]\nid = "kernel:namespace"\n',
        # 未知字段
        _SYSTEM_BLOCK
        + '\n[[distro.demo.system.providers]]\nid = "kernel:namespace"\n'
        + "principal = '[\"local\",\"ns\"]'\ndiagnostics = false\n",
        # principal 必须是 Subject.ref 的紧凑 JSON 形状
        _SYSTEM_BLOCK
        + '\n[[distro.demo.system.providers]]\nid = "kernel:namespace"\n'
        + 'principal = "local|ns"\n',
        # id 必须是 <component>:<provider>
        _SYSTEM_BLOCK
        + '\n[[distro.demo.system.providers]]\nid = "kernel"\n'
        + "principal = '[\"local\",\"ns\"]'\n",
        # resolution_scope 不能是空串
        _SYSTEM_BLOCK
        + '\n[[distro.demo.system.providers]]\nid = "kernel:namespace"\n'
        + "principal = '[\"local\",\"ns\"]'\nresolution_scope = \"\"\n",
    ],
)
def test_invalid_service_binding_declaration(workspace: Path, block: str) -> None:
    _replace_system(workspace, block)
    with pytest.raises(ManifestError):
        load_manifest(workspace)


def test_access_rule_needs_exactly_one_principal(workspace: Path) -> None:
    body = workspace.read_text(encoding="utf-8")
    target = "subject = '[\"local\",\"admin\"]'\n"
    assert target in body
    workspace.write_text(
        body.replace(target, target + 'group = "admins"\n'),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(workspace)


def test_legacy_workspace_without_system_section_still_checks(tmp_path: Path) -> None:
    """旧清单没有 system/identity 时仍可静态校验；产出代内目录的要求属构建期，不在本用例。"""
    component = tmp_path / "component"
    component.mkdir()
    (component / "component.toml").write_text(
        'schema = 1\nid = "component"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    manifest = tmp_path / "ghrah.toml"
    manifest.write_text(
        """schema = 1
[workspace]
name = "legacy"
version = "0.1.0"
[components.component]
path = "component"
[distro.demo]
components = ["component"]
roots = []
[distro.demo.instances.only]
component = "component"
host = "process"
""",
        encoding="utf-8",
    )
    assert load_manifest(manifest).digest
