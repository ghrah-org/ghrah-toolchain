# SPDX-License-Identifier: Apache-2.0
"""代内 catalog 的构建期汇总与校验。

这些用例覆盖 catalog.py 的纯计算面：诊断格式、一次报全的收集顺序、摘要
规范化。安装域与代目录落位不在本模块范围内。``identity`` 的跨文件引用
另组用例，验证离线 registry 能解析 workspace 的 ``$anchor``。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from ghrah_toolchain.catalog import (
    CATALOG_SCHEMA,
    CatalogError,
    Diagnostic,
    build_catalog,
    canonical_json,
    catalog_digest,
    collect_diagnostics,
    collect_schema_diagnostics,
)
from ghrah_toolchain.manifest import ManifestError, _validate


def test_catalog_schema_constant() -> None:
    assert CATALOG_SCHEMA == 1


def test_diagnostic_render_matches_contract_format() -> None:
    """诊断渲染：``<文件>:[<字段路径>] <错误码>: <说明>``。"""
    diag = Diagnostic(
        file="ghrah-kernel/component.toml",
        path="providers.namespace.restart",
        code="E-PROV-POLICY",
        message='kind="namespace" 必须 restart="never"（当前 "on_failure"）',
    )
    assert diag.render() == (
        "ghrah-kernel/component.toml:[providers.namespace.restart] E-PROV-POLICY: "
        'kind="namespace" 必须 restart="never"（当前 "on_failure"）'
    )


def test_diagnostic_top_level_path_is_empty() -> None:
    diag = Diagnostic(file="ghrah.toml", path="", code="E-CAT-DISTRO", message="未选择发行版")
    assert diag.render() == "ghrah.toml:[] E-CAT-DISTRO: 未选择发行版"


def test_catalog_error_carries_all_diagnostics_line_by_line() -> None:
    """一次报全：不得折叠成一行，也不得只留首条。"""
    diagnostics = [
        Diagnostic(
            file="b/component.toml", path="providers.x", code="E-PROV-CONFIG", message="第二条"
        ),
        Diagnostic(
            file="a/component.toml", path="providers.y", code="E-PROV-POLICY", message="第一条"
        ),
    ]
    error = CatalogError(diagnostics)
    assert error.exit_code == 3
    assert len(error.diagnostics) == 2
    assert str(error).splitlines() == [d.render() for d in diagnostics]


def test_diagnostic_sort_key_orders_by_file_then_path() -> None:
    """收集顺序：类别序 → 文件路径 → 字段路径。"""
    diagnostics = [
        Diagnostic(file="b/component.toml", path="providers.a", code="E-PROV-CONFIG", message="m"),
        Diagnostic(file="a/component.toml", path="providers.z", code="E-PROV-CONFIG", message="m"),
        Diagnostic(file="a/component.toml", path="providers.a", code="E-PROV-CONFIG", message="m"),
    ]
    ordered = sorted(diagnostics, key=Diagnostic.sort_key)
    assert [(d.file, d.path) for d in ordered] == [
        ("a/component.toml", "providers.a"),
        ("a/component.toml", "providers.z"),
        ("b/component.toml", "providers.a"),
    ]


def test_canonical_json_sorts_keys_and_keeps_non_ascii() -> None:
    payload = {"b": 1, "a": {"y": 2, "x": "中文"}, "c": [3, 1, 2]}
    assert canonical_json(payload) == '{"a":{"x":"中文","y":2},"b":1,"c":[3,1,2]}'


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})


def test_catalog_digest_ignores_digest_field() -> None:
    base = {"schema": 1, "distro": "generic"}
    with_digest = dict(base, digest="sha256:" + "0" * 64)
    assert catalog_digest(base) == catalog_digest(with_digest)


def test_catalog_digest_is_sha256_of_canonical_body() -> None:
    """校验方必须重算，不得只信任文件里的 digest。"""
    body = {"schema": 1, "workspace": {"name": "ghrah", "version": "0.2.0"}}
    digest = catalog_digest(dict(body, digest="sha256:" + "0" * 64))
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    # 同内容不同键序必须得到同一摘要（canonical 的用途）
    reordered = {"workspace": {"version": "0.2.0", "name": "ghrah"}, "schema": 1}
    assert catalog_digest(reordered) == digest


def test_catalog_digest_changes_when_body_changes() -> None:
    a = {"schema": 1, "distro": "generic"}
    b = {"schema": 1, "distro": "other"}
    assert catalog_digest(a) != catalog_digest(b)


def test_diagnostics_are_json_serialisable_for_cli_json_mode() -> None:
    """--json 模式下诊断要能进 JSON，不被 render 的换行污染。"""
    diag = Diagnostic(file="ghrah.toml", path="", code="E-CAT-SYSTEM", message="缺 system")
    payload = {"diagnostics": [diag.render()]}
    assert json.loads(json.dumps(payload, ensure_ascii=False))["diagnostics"] == [
        "ghrah.toml:[] E-CAT-SYSTEM: 缺 system"
    ]


# ── schema 聚合 ────────────────────────────────────────────

_VALID_MANIFEST = """schema = 1

[workspace]
name = "example"
version = "0.2.0"
default_distro = "demo"

[components.kernel]
path = "kernel"

[distro.demo]
components = ["kernel"]
roots = ["subject/host"]

[distro.demo.instances.subject]
component = "kernel"
host = "process"
host_entry = "subject/host"
"""

_VALID_COMPONENT = """schema = 1
id = "kernel"
version = "0.1.0a0"

[entries.host]
kind = "process"
argv = ["ghrah-subject-host"]
"""


def _workspace(
    tmp_path: Path, manifest: str = _VALID_MANIFEST, component: str = _VALID_COMPONENT
) -> Path:
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "component.toml").write_text(component, encoding="utf-8")
    (tmp_path / "ghrah.toml").write_text(manifest, encoding="utf-8")
    return tmp_path


def test_valid_declarations_produce_no_schema_diagnostics(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    data, components, diagnostics = collect_schema_diagnostics(root)
    assert data is not None
    assert set(components) == {"kernel"}
    assert diagnostics == []


def test_manifest_schema_error_is_reported_with_file_path(tmp_path: Path) -> None:
    """诊断必须带「相对工作区根的文件 + 字段路径」，能直接跳转。"""
    root = _workspace(tmp_path, manifest=_VALID_MANIFEST.replace('version = "0.2.0"\n', ""))
    _, _, diagnostics = collect_schema_diagnostics(root)
    assert diagnostics, "缺 workspace.version 必须报错"
    assert all(d.file == "ghrah.toml" for d in diagnostics)


def test_component_schema_error_uses_component_relative_path(tmp_path: Path) -> None:
    broken = _VALID_COMPONENT.replace('id = "kernel"\n', "")
    root = _workspace(tmp_path, component=broken)
    _, _, diagnostics = collect_schema_diagnostics(root)
    assert diagnostics, "组件缺 id 必须报错"
    assert {d.file for d in diagnostics} == {"kernel/component.toml"}


def test_multiple_schema_errors_are_all_collected(tmp_path: Path) -> None:
    """一次报全：不得只报首错。"""
    broken = _VALID_COMPONENT.replace('id = "kernel"\n', "").replace('version = "0.1.0a0"\n', "")
    root = _workspace(tmp_path, component=broken)
    _, _, diagnostics = collect_schema_diagnostics(root)
    assert len(diagnostics) >= 2, f"至少应报出 id 与 version 两处，实际: {diagnostics}"


def test_unparsable_toml_becomes_diagnostic_not_exception(tmp_path: Path) -> None:
    """解析失败也要折算成诊断，否则「一次报全」在读取阶段就断了。"""
    root = _workspace(tmp_path, component="schema = 1\nid = \n")
    _, components, diagnostics = collect_schema_diagnostics(root)
    assert diagnostics
    assert components == {}
    assert any(d.code == "E-CAT-PARSE" for d in diagnostics)


def test_missing_component_toml_is_reported(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "kernel" / "component.toml").unlink()
    _, _, diagnostics = collect_schema_diagnostics(root)
    assert diagnostics
    assert {d.file for d in diagnostics} == {"kernel/component.toml"}


# ── 语义校验总入口 ────────────────────────────────────────

_FULL_COMPONENT = """schema = 1
id = "kernel"
version = "0.1.0a0"

[kernel]
module = "ghrah_kernel"
entry = "_kernel"

[entries.host]
kind = "process"
argv = ["ghrah-subject-host"]

[providers.namespace]
module = "ghrah_kernel.integration.entry"
provider = "namespace"
kind = "namespace"
order = 10
restart = "never"

[providers.namespace.schemes.ns]
contract = "ghrah.namespace/v0"
actions = ["bind", "unbind", "inspect"]
"""

_FULL_MANIFEST = """schema = 1

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
host_instance = "subject"
kernel = "kernel"

[[distro.demo.system.providers]]
id = "kernel:namespace"
principal = '["local","ns"]'

[distro.demo.instances.subject]
component = "kernel"
host = "process"
host_entry = "subject/host"

[distro.demo.identity]
grant_lifetime = { mode = "instance" }
subjects = [
    { issuer = "local", subject_id = "admin", kind = "user" },
    { issuer = "local", subject_id = "ns", kind = "service" },
]
groups = []
namespaces = [{ namespace_id = "main", owner = '["local","admin"]' }]
scopes = [{
    scope_id = "main",
    namespaces = ["main"],
    subjects = ['["local","admin"]', '["local","ns"]'],
}]
access = []
"""


def _full(tmp_path: Path, manifest: str = _FULL_MANIFEST, component: str = _FULL_COMPONENT) -> Path:
    (tmp_path / "kernel").mkdir()
    (tmp_path / "kernel" / "component.toml").write_text(component, encoding="utf-8")
    (tmp_path / "ghrah.toml").write_text(manifest, encoding="utf-8")
    return tmp_path


def _codes(root: Path) -> list[str]:
    _, _, diagnostics = collect_diagnostics(root)
    return [d.code for d in diagnostics]


def test_full_valid_declaration_has_no_diagnostics(tmp_path: Path) -> None:
    """正向锚点：全绿是后面每条负例可比对的前提。"""
    root = _full(tmp_path)
    data, distro, diagnostics = collect_diagnostics(root)
    assert data is not None
    assert distro == "demo"
    assert diagnostics == [], [d.render() for d in diagnostics]


def test_missing_distro_selection_is_rejected(tmp_path: Path) -> None:
    """不猜默认发行版。"""
    manifest = _FULL_MANIFEST.replace('default_distro = "demo"\n', "")
    root = _full(tmp_path, manifest=manifest)
    assert "E-CAT-DISTRO" in _codes(root)


def test_unknown_distro_is_rejected(tmp_path: Path) -> None:
    root = _full(tmp_path)
    _, distro, diagnostics = collect_diagnostics(root, distro="absent")
    assert distro is None
    assert any(d.code == "E-CAT-DISTRO" for d in diagnostics)


def test_missing_system_is_rejected(tmp_path: Path) -> None:
    """要产出 catalog 的构建必须显式声明 system。"""
    body = _FULL_MANIFEST
    start = body.index("[distro.demo.system]")
    end = body.index("[[distro.demo.system.providers]]")
    manifest = body[:start] + body[end:]
    manifest = manifest.replace("[[distro.demo.system.providers]]", "\n# moved\n", 1)
    root = _full(
        tmp_path,
        manifest=manifest.replace('id = "kernel:namespace"\nprincipal', "x = 1\nprincipal"),
    )
    assert "E-CAT-SYSTEM" in _codes(root)


def test_namespace_must_be_first(tmp_path: Path) -> None:
    """排序后首位须为 namespace。"""
    component = (
        _FULL_COMPONENT.replace("order = 10", "order = 20")
        + """
[providers.agent]
module = "ghrah_kernel.integration.entry"
provider = "agent"
kind = "agent"
order = 5
restart = "on_failure"

[providers.agent.schemes.agent]
contract = "ghrah.agent-chat/v0"
actions = ["receive"]
"""
    )
    manifest = (
        _FULL_MANIFEST
        + """
[[distro.demo.system.providers]]
id = "kernel:agent"
principal = '["local","ns"]'
"""
    )
    root = _full(tmp_path, manifest=manifest, component=component)
    assert "E-PROV-NAMESPACE" in _codes(root)


def test_duplicate_contract_action_is_rejected(tmp_path: Path) -> None:
    """(contract, action) 全表唯一。"""
    component = (
        _FULL_COMPONENT
        + """
[providers.dup]
module = "dummy"
provider = "dup"
kind = "service"

[providers.dup.schemes.ns]
contract = "ghrah.namespace/v0"
actions = ["bind"]
"""
    )
    manifest = (
        _FULL_MANIFEST
        + """
[[distro.demo.system.providers]]
id = "kernel:dup"
principal = '["local","ns"]'
"""
    )
    root = _full(tmp_path, manifest=manifest, component=component)
    assert "E-PROV-ACTION-DUP" in _codes(root)


def test_unknown_dependency_target_is_rejected(tmp_path: Path) -> None:
    """after 目标须为已启用 id。"""
    component = _FULL_COMPONENT.replace(
        'restart = "never"', 'restart = "never"\nafter = ["kernel:absent"]'
    )
    root = _full(tmp_path, component=component)
    assert "E-PROV-DEPENDENCY" in _codes(root)


def test_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    """依赖图无环。"""
    component = (
        _FULL_COMPONENT.replace(
            'restart = "never"', 'restart = "never"\nrequires_ready = ["kernel:loop"]'
        )
        + """
[providers.loop]
module = "dummy"
provider = "loop"
kind = "service"
requires_ready = ["kernel:namespace"]

[providers.loop.schemes.ns]
contract = "ghrah.other/v0"
actions = ["ping"]
"""
    )
    manifest = (
        _FULL_MANIFEST
        + """
[[distro.demo.system.providers]]
id = "kernel:loop"
principal = '["local","ns"]'
"""
    )
    root = _full(tmp_path, manifest=manifest, component=component)
    assert "E-PROV-DEPENDENCY" in _codes(root)


def test_namespace_restart_must_be_never(tmp_path: Path) -> None:
    """汇总层：namespace ⇒ restart="never"。"""
    component = _FULL_COMPONENT.replace('restart = "never"', 'restart = "on_failure"')
    root = _full(tmp_path, component=component)
    assert "E-PROV-POLICY" in _codes(root)


def test_close_action_must_belong_to_own_schemes(tmp_path: Path) -> None:
    """汇总层：close_action 的 (scheme, action) 属于本服务 schemes。"""
    component = _FULL_COMPONENT.replace(
        'restart = "never"', 'restart = "never"\nclose_action = ["ns", "absent"]'
    )
    root = _full(tmp_path, component=component)
    assert "E-PROV-POLICY" in _codes(root)


def test_binding_to_undeclared_scope_is_rejected(tmp_path: Path) -> None:
    """bindings.scope 须在 identity.scopes 中。"""
    component = (
        _FULL_COMPONENT
        + """
[providers.namespace.bindings.main]
path = "/ns/main"
scheme = "ns"
scope = "main2"
namespace = "main"
"""
    )
    root = _full(tmp_path, component=component)
    assert "E-PROV-BINDING" in _codes(root)


def _component_with_configuration() -> str:
    """把 configuration 放进 [providers.namespace] 段内（不能落在 schemes 表之后）。"""
    return _FULL_COMPONENT.replace(
        'restart = "never"', 'restart = "never"\nconfiguration = "ns.json"', 1
    )


def test_configuration_absolute_path_is_rejected(tmp_path: Path) -> None:
    """展开前的绝对路径一律拒绝。"""
    root = _full(tmp_path, component=_component_with_configuration())
    (root / "kernel" / "ns.json").write_text(
        json.dumps({"database": "/home/u/main.db"}), encoding="utf-8"
    )
    assert "E-PROV-CONFIG" in _codes(root)


def test_configuration_reserved_key_is_rejected(tmp_path: Path) -> None:
    """保留键不得出现在声明里。"""
    root = _full(tmp_path, component=_component_with_configuration())
    (root / "kernel" / "ns.json").write_text(
        json.dumps({"principal": '["local","ns"]'}), encoding="utf-8"
    )
    assert "E-PROV-CONFIG" in _codes(root)


def test_configuration_reserved_token_is_rejected(tmp_path: Path) -> None:
    """宿主保留参数不得作为独立 token 出现。"""
    root = _full(tmp_path, component=_component_with_configuration())
    (root / "kernel" / "ns.json").write_text(
        json.dumps({"args": ["--credential=x"]}), encoding="utf-8"
    )
    assert "E-PROV-CONFIG" in _codes(root)


def test_placeholder_is_not_expanded(tmp_path: Path) -> None:
    """构建期保留占位符原样，不展开。"""
    root = _full(tmp_path, component=_component_with_configuration())
    (root / "kernel" / "ns.json").write_text(
        json.dumps({"database": "${DATA_ROOT}/agent/main.db"}), encoding="utf-8"
    )
    assert "E-PROV-CONFIG" not in _codes(root)


def test_configuration_missing_file_is_reported(tmp_path: Path) -> None:
    """声明了 configuration 但文件不存在：报 E-PROV-CONFIG 而非崩溃。"""
    root = _full(tmp_path, component=_component_with_configuration())
    assert "E-PROV-CONFIG" in _codes(root)


def test_service_principal_must_not_be_user(tmp_path: Path) -> None:
    """服务运行主体不能是 user。"""
    manifest = _FULL_MANIFEST.replace(
        """[[distro.demo.system.providers]]
id = "kernel:namespace"
principal = '["local","ns"]'""",
        """[[distro.demo.system.providers]]
id = "kernel:namespace"
principal = '["local","admin"]'""",
    )
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-IDENTITY" in _codes(root)


def test_undeclared_principal_is_rejected(tmp_path: Path) -> None:
    """principal 须解析到 identity.subjects。"""
    manifest = _FULL_MANIFEST.replace(
        'principal = \'["local","ns"]\'', 'principal = \'["local","ghost"]\''
    )
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-IDENTITY" in _codes(root)


def test_missing_binding_record_is_rejected(tmp_path: Path) -> None:
    """每个启用服务恰一条绑定记录。"""
    manifest = _FULL_MANIFEST.replace('id = "kernel:namespace"', 'id = "kernel:other"')
    root = _full(tmp_path, manifest=manifest)
    with pytest.raises(CatalogError, match="E-SYS-IDENTITY.*kernel:namespace.*缺少绑定记录"):
        build_catalog(root)


def test_resolution_scope_must_be_open_to_principal(tmp_path: Path) -> None:
    """scope 的 subjects 须含该 principal。"""
    manifest = _FULL_MANIFEST.replace(
        'principal = \'["local","ns"]\'',
        'principal = \'["local","ns"]\'\nresolution_scope = "main"',
    ).replace(
        'subjects = [\'["local","admin"]\', \'["local","ns"]\']',
        'subjects = [\'["local","admin"]\']',
    )
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-SCOPE" in _codes(root)


def test_kernel_component_must_declare_kernel_section(tmp_path: Path) -> None:
    """system.kernel 组件须声明 [kernel]。"""
    component = _FULL_COMPONENT.replace(
        '[kernel]\nmodule = "ghrah_kernel"\nentry = "_kernel"\n\n', ""
    )
    root = _full(tmp_path, component=component)
    assert "E-SYS-KERNEL" in _codes(root)


def test_host_instance_must_exist(tmp_path: Path) -> None:
    """host_instance 指向存在的实例。"""
    manifest = _FULL_MANIFEST.replace('host_instance = "subject"', 'host_instance = "absent"')
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-HOST" in _codes(root)


def test_host_entry_must_be_declared(tmp_path: Path) -> None:
    """不猜默认入口。"""
    manifest = _FULL_MANIFEST.replace('host_entry = "subject/host"\n', "")
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-HOST" in _codes(root)


def test_multiple_admins_are_rejected_by_callers_rule(tmp_path: Path) -> None:
    """v0 只支持单管理员。"""
    manifest = _FULL_MANIFEST.replace(
        '{ issuer = "local", subject_id = "ns", kind = "service" },',
        '{ issuer = "local", subject_id = "ns", kind = "service" },\n'
        '    { issuer = "local", subject_id = "admin2", kind = "user" },',
    )
    root = _full(tmp_path, manifest=manifest)
    assert "E-SYS-CALLERS" in _codes(root)


def test_first_scope_must_admit_the_admin(tmp_path: Path) -> None:
    manifest = _FULL_MANIFEST.replace(
        'subjects = [\'["local","admin"]\', \'["local","ns"]\']',
        'subjects = [\'["local","ns"]\']',
    )
    root = _full(tmp_path, manifest=manifest)
    with pytest.raises(CatalogError, match=r"scopes\[0\]\.subjects\].*E-SYS-CALLERS"):
        build_catalog(root)


def test_invalid_schema_does_not_hide_other_diagnostics(tmp_path: Path) -> None:
    manifest = _FULL_MANIFEST.replace(
        """scopes = [{
    scope_id = "main",
    namespaces = ["main"],
    subjects = ['["local","admin"]', '["local","ns"]'],
}]""",
        "scopes = 123",
    ).replace("[distro.demo]", '[components.broken]\npath = "broken"\n\n[distro.demo]')
    manifest = manifest.replace('components = ["kernel"]', 'components = ["kernel", "broken"]')
    manifest += """
[[distro.demo.system.providers]]
id = "broken:namespace"
principal = '["local","ns"]'
"""
    component = _FULL_COMPONENT.replace(
        'restart = "never"', 'restart = "on_failure"\nafter = ["broken:namespace"]'
    )
    root = _full(tmp_path, manifest=manifest, component=component)
    broken = root / "broken" / "component.toml"
    broken.parent.mkdir()
    broken.write_text(_FULL_COMPONENT.replace("order = 10", 'order = "high"'), encoding="utf-8")

    with pytest.raises(CatalogError) as excinfo:
        build_catalog(root)
    assert [d.code for d in excinfo.value.diagnostics] == [
        "E-CAT-SKIP",
        "E-CAT-SCHEMA",
        "E-CAT-SCHEMA",
        "E-PROV-POLICY",
    ]


def test_catalog_rejects_undefined_manifest_reference(tmp_path: Path) -> None:
    manifest = _FULL_MANIFEST.replace('roots = ["subject/host"]', 'roots = ["subject/nope"]')
    root = _full(tmp_path, manifest=manifest)
    with pytest.raises(CatalogError, match=r"E-MANIFEST-REF.*demo\.roots.*subject/nope"):
        build_catalog(root)


def test_host_entry_must_belong_to_the_selected_instance(tmp_path: Path) -> None:
    manifest = _FULL_MANIFEST.replace('host_entry = "subject/host"', 'host_entry = "other/host"')
    manifest += '\n[distro.demo.instances.other]\ncomponent = "kernel"\nhost = "process"\n'
    root = _full(tmp_path, manifest=manifest)
    with pytest.raises(CatalogError, match=r"host_entry\].*E-SYS-HOST.*other/host"):
        build_catalog(root)


@pytest.mark.parametrize(
    ("original", "replacement", "field"),
    [
        ('components = ["kernel"]', "components = [7]", r"components\[0\]"),
        ('issuer = "local"', "issuer = 2026-11-08", r"subjects\[0\]\.issuer"),
    ],
)
def test_invalid_field_values_remain_schema_diagnostics(
    tmp_path: Path, original: str, replacement: str, field: str
) -> None:
    root = _full(tmp_path, manifest=_FULL_MANIFEST.replace(original, replacement, 1))
    with pytest.raises(CatalogError, match=rf"{field}\].*E-CAT-SCHEMA"):
        build_catalog(root)


def test_catalog_rejects_component_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _full(root, manifest=_FULL_MANIFEST.replace('path = "kernel"', 'path = "../outside"'))
    (root / "kernel").rename(tmp_path / "outside")
    with pytest.raises(CatalogError, match=r"components\.kernel\.path\].*E-MANIFEST-REF.*越界"):
        build_catalog(root)


# ── 组装 ───────────────────────────────────────────────────


def test_build_catalog_produces_contract_shape(tmp_path: Path) -> None:
    """用固定的预期产物校验完整形状、字段来源与摘要。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    golden = Path(__file__).with_name("catalog_golden.json")
    assert catalog == json.loads(golden.read_text(encoding="utf-8"))


def test_build_catalog_host_and_kernel_come_from_declarations(tmp_path: Path) -> None:
    """kernel.module/entry 来自组件 [kernel]，不是工具链常量。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    assert catalog["host"] == {
        "instance": "subject",
        "component": "kernel",
        "entry": "subject/host",
        "command": ["ghrah-subject-host"],
    }
    assert catalog["kernel"] == {
        "component": "kernel",
        "module": "ghrah_kernel",
        "entry": "_kernel",
    }


def test_build_catalog_service_is_sparse(tmp_path: Path) -> None:
    """可选字段声明了才出现；bindings/configuration 恒有。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    service = catalog["services"][0]
    assert service["order"] == 10
    assert service["restart"] == "never"
    # 未声明的可选字段不得出现
    for absent in ("after", "requires_ready", "max_restarts", "window_seconds", "health"):
        assert absent not in service
    assert service["bindings"] == []
    assert service["configuration"] == {}


def test_build_catalog_binding_comes_from_deployer_record(tmp_path: Path) -> None:
    """binding 唯一来源是 system.providers；diagnostics 恒 false。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    binding = catalog["services"][0]["binding"]
    assert binding == {
        "principal": '["local","ns"]',
        "resolution_scope": None,
        "diagnostics": False,
    }


def test_build_catalog_callers_derived_from_single_admin(tmp_path: Path) -> None:
    """首个 user 主体 + 首个 scope；只有 control 带诊断许可。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    assert catalog["callers"]["control"] == {
        "principal": '["local","admin"]',
        "resolution_scope": "main",
        "diagnostics": True,
    }
    assert catalog["callers"]["observer"]["diagnostics"] is False
    assert catalog["callers"]["peer"]["diagnostics"] is False


def test_build_catalog_schemes_become_array_in_declaration_order(tmp_path: Path) -> None:
    """catalog 的 schemes 是数组（声明里是对象），名为 name 的字段随行。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    assert catalog["services"][0]["schemes"] == [
        {
            "name": "ns",
            "contract": "ghrah.namespace/v0",
            "actions": ["bind", "unbind", "inspect"],
        }
    ]


def test_build_catalog_digest_is_recomputable(tmp_path: Path) -> None:
    """校验方重算摘要，不得只信文件里的值。"""
    root = _full(tmp_path)
    catalog = build_catalog(root)
    assert catalog["digest"] == catalog_digest(catalog)
    tampered = dict(catalog, distro="other")
    assert catalog_digest(tampered) != catalog["digest"]


def test_build_catalog_keeps_placeholder_unexpanded(tmp_path: Path) -> None:
    """占位符原样进 catalog，由宿主在映射时展开。"""
    root = _full(tmp_path, component=_component_with_configuration())
    (root / "kernel" / "ns.json").write_text(
        json.dumps({"database": "${DATA_ROOT}/agent/main.db"}), encoding="utf-8"
    )
    catalog = build_catalog(root)
    assert catalog["services"][0]["configuration"] == {"database": "${DATA_ROOT}/agent/main.db"}


def test_build_catalog_raises_with_all_diagnostics(tmp_path: Path) -> None:
    """同一份清单按类别顺序报出六类错误。"""
    component = (
        _component_with_configuration().replace(
            'restart = "never"',
            'restart = "on_failure"\nafter = ["kernel:absent"]',
        )
        + """
[providers.namespace.bindings.main]
path = "/ns/main"
scheme = "ns"
scope = "missing"
namespace = "main"

[providers.other]
module = "dummy"
provider = "other"
kind = "namespace"
order = 20

[providers.other.schemes.ns]
contract = "ghrah.namespace/v0"
actions = ["bind"]
"""
    )
    manifest = (
        _FULL_MANIFEST
        + """
[[distro.demo.system.providers]]
id = "kernel:other"
principal = '["local","ns"]'
"""
    )
    root = _full(tmp_path, manifest=manifest, component=component)
    (root / "kernel" / "ns.json").write_text('{"database":"/tmp/db"}', encoding="utf-8")
    with pytest.raises(CatalogError) as excinfo:
        build_catalog(root)
    assert excinfo.value.exit_code == 3
    assert [d.code for d in excinfo.value.diagnostics] == [
        "E-PROV-ACTION-DUP",
        "E-PROV-DEPENDENCY",
        "E-PROV-NAMESPACE",
        "E-PROV-POLICY",
        "E-PROV-BINDING",
        "E-PROV-CONFIG",
    ]


def test_build_catalog_is_deterministic(tmp_path: Path) -> None:
    """同一份声明两次构建必须得到同一摘要（可复现的代指纹前提）。"""
    root = _full(tmp_path)
    first = build_catalog(root)
    second = build_catalog(root)
    assert first["digest"] == second["digest"]
    assert canonical_json(first) == canonical_json(second)


@pytest.mark.parametrize("case", ["valid", "absent", "missing", "unknown", "ttl", "access"])
def test_identity_schema_resolves_offline_and_matches_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_kw: pytest.fail("network lookup"))
    root = _full(tmp_path)
    catalog = build_catalog(root)
    workspace = tomllib.loads(_FULL_MANIFEST)
    for kind, document, table in (
        ("workspace", workspace, workspace["distro"]["demo"]),
        ("catalog", catalog, catalog),
    ):
        identity = table["identity"]
        if case == "absent":
            del table["identity"]
        elif case == "missing":
            del identity["access"]
        elif case == "unknown":
            identity["unexpected"] = True
        elif case == "ttl":
            identity["grant_lifetime"] = {"mode": "ttl", "ttl_seconds": 0}
        elif case == "access":
            identity["access"] = [
                {
                    "rule_id": "rule",
                    "namespace_id": "main",
                    "resource": "/item",
                    "actions": ["read"],
                    "subject": '["local","admin"]',
                    "group": "team",
                }
            ]
        if case == "valid" or (case == "absent" and kind == "workspace"):
            _validate(document, kind, root / kind)
        else:
            with pytest.raises(ManifestError, match="identity"):
                _validate(document, kind, root / kind)
