# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# SPDX-License-Identifier: Apache-2.0

"""代内 catalog 的构建期汇总、校验与摘要计算。

本模块只做**纯计算 + 读文件**：不碰安装域、不 spawn 进程。它把各组件
``component.toml`` 的 ``[providers]``/``[kernel]``、发行版的 ``system``/``identity``
按契约 §5.10 的字段来源总表汇总成代内 ``catalog.json``。

诊断分两层，口径不可混：

- ``E-PROV-*`` 六类：验证「包作者把声明写对了没有」；
- ``E-SYS-*``：验证「部署者把谁指派给了谁、身份集是否支撑得住、被选中的内核
  组件有没有声明入口」。

两者都走契约 §4.2 的格式，按 §4.1 **一次报全**，不 fail-fast。
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ghrah_toolchain.errors import ToolchainError
from ghrah_toolchain.manifest import ManifestError, _validate, load_manifest

CATALOG_SCHEMA = 1
"""catalog 的契约版本（契约 §5.3：v0 固定为 1）。"""

MAX_CONFIGURATION_BYTES = 65536
"""单个服务 ``configuration`` 的字节上限（契约 §4.3.6 第 5 条、§5.8）。"""

_PROVIDER_DIAGNOSTIC_ORDER = {
    "E-PROV-ACTION-DUP": 1,
    "E-PROV-DEPENDENCY": 2,
    "E-PROV-NAMESPACE": 3,
    "E-PROV-POLICY": 4,
    "E-PROV-BINDING": 5,
    "E-PROV-CONFIG": 6,
}


@dataclass(frozen=True)
class Diagnostic:
    """一条构建期诊断（契约 §4.2 冻结格式）。

    ``file`` 是相对工作区根的路径（如 ``ghrah-kernel/component.toml``），
    ``path`` 是字段路径（如 ``providers.namespace.restart``），顶层错误用空串。
    位置永远是「文件 + 字段路径」，能直接跳转，不允许只报服务 id。
    """

    file: str
    path: str
    code: str
    message: str

    def render(self) -> str:
        return f"{self.file}:[{self.path}] {self.code}: {self.message}"

    def sort_key(self) -> tuple[int, str, str, str]:
        """六类 provider 诊断按类别排列，类内按文件和字段排列。"""
        return (_PROVIDER_DIAGNOSTIC_ORDER.get(self.code, 0), self.file, self.path, self.code)


class CatalogError(ToolchainError):
    """构建期 catalog 汇总失败；消息为逐行 render 的全部诊断。"""

    exit_code = 3

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = list(diagnostics)
        super().__init__("\n".join(d.render() for d in self.diagnostics))


def canonical_json(value: object) -> str:
    """契约 §5.9 的规范化：键排序、数组保序、紧凑分隔符、不转义非 ASCII。"""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def catalog_digest(catalog: dict[str, Any]) -> str:
    """摘要 = sha256(canonical_json(删除 ``digest`` 后的整份 catalog))。"""
    body = {key: value for key, value in catalog.items() if key != "digest"}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ── schema 聚合（算法第 1–2 步）─────────────────────────────


def _schema(kind: str) -> dict[str, Any]:
    """从打包资源读输入清单的 schema。"""
    resource = files("ghrah_toolchain").joinpath("schemas", f"{kind}-v1.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _field_path(error: Any) -> str:
    """把 jsonschema 的 absolute_path 渲染成 ``a.b[0].c`` 形式。

    ``required`` 类错误指向的是**父对象**，字段路径要补上缺失的键名，
    否则「文件 + 字段路径」就跳不到真正缺的那一行。
    """
    parts: list[str] = []
    for item in error.absolute_path:
        parts.append(f"[{item}]" if isinstance(item, int) else (f".{item}" if parts else str(item)))
    base = "".join(parts)
    if error.validator == "required":
        for key in error.validator_value:
            if error.message == f"{key!r} is a required property":
                return f"{base}.{key}" if base else key
    return base


def _schema_diagnostics(
    data: Any,
    kind: str,
    file: str,
    *,
    code: str,
) -> list[Diagnostic]:
    """收集一份文档的**全部** schema 错误（不 fail-fast）。

    契约 §4.1 明确要求「不得因为前面有 schema 错误就跳过后续检查」，
    因此这里不用 ``manifest._validate`` 的取首错语义。
    """
    validator = Draft202012Validator(_schema(kind))
    errors = sorted(
        validator.iter_errors(data),
        key=lambda e: (_field_path(e), e.message),
    )
    return [
        Diagnostic(file=file, path=_field_path(error), code=code, message=error.message)
        for error in errors
    ]


def _read_toml(path: Path, relative: str) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """读 TOML 原文；解析失败折算为一条诊断而不是抛异常，保证仍能一次报全。"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except (OSError, ValueError) as exc:
        return None, [
            Diagnostic(file=relative, path="", code="E-CAT-PARSE", message=f"无法解析清单: {exc}")
        ]
    if not isinstance(data, dict):
        return None, [
            Diagnostic(file=relative, path="", code="E-CAT-PARSE", message="清单顶层必须是对象")
        ]
    return data, []


def collect_schema_diagnostics(
    root: Path,
) -> tuple[dict[str, Any] | None, dict[str, dict], list[Diagnostic]]:
    """算法第 1–2 步：读原始清单、聚合 workspace 与各组件 schema 错误。

    返回 ``(workspace_data, components, diagnostics)``。``workspace_data`` 在
    ghrah.toml 无法解析时为 ``None``；``components`` 只含通过 schema 的组件。
    """
    diagnostics: list[Diagnostic] = []
    manifest_path = root / "ghrah.toml"
    data, read_errors = _read_toml(manifest_path, "ghrah.toml")
    diagnostics.extend(read_errors)
    if data is None:
        return None, {}, diagnostics

    diagnostics.extend(_schema_diagnostics(data, "workspace", "ghrah.toml", code="E-CAT-SCHEMA"))

    components: dict[str, dict] = {}
    raw_components = data.get("components")
    if isinstance(raw_components, dict):
        workspace_root = root.resolve()
        for ident, source in sorted(raw_components.items()):
            if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                continue  # schema 已报错，这里只做能做的
            relative = f"{source['path']}/component.toml"
            component_root = (workspace_root / source["path"]).resolve()
            component_path = (component_root / "component.toml").resolve()
            if not component_root.is_relative_to(
                workspace_root
            ) or not component_path.is_relative_to(component_root):
                diagnostics.append(
                    Diagnostic(
                        "ghrah.toml",
                        f"components.{ident}.path",
                        "E-MANIFEST-REF",
                        f"组件清单路径越界: {relative}",
                    )
                )
                continue
            component, component_read_errors = _read_toml(component_path, relative)
            diagnostics.extend(component_read_errors)
            if component is None:
                continue
            schema_errors = _schema_diagnostics(
                component, "component", relative, code="E-CAT-SCHEMA"
            )
            if schema_errors:
                diagnostics.extend(schema_errors)
                diagnostics.append(
                    Diagnostic(
                        relative, "", "E-CAT-SKIP", "组件未通过 schema，跳过该组件的语义检查"
                    )
                )
            else:
                components[ident] = component
    return data, components, diagnostics


# ── 语义校验（算法第 5 步：六类 + §4.6）──────────────────────


@dataclass(frozen=True)
class ServiceView:
    """一个已启用服务在构建期的装配视图（尚未落成 catalog 条目）。

    服务集合 = ``distro.components`` 选中组件的 ``[providers]`` 全展开。
    排序键严格按契约 §5.5：``(order 缺省 0, distro.components 索引, 组件内声明序)``
    ——只按 order 排会在多组件时得到不稳定顺序。
    """

    id: str
    component_id: str
    provider_key: str
    declaration: dict[str, Any]
    component_index: int
    declaration_index: int

    @property
    def order(self) -> int:
        return int(self.declaration.get("order", 0))

    @property
    def kind(self) -> str:
        return str(self.declaration.get("kind", ""))

    @property
    def schemes(self) -> dict[str, Any]:
        schemes = self.declaration.get("schemes")
        return schemes if isinstance(schemes, dict) else {}

    @property
    def bindings(self) -> dict[str, Any]:
        bindings = self.declaration.get("bindings")
        return bindings if isinstance(bindings, dict) else {}

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.order, self.component_index, self.declaration_index)


def _component_relpath(data: dict[str, Any], ident: str) -> str:
    """组件声明的相对路径：诊断要能直接跳到那一份 component.toml。"""
    source = data.get("components", {}).get(ident, {})
    path = source.get("path") if isinstance(source, dict) else None
    return f"{path}/component.toml" if isinstance(path, str) else f"{ident}/component.toml"


def enabled_services(
    data: dict[str, Any],
    components: dict[str, dict],
    distro_name: str,
) -> list[ServiceView]:
    """按 §5.5 的排序键列出该发行版启用的全部服务。"""
    distro = data.get("distro", {}).get(distro_name, {})
    selected = distro.get("components", [])
    if not isinstance(selected, list):
        return []
    services: list[ServiceView] = []
    for component_index, ident in enumerate(selected):
        component = components.get(ident) if isinstance(ident, str) else None
        if not isinstance(component, dict):
            continue  # schema 未通过者已单独报错，语义检查跳过
        providers = component.get("providers")
        if not isinstance(providers, dict):
            continue
        for declaration_index, (provider_key, declaration) in enumerate(providers.items()):
            if not isinstance(declaration, dict):
                continue
            services.append(
                ServiceView(
                    id=f"{ident}:{provider_key}",
                    component_id=ident,
                    provider_key=provider_key,
                    declaration=declaration,
                    component_index=component_index,
                    declaration_index=declaration_index,
                )
            )
    return sorted(services, key=lambda s: s.sort_key)


def _subject_ref(subject: dict[str, Any]) -> str:
    """Subject.ref 的紧凑 JSON 形状，与声明里的 principal 可直接比对。"""
    return json.dumps(
        [str(subject.get("issuer")), str(subject.get("subject_id"))],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _check_action_dup(data: dict[str, Any], services: list[ServiceView]) -> list[Diagnostic]:
    """§4.3.1：``(contract, action)`` 全表唯一。跨组件、跨 provider、多 scheme 都算。"""
    diagnostics: list[Diagnostic] = []
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for service in services:
        file = _component_relpath(data, service.component_id)
        for scheme_name, scheme in service.schemes.items():
            if not isinstance(scheme, dict):
                continue
            contract = scheme.get("contract")
            for action in scheme.get("actions", []):
                key = (str(contract), str(action))
                if key in owners:
                    owner_id, owner_path = owners[key]
                    diagnostics.append(
                        Diagnostic(
                            file=file,
                            path=f"providers.{service.provider_key}.schemes.{scheme_name}.actions",
                            code="E-PROV-ACTION-DUP",
                            message=(
                                f"契约动作 ({contract}, {action}) 已被 {owner_id} 声明"
                                f"（见 {owner_path}）；同一 (contract, action) 只能有一个声明者"
                            ),
                        )
                    )
                else:
                    owners[key] = (
                        service.id,
                        f"{file}:[providers.{service.provider_key}.schemes.{scheme_name}.actions]",
                    )
    return diagnostics


def _check_dependency(
    data: dict[str, Any], services: list[ServiceView], skipped_components: set[str]
) -> list[Diagnostic]:
    """§4.3.2：``after``/``requires_ready`` 目标须为已启用 id，且依赖图无环。"""
    diagnostics: list[Diagnostic] = []
    known = {service.id for service in services}
    graph: dict[str, set[str]] = {}
    for service in services:
        file = _component_relpath(data, service.component_id)
        graph[service.id] = set()
        for field in ("after", "requires_ready"):
            targets = service.declaration.get(field)
            if not isinstance(targets, list):
                continue
            for target in targets:
                if target in known:
                    graph[service.id].add(target)
                elif target.partition(":")[0] not in skipped_components:
                    listed = "、".join(sorted(known)) or "（无）"
                    diagnostics.append(
                        Diagnostic(
                            file=file,
                            path=f"providers.{service.provider_key}.{field}",
                            code="E-PROV-DEPENDENCY",
                            message=(f'引用未启用或未声明的服务 "{target}"（已启用：{listed}）'),
                        )
                    )

    # 无环检查：报告实际环路，错误信息要能照着走
    state: dict[str, int] = {}  # 1=访问中, 2=已完成
    reported = False

    def visit(node: str, stack: list[str]) -> None:
        nonlocal reported
        if reported:
            return
        if state.get(node) == 1:
            cycle = stack[stack.index(node) :] + [node]
            # 在「引用方」报错：取环中首个服务的声明组件
            diagnostics.append(
                Diagnostic(
                    file=_component_relpath(data, node.split(":")[0]),
                    path=f"providers.{node.split(':', 1)[1]}.after" if ":" in node else "",
                    code="E-PROV-DEPENDENCY",
                    message="after/requires_ready 依赖成环，涉及 " + "→".join(cycle),
                )
            )
            reported = True
            return
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in sorted(graph.get(node, ())):
            visit(dep, stack + [node])
        state[node] = 2

    for node in sorted(graph):
        visit(node, [])
    return diagnostics


def _check_namespace(
    data: dict[str, Any], services: list[ServiceView], distro_name: str, *, complete: bool
) -> list[Diagnostic]:
    """§4.3.3：排序后首位必须是 namespace，且全表恰一个。"""
    diagnostics: list[Diagnostic] = []
    namespaces = [service for service in services if service.kind == "namespace"]
    if len(namespaces) > 1 or (complete and not namespaces):
        listed = "、".join(service.id for service in namespaces)
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.system.providers",
                code="E-PROV-NAMESPACE",
                message=f"命名服务必须唯一，当前 {len(namespaces)} 个：{listed}",
            )
        )
    if namespaces and (first := services[0]).kind != "namespace":
        head = namespaces[0]
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.system.providers",
                code="E-PROV-NAMESPACE",
                message=(
                    "命名服务必须是首位；"
                    f"{first.id}(order={first.order}) 早于 {head.id}(order={head.order})"
                ),
            )
        )
    return diagnostics


def _check_policy(data: dict[str, Any], services: list[ServiceView]) -> list[Diagnostic]:
    """§4.3.4 的汇总层两条：namespace ⇒ never；close_action 成员属于本服务 schemes。"""
    diagnostics: list[Diagnostic] = []
    for service in services:
        file = _component_relpath(data, service.component_id)
        if service.kind == "namespace":
            restart = service.declaration.get("restart", "never")
            if restart != "never":
                diagnostics.append(
                    Diagnostic(
                        file=file,
                        path=f"providers.{service.provider_key}.restart",
                        code="E-PROV-POLICY",
                        message=f'kind="namespace" 必须 restart="never"（当前 "{restart}"）',
                    )
                )
        close_action = service.declaration.get("close_action")
        if isinstance(close_action, list) and len(close_action) == 2:
            scheme_name, action = str(close_action[0]), str(close_action[1])
            scheme = service.schemes.get(scheme_name)
            allowed = scheme.get("actions", []) if isinstance(scheme, dict) else []
            if action not in allowed:
                options = "、".join(str(a) for a in allowed) or "（该 scheme 无动作）"
                diagnostics.append(
                    Diagnostic(
                        file=file,
                        path=f"providers.{service.provider_key}.close_action",
                        code="E-PROV-POLICY",
                        message=(
                            f"关闭动作 ({scheme_name}, {action}) 不属于本服务声明的 "
                            f"schemes.{scheme_name}.actions（可选：{options}）"
                        ),
                    )
                )
    return diagnostics


def _check_bindings(
    data: dict[str, Any],
    services: list[ServiceView],
    identity: dict[str, Any] | None,
) -> list[Diagnostic]:
    """§4.3.5：bindings 的 scope/namespace 须在 identity 中，scheme 须是本服务的键。"""
    diagnostics: list[Diagnostic] = []
    scope_records = (identity or {}).get("scopes")
    namespace_records = (identity or {}).get("namespaces")
    scopes = (
        {
            scope.get("scope_id")
            for scope in scope_records
            if isinstance(scope, dict) and isinstance(scope.get("scope_id"), str)
        }
        if isinstance(scope_records, list)
        else None
    )
    namespaces = (
        {
            ns.get("namespace_id")
            for ns in namespace_records
            if isinstance(ns, dict) and isinstance(ns.get("namespace_id"), str)
        }
        if isinstance(namespace_records, list)
        else None
    )
    for service in services:
        file = _component_relpath(data, service.component_id)
        for binding_name, binding in service.bindings.items():
            if not isinstance(binding, dict):
                continue
            path = f"providers.{service.provider_key}.bindings.{binding_name}"
            scheme_name = str(binding.get("scheme"))
            if scheme_name not in service.schemes:
                options = "、".join(service.schemes) or "（无）"
                diagnostics.append(
                    Diagnostic(
                        file=file,
                        path=f"{path}.scheme",
                        code="E-PROV-BINDING",
                        message=(f'引用本服务未声明的 scheme "{scheme_name}"（已声明：{options}）'),
                    )
                )
            if scopes is not None:
                scope = str(binding.get("scope"))
                if scope not in scopes:
                    listed = "、".join(sorted(scopes)) or "（无）"
                    diagnostics.append(
                        Diagnostic(
                            file=file,
                            path=f"{path}.scope",
                            code="E-PROV-BINDING",
                            message=(
                                f'引用未声明的解析 Scope "{scope}"（identity.scopes 仅有 {listed}）'
                            ),
                        )
                    )
            if namespaces is not None:
                namespace = str(binding.get("namespace"))
                if namespace not in namespaces:
                    listed = "、".join(sorted(namespaces)) or "（无）"
                    diagnostics.append(
                        Diagnostic(
                            file=file,
                            path=f"{path}.namespace",
                            code="E-PROV-BINDING",
                            message=(
                                f'引用未声明的 Namespace "{namespace}"'
                                f"（identity.namespaces 仅有 {listed}）"
                            ),
                        )
                    )
    return diagnostics


_RESERVED_CONFIG_KEYS = ("provider", "identities", "administrator", "principal")
_RESERVED_CONFIG_TOKENS = (
    "--provider-fd",
    "--caller-fd",
    "--credential",
    "--enrollment",
    "--config-json",
)


def _walk_strings(value: Any, path: str) -> list[tuple[str, str]]:
    """遍历 configuration 里的全部字符串值，带回字段路径。"""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    return found


def _is_absolute(value: str) -> bool:
    """展开前的绝对路径一律拒绝；Windows 盘符与 UNC 一并覆盖。"""
    if value.startswith("/") or value.startswith("\\\\"):
        return True
    return len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "\\/"


def _check_configuration(
    root: Path,
    data: dict[str, Any],
    components: dict[str, dict],
    services: list[ServiceView],
) -> list[Diagnostic]:
    """§4.3.6：JSON 对象、无绝对路径、无保留参数、无保留键、未展开 ≤65536 字节。"""
    diagnostics: list[Diagnostic] = []
    for service in services:
        file = _component_relpath(data, service.component_id)
        path = f"providers.{service.provider_key}.configuration"
        relative = service.declaration.get("configuration")
        if relative is None:
            continue
        if not isinstance(relative, str):
            continue  # schema 已报类型错误
        source = data.get("components", {}).get(service.component_id, {})
        component_root = root / str(source.get("path", service.component_id))
        target = (component_root / relative).resolve()
        if not target.is_relative_to(component_root.resolve()):
            diagnostics.append(
                Diagnostic(
                    file=file,
                    path=path,
                    code="E-PROV-CONFIG",
                    message=f'配置路径 "{relative}" 越出组件根',
                )
            )
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            diagnostics.append(
                Diagnostic(
                    file=file,
                    path=path,
                    code="E-PROV-CONFIG",
                    message=f"配置 {relative} 无法读取或不是合法 JSON: {exc}",
                )
            )
            continue
        if not isinstance(payload, dict):
            diagnostics.append(
                Diagnostic(
                    file=file,
                    path=path,
                    code="E-PROV-CONFIG",
                    message=(
                        f"配置 {relative} 的顶层必须是 JSON 对象（当前 {type(payload).__name__}）"
                    ),
                )
            )
            continue
        for key in payload:
            if key in _RESERVED_CONFIG_KEYS:
                diagnostics.append(
                    Diagnostic(
                        file=file,
                        path=f"{path}.{key}",
                        code="E-PROV-CONFIG",
                        message=(
                            f'保留键 "{key}" 不得出现在声明里；'
                            "服务运行主体由 system.providers[].principal 指派，映射时注入"
                        ),
                    )
                )
        for field_path, value in _walk_strings(payload, ""):
            if _is_absolute(value):
                diagnostics.append(
                    Diagnostic(
                        file=file,
                        path=f"{path}.{field_path}" if field_path else path,
                        code="E-PROV-CONFIG",
                        message=(
                            f'含绝对路径 "{value}"；'
                            "请改用 ${DATA_ROOT}/${DISTRO_ROOT}/${GENERATION_ID}"
                        ),
                    )
                )
            for token in _RESERVED_CONFIG_TOKENS:
                if token in value:
                    diagnostics.append(
                        Diagnostic(
                            file=file,
                            path=f"{path}.{field_path}" if field_path else path,
                            code="E-PROV-CONFIG",
                            message=(
                                f'含宿主保留参数 "{token}"；连接参数由内核注入，不得出现在声明中'
                            ),
                        )
                    )
                    break
        try:
            size = len(canonical_json(payload).encode("utf-8"))
        except ValueError as exc:
            diagnostics.append(Diagnostic(file, path, "E-PROV-CONFIG", f"配置不是有效 JSON: {exc}"))
            continue
        if size > MAX_CONFIGURATION_BYTES:
            diagnostics.append(
                Diagnostic(
                    file=file,
                    path=path,
                    code="E-PROV-CONFIG",
                    message=f"展开前配置 {size} 字节，超过上限 {MAX_CONFIGURATION_BYTES}",
                )
            )
    return diagnostics


# ── §4.6 系统声明校验（部署者视角，独立前缀）─────────────────


def _check_system_identity(
    data: dict[str, Any],
    services: list[ServiceView],
    distro_name: str,
    system: dict[str, Any] | None,
    identity: dict[str, Any] | None,
    skipped_components: set[str],
) -> list[Diagnostic]:
    """§4.6 第 1–4 条：绑定记录齐备、principal 可解析、主体种类合法。"""
    diagnostics: list[Diagnostic] = []
    records = (system or {}).get("providers")
    if not isinstance(records, list):
        return diagnostics
    prefix = f"distro.{distro_name}.system.providers"

    seen: dict[str, int] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        ident = str(record.get("id", ""))
        if ident in seen:
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=f"{prefix}[{index}].id",
                    code="E-SYS-IDENTITY",
                    message=f'服务 "{ident}" 的绑定记录重复',
                )
            )
        else:
            seen[ident] = index

    # 每个启用服务恰一条记录
    enabled = {service.id for service in services}
    for ident in sorted(enabled - set(seen)):
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=prefix,
                code="E-SYS-IDENTITY",
                message=(
                    f"服务 {ident} 已启用但缺少绑定记录；每个启用服务必须恰有一条 providers 记录"
                ),
            )
        )
    for ident in sorted(set(seen) - enabled):
        if ident.partition(":")[0] in skipped_components:
            continue
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"{prefix}[{seen[ident]}].id",
                code="E-SYS-IDENTITY",
                message=(
                    f'服务 "{ident}" 不在已启用的 provider 集合中'
                    f"（已启用：{'、'.join(sorted(enabled)) or '（无）'}）"
                ),
            )
        )

    subjects = (identity or {}).get("subjects")
    if not isinstance(subjects, list):
        return diagnostics
    by_ref = {_subject_ref(subject): subject for subject in subjects if isinstance(subject, dict)}
    declared = "、".join(sorted(by_ref)) or "（无）"
    for ident, index in sorted(seen.items()):
        record = records[index]
        principal = str(record.get("principal", ""))
        subject = by_ref.get(principal)
        path = f"{prefix}[{index}].principal"
        if subject is None:
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=path,
                    code="E-SYS-IDENTITY",
                    message=f"主体 {principal} 未在 identity.subjects 中声明（已声明：{declared}）",
                )
            )
            continue
        if subject.get("kind") == "user":
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=path,
                    code="E-SYS-IDENTITY",
                    message=(
                        f"服务运行主体不能是 user 主体（当前 {principal}）；"
                        "请在 identity.subjects 里声明 service 或 agent 主体"
                    ),
                )
            )
    return diagnostics


def _check_system_scope(
    data: dict[str, Any],
    distro_name: str,
    system: dict[str, Any] | None,
    identity: dict[str, Any] | None,
) -> list[Diagnostic]:
    """§4.6 第 5 条：给出 resolution_scope 时，scope 已声明且对该 principal 开放。"""
    diagnostics: list[Diagnostic] = []
    prefix = f"distro.{distro_name}.system.providers"
    scope_records = (identity or {}).get("scopes")
    records = (system or {}).get("providers")
    if not isinstance(scope_records, list) or not isinstance(records, list):
        return diagnostics
    scopes = {
        str(scope.get("scope_id")): scope for scope in scope_records if isinstance(scope, dict)
    }
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        scope_name = record.get("resolution_scope")
        if scope_name is None:
            continue
        scope_name = str(scope_name)
        scope = scopes.get(scope_name)
        if scope is None:
            listed = "、".join(sorted(scopes)) or "（无）"
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=f"{prefix}[{index}].resolution_scope",
                    code="E-SYS-SCOPE",
                    message=f'解析 Scope "{scope_name}" 未声明（identity.scopes 仅有 {listed}）',
                )
            )
            continue
        allowed = scope.get("subjects")
        principal = str(record.get("principal", ""))
        if isinstance(allowed, list) and principal not in allowed:
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=f"{prefix}[{index}].resolution_scope",
                    code="E-SYS-SCOPE",
                    message=(
                        f'解析 Scope "{scope_name}" 未对主体 {principal} 开放'
                        "（scope.subjects 不含该主体）"
                    ),
                )
            )
    return diagnostics


def _check_kernel_entry(
    data: dict[str, Any],
    components: dict[str, dict],
    distro_name: str,
    system: dict[str, Any] | None,
    selected: list[str],
) -> list[Diagnostic]:
    """§4.6 第 7 条：system.kernel 选中组件须声明形状合法的 [kernel]。"""
    diagnostics: list[Diagnostic] = []
    kernel_id = str((system or {}).get("kernel", ""))
    path = f"distro.{distro_name}.system.kernel"
    if kernel_id not in selected:
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=path,
                code="E-SYS-KERNEL",
                message=(
                    f'内核组件 "{kernel_id}" 未在发行版的 components 中'
                    f"（已选：{'、'.join(selected) or '（无）'}）"
                ),
            )
        )
        return diagnostics
    component = components.get(kernel_id)
    if not isinstance(component, dict):
        return diagnostics  # schema 未通过，已单独报错
    declaration = component.get("kernel")
    if not isinstance(declaration, dict):
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=path,
                code="E-SYS-KERNEL",
                message=(
                    f"内核组件 {kernel_id} 未声明 [kernel]（module/entry）；构建期不猜测默认入口"
                ),
            )
        )
    return diagnostics


def _check_host_entry(
    data: dict[str, Any],
    components: dict[str, dict],
    distro_name: str,
    system: dict[str, Any] | None,
) -> list[Diagnostic]:
    """§4.6 第 8 条：host_instance 存在、host_entry 指向 process 条目、argv 非空无绝对路径。"""
    diagnostics: list[Diagnostic] = []
    distro = data.get("distro", {}).get(distro_name, {})
    instances = distro.get("instances", {})
    if not isinstance(instances, dict):
        return diagnostics
    instance_name = str((system or {}).get("host_instance", ""))
    path = f"distro.{distro_name}.system.host_instance"
    instance = instances.get(instance_name)
    if not isinstance(instance, dict):
        listed = "、".join(sorted(instances)) or "（无）"
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=path,
                code="E-SYS-HOST",
                message=f'实例 "{instance_name}" 未在 instances 中声明（已声明：{listed}）',
            )
        )
        return diagnostics

    entry_ref = instance.get("host_entry")
    if not isinstance(entry_ref, str):
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.instances.{instance_name}.host_entry",
                code="E-SYS-HOST",
                message=f"宿主实例 {instance_name} 未声明 host_entry；构建期不猜测默认入口",
            )
        )
        return diagnostics

    component_id = instance.get("component")
    selected = distro.get("components")
    if (
        isinstance(component_id, str)
        and isinstance(selected, list)
        and component_id not in selected
    ):
        return [
            Diagnostic(
                "ghrah.toml",
                f"distro.{distro_name}.instances.{instance_name}.component",
                "E-SYS-HOST",
                f"宿主实例的组件 {component_id!r} 未在发行版的 components 中选中",
            )
        ]
    component = components.get(component_id) if isinstance(component_id, str) else None
    if not isinstance(component, dict):
        return diagnostics
    target_instance, _, entry_id = entry_ref.partition("/")
    entry = component.get("entries", {}).get(entry_id) if target_instance == instance_name else None
    if not isinstance(entry, dict):
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.instances.{instance_name}.host_entry",
                code="E-SYS-HOST",
                message=f'host_entry "{entry_ref}" 必须指向实例 {instance_name!r} 的已声明入口',
            )
        )
        return diagnostics
    if entry.get("kind") != "process":
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.instances.{instance_name}.host_entry",
                code="E-SYS-HOST",
                message=(
                    f'host_entry "{entry_ref}" 的 kind 必须是 process（当前 "{entry.get("kind")}"）'
                ),
            )
        )
        return diagnostics
    argv = entry.get("argv") or []
    if not argv:
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.instances.{instance_name}.host_entry",
                code="E-SYS-HOST",
                message=f'host_entry "{entry_ref}" 的 argv 为空；catalog 不写死宿主入口',
            )
        )
    for index, item in enumerate(argv):
        if _is_absolute(str(item)):
            diagnostics.append(
                Diagnostic(
                    file="ghrah.toml",
                    path=f"distro.{distro_name}.instances.{instance_name}.host_entry",
                    code="E-SYS-HOST",
                    message=f"host.command 元素不得是绝对路径（argv[{index}] = {item!r}）",
                )
            )
    return diagnostics


def _check_callers(identity: dict[str, Any] | None, distro_name: str) -> list[Diagnostic]:
    """校验唯一管理员及其对首个 scope 的访问权限。"""
    subjects = (identity or {}).get("subjects", [])
    if not isinstance(subjects, list):
        return []
    users = [s for s in subjects if isinstance(s, dict) and s.get("kind") == "user"]
    if len(users) == 1:
        scopes = (identity or {}).get("scopes")
        if isinstance(scopes, list) and scopes and isinstance(scopes[0], dict):
            scope = scopes[0]
            members = scope.get("subjects")
            principal = _subject_ref(users[0])
            if isinstance(members, list) and principal not in members:
                return [
                    Diagnostic(
                        "ghrah.toml",
                        f"distro.{distro_name}.identity.scopes[0].subjects",
                        "E-SYS-CALLERS",
                        f"首个 scope {scope.get('scope_id')!r} 未向管理员 {principal} 开放",
                    )
                ]
        return []
    listed = "、".join(_subject_ref(s) for s in users) or "（无）"
    return [
        Diagnostic(
            file="ghrah.toml",
            path=f"distro.{distro_name}.identity.subjects",
            code="E-SYS-CALLERS",
            message=(
                f"v0 只支持单管理员：kind == user 的主体须恰一个（当前 {len(users)} 个：{listed}）"
            ),
        )
    ]


# ── 发行版解析与总入口（算法第 3–6 步）───────────────────────


def resolve_distro(
    data: dict[str, Any], distro: str | None, diagnostics: list[Diagnostic]
) -> str | None:
    """算法第 3 步：``--distro`` 优先，缺省取 ``workspace.default_distro``；都没有则拒绝。

    契约 §5.3 明确「不猜」：不猜默认发行版，也不在指定名不存在时回退。
    """
    distros = data.get("distro", {})
    workspace = data.get("workspace", {})
    if not isinstance(distros, dict) or (distro is None and not isinstance(workspace, dict)):
        return None  # schema 已报告类型错误
    declared = sorted(distros)
    chosen = distro if distro is not None else workspace.get("default_distro")
    if chosen is None:
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path="workspace.default_distro",
                code="E-CAT-DISTRO",
                message=(
                    "未指定发行版且 workspace.default_distro 未声明；"
                    f"请用 --distro 指定（已声明：{'、'.join(declared) or '（无）'}）"
                ),
            )
        )
        return None
    if not isinstance(chosen, str):
        return None
    if chosen not in distros:
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path="distro",
                code="E-CAT-DISTRO",
                message=(
                    f'发行版 "{chosen}" 未在 [distro] 中声明'
                    f"（已声明：{'、'.join(declared) or '（无）'}）"
                ),
            )
        )
        return None
    return chosen


def validate_semantics(
    root: Path,
    data: dict[str, Any],
    components: dict[str, dict],
    distro_name: str,
    diagnostics: list[Diagnostic],
) -> None:
    """算法第 4–5 步：系统声明可用性 + 六类 + §4.6，全部追加进 ``diagnostics``。

    schema 未通过的组件已在收集阶段报错，这里只检查通过 schema 的组件。
    """
    distro = data.get("distro", {}).get(distro_name, {})
    if not isinstance(distro, dict):
        return  # schema 已报告类型错误
    system = distro.get("system")
    identity = distro.get("identity")
    selected = distro.get("components", [])
    declared_components = data.get("components", {})
    skipped_components = (
        {ident for ident in selected if isinstance(ident, str) and ident in declared_components}
        - components.keys()
        if isinstance(selected, list) and isinstance(declared_components, dict)
        else set()
    )

    if "system" not in distro:
        diagnostics.append(
            Diagnostic(
                file="ghrah.toml",
                path=f"distro.{distro_name}.system",
                code="E-CAT-SYSTEM",
                message=(
                    "要产出 catalog 的构建必须显式声明 system（host_instance / kernel / providers）"
                ),
            )
        )

    services = enabled_services(data, components, distro_name)

    # 六类（包作者声明自洽）
    diagnostics.extend(_check_action_dup(data, services))
    diagnostics.extend(_check_dependency(data, services, skipped_components))
    selection_has_valid_shape = isinstance(selected, list) and all(
        isinstance(c, str) for c in selected
    )
    all_components_valid = selection_has_valid_shape and all(c in components for c in selected)
    diagnostics.extend(_check_namespace(data, services, distro_name, complete=all_components_valid))
    diagnostics.extend(_check_policy(data, services))
    diagnostics.extend(
        _check_bindings(data, services, identity if isinstance(identity, dict) else None)
    )
    diagnostics.extend(_check_configuration(root, data, components, services))

    # §4.6（部署者指派成立）
    if isinstance(system, dict):
        diagnostics.extend(
            _check_system_identity(
                data,
                services,
                distro_name,
                system,
                identity if isinstance(identity, dict) else None,
                skipped_components,
            )
        )
        diagnostics.extend(
            _check_system_scope(
                data, distro_name, system, identity if isinstance(identity, dict) else None
            )
        )
        if selection_has_valid_shape:
            diagnostics.extend(_check_kernel_entry(data, components, distro_name, system, selected))
        diagnostics.extend(_check_host_entry(data, components, distro_name, system))
    diagnostics.extend(
        _check_callers(identity if isinstance(identity, dict) else None, distro_name)
    )


def collect_diagnostics(
    root: Path, distro: str | None = None
) -> tuple[dict[str, Any] | None, str | None, list[Diagnostic]]:
    """算法第 1–6 步：读清单 → schema 聚合 → 解析发行版 → 语义校验。

    返回 ``(workspace_data, distro_name, diagnostics)``；调用方据 ``diagnostics``
    是否为空决定继续组装还是抛 :class:`CatalogError`。
    """
    data, components, diagnostics = collect_schema_diagnostics(root)
    if data is None:
        return None, None, sorted(diagnostics, key=Diagnostic.sort_key)

    distro_name = resolve_distro(data, distro, diagnostics)
    if distro_name is None:
        return data, None, sorted(diagnostics, key=Diagnostic.sort_key)

    validate_semantics(root, data, components, distro_name, diagnostics)
    return data, distro_name, sorted(diagnostics, key=Diagnostic.sort_key)


# ── 组装（算法第 7 步：按 §5.10 逐行搬运）───────────────────

#: 可选字段：声明了才出现（契约 §5.5 的稀疏规范化）。
_SPARSE_FIELDS = (
    "order",
    "after",
    "requires_ready",
    "restart",
    "max_restarts",
    "window_seconds",
    "drain_ms",
    "close_timeout_ms",
    "close_action",
    "ready",
    "health",
)


def _caller_binding(
    principal: str, resolution_scope: str | None, *, diagnostics: bool
) -> dict[str, Any]:
    return {
        "principal": principal,
        "resolution_scope": resolution_scope,
        "diagnostics": diagnostics,
    }


def _build_host(
    data: dict[str, Any],
    components: dict[str, dict],
    system: dict[str, Any],
    distro_name: str,
) -> dict[str, Any]:
    """§5.10：host 四字段全部来自声明，不猜、不补默认。"""
    instance_name = str(system.get("host_instance", ""))
    instance = data["distro"][distro_name]["instances"][instance_name]
    component_id = str(instance.get("component", ""))
    entry_ref = str(instance.get("host_entry", ""))
    entry_id = entry_ref.split("/", 1)[1] if "/" in entry_ref else entry_ref
    entry = components[component_id]["entries"][entry_id]
    return {
        "instance": instance_name,
        "component": component_id,
        "entry": entry_ref,
        "command": [str(item) for item in entry.get("argv", [])],
    }


def _build_kernel(components: dict[str, dict], system: dict[str, Any]) -> dict[str, Any]:
    """§5.10：kernel.module/entry 来自该组件的 [kernel]，不是工具链常量。"""
    component_id = str(system.get("kernel", ""))
    declaration = components[component_id]["kernel"]
    return {
        "component": component_id,
        "module": str(declaration.get("module", "")),
        "entry": str(declaration.get("entry", "")),
    }


def _build_configuration(root: Path, data: dict[str, Any], service: ServiceView) -> dict[str, Any]:
    """声明里无 configuration 则 ``{}``；有则读出文件内容，占位符原样保留。"""
    relative = service.declaration.get("configuration")
    if not isinstance(relative, str):
        return {}
    source = data.get("components", {}).get(service.component_id, {})
    component_root = root / str(source.get("path", service.component_id))
    return json.loads((component_root / relative).read_text(encoding="utf-8"))


def _build_service(
    root: Path,
    data: dict[str, Any],
    service: ServiceView,
    bindings_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """一条 ``services[]``：派生字段 + 稀疏可选字段 + 两个恒有的容器。"""
    declaration = service.declaration
    entry: dict[str, Any] = {
        "id": service.id,
        "component": service.component_id,
        "module": str(declaration.get("module", "")),
        "provider": str(declaration.get("provider", "")),
        "kind": service.kind,
    }
    for field in _SPARSE_FIELDS:
        if field in declaration:
            entry[field] = declaration[field]

    # schemes 在 catalog 里是数组（声明里是对象）；顺序 = 声明序
    entry["schemes"] = [
        {
            "name": scheme_name,
            "contract": str(scheme.get("contract", "")),
            "actions": [str(action) for action in scheme.get("actions", [])],
        }
        for scheme_name, scheme in service.schemes.items()
    ]
    # bindings 恒为数组（声明的对象名不进入 catalog，只有四项内容）
    entry["bindings"] = [
        {
            "path": str(binding.get("path", "")),
            "scheme": str(binding.get("scheme", "")),
            "scope": str(binding.get("scope", "")),
            "namespace": str(binding.get("namespace", "")),
        }
        for binding in service.bindings.values()
    ]

    record = bindings_by_id[service.id]
    entry["binding"] = _caller_binding(
        str(record.get("principal", "")),
        record.get("resolution_scope"),
        diagnostics=False,  # §5.5：服务不是观察者
    )
    entry["configuration"] = _build_configuration(root, data, service)
    return entry


def _build_callers(identity: dict[str, Any]) -> dict[str, Any]:
    """契约 §3.4：首个 user 主体 + 首个 scope；诊断许可只给 control。"""
    subjects = [s for s in identity.get("subjects", []) if isinstance(s, dict)]
    admin = next(subject for subject in subjects if subject.get("kind") == "user")
    principal = _subject_ref(admin)
    scopes = [s for s in identity.get("scopes", []) if isinstance(s, dict)]
    scope = str(scopes[0]["scope_id"]) if scopes else None
    return {
        "control": _caller_binding(principal, scope, diagnostics=True),
        "observer": _caller_binding(principal, scope, diagnostics=False),
        "peer": _caller_binding(principal, scope, diagnostics=False),
    }


def build_catalog(root: Path, *, distro: str | None = None) -> dict[str, Any]:
    """把工作区清单汇总成代内 catalog（契约 §5.2 的形状）。

    诊断非空即抛 :class:`CatalogError`，**一条不落**；全部无诊断才组装。
    组装完成后用 ``catalog-v1.json`` 自校验，避免产出"看着对但形状不合契约"
    的目录——校验通过才返回。
    """
    data, distro_name, diagnostics = collect_diagnostics(root, distro)
    if diagnostics:
        raise CatalogError(diagnostics)

    assert data is not None and distro_name is not None  # 无诊断时必有二者
    try:
        workspace = load_manifest(root / "ghrah.toml")
    except (ManifestError, OSError, ValueError) as exc:
        raise CatalogError([Diagnostic("ghrah.toml", "", "E-MANIFEST-REF", str(exc))]) from exc
    data = workspace.data
    components = workspace.components
    system = data["distro"][distro_name]["system"]
    identity = data["distro"][distro_name].get("identity", {})
    services = enabled_services(data, components, distro_name)
    bindings_by_id = {
        str(record.get("id")): record
        for record in system.get("providers", [])
        if isinstance(record, dict)
    }

    catalog: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "workspace": {
            "name": str(data["workspace"]["name"]),
            "version": str(data["workspace"]["version"]),
        },
        "distro": distro_name,
        "host": _build_host(data, components, system, distro_name),
        "kernel": _build_kernel(components, system),
        "services": [_build_service(root, data, service, bindings_by_id) for service in services],
        "identity": identity,
        "callers": _build_callers(identity),
    }
    catalog["digest"] = catalog_digest(catalog)

    try:
        _validate(catalog, "catalog", root / "catalog.json")
    except Exception as exc:  # noqa: BLE001 - 生成物不合契约即构建失败
        raise CatalogError(
            [
                Diagnostic(
                    file="catalog.json",
                    path="",
                    code="E-CAT-SHAPE",
                    message=f"生成的 catalog 未通过 catalog-v1.json 校验: {exc}",
                )
            ]
        ) from exc
    return catalog
