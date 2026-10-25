# SPDX-License-Identifier: Apache-2.0
"""工作区清单的离线读取、结构校验与显式引用解析。"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from ghrah_toolchain.errors import ToolchainError

_MAX_BYTES = 1024 * 1024


class ManifestError(ToolchainError):
    """清单格式、路径或引用不合法。"""

    exit_code = 3


def _load(path: Path, *, json_document: bool = False) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise ManifestError(f"{path}: 文件超过 {_MAX_BYTES} 字节")
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if json_document else tomllib.loads(raw)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: 顶层必须是对象")
    return data


def _within(root: Path, value: str, *, directory: bool = False) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ManifestError(f"{root}: 路径越界: {value}")
    if not (path.is_dir() if directory else path.is_file()):
        raise ManifestError(f"{root}: 路径不存在或类型不符: {value}")
    return path


def _validate(data: dict[str, Any], kind: str, path: Path) -> None:
    resource = files("ghrah_toolchain").joinpath("schemas", f"{kind}-v1.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    # 所有引用均位于打包 schema 的 $defs 中，不访问网络。
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        field = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ManifestError(f"{path}:{field}: {error.message}")


def _reference(mapping: dict, key: str, label: str) -> Any:
    if key not in mapping:
        raise ManifestError(f"{label}: 未定义引用 {key!r}")
    return mapping[key]


def _acyclic(graph: dict[str, set[str]], label: str) -> None:
    active: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            raise ManifestError(f"{label}: 依赖成环，涉及 {node}")
        if node in done:
            return
        active.add(node)
        for dep in sorted(graph[node]):
            visit(dep)
        active.remove(node)
        done.add(node)

    for node in sorted(graph):
        visit(node)


@dataclass(frozen=True)
class WorkspaceManifest:
    """已校验的静态组合视图；不代表运行时就绪或授权。"""

    path: Path
    data: dict[str, Any]
    components: dict[str, dict[str, Any]]
    digest: str

    def metadata(self) -> dict[str, Any]:
        """返回与调用方内存独立的机器可读静态视图。"""
        return {
            "manifest_path": str(self.path),
            "digest": self.digest,
            "workspace": copy.deepcopy(self.data["workspace"]),
            "components": copy.deepcopy(self.components),
            "distro": copy.deepcopy(self.data["distro"]),
            "profile": copy.deepcopy(self.data.get("profile", {})),
            "validation": "static",
            "runtime_binding_applied": False,
        }


def discover_manifest(start: Path | None = None, manifest_path: Path | None = None) -> Path:
    """从当前目录向上寻找源码清单；显式路径优先，不回落用户安装域。"""
    if manifest_path is not None:
        path = manifest_path.expanduser().resolve()
        if not path.is_file():
            raise ManifestError(f"清单不存在: {path}")
        return path
    base = (start or Path.cwd()).resolve()
    for root in (base, *base.parents):
        candidate = root / "ghrah.toml"
        if candidate.is_file():
            return candidate
    raise ManifestError(f"{base}: 未找到 ghrah.toml")


def load_manifest(path: Path | None = None, *, start: Path | None = None) -> WorkspaceManifest:
    """校验 schema、组件来源、原生身份和组合引用，不加载任何业务实现。"""
    manifest_path = discover_manifest(start, path)
    root = manifest_path.parent
    data = _load(manifest_path)
    _validate(data, "workspace", manifest_path)
    components: dict[str, dict[str, Any]] = {}
    inputs: dict[str, str] = {}

    def record(file: Path) -> None:
        inputs[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()

    for lock in data["workspace"].get("native_locks", []):
        record(_within(root, lock))
    default = data["workspace"].get("default_distro")
    if default is not None:
        _reference(data["distro"], default, "workspace.default_distro")
    for ident, source in data["components"].items():
        component_root = _within(root, source["path"], directory=True)
        file = _within(component_root, "component.toml")
        component = _load(file)
        _validate(component, "component", file)
        if component["id"] != ident:
            raise ManifestError(f"{file}: id 与 components.{ident} 不一致")
        try:
            Version(component["version"])
        except InvalidVersion as exc:
            raise ManifestError(f"{file}: 无效 version") from exc
        build = component.get("build")
        if build and "native_lock" in build:
            record(_within(root, build["native_lock"]))
        if build:
            for output in build["outputs"]:
                if Path(output).is_absolute() or ".." in Path(output).parts:
                    raise ManifestError(f"{file}: 构建输出路径越界: {output}")
        if "tests" in component and "cwd" in component["tests"]:
            _within(component_root, component["tests"]["cwd"], directory=True)
        if build and build["backend"] in {"uv", "pnpm"}:
            native = _within(
                component_root, "pyproject.toml" if build["backend"] == "uv" else "package.json"
            )
            native_data = _load(native, json_document=build["backend"] == "pnpm")
            native_name = (
                native_data.get("project", {}).get("name")
                if build["backend"] == "uv"
                else native_data.get("name")
            )
            if build["member"] != native_name:
                raise ManifestError(f"{file}: build.member 与原生包身份 {native_name!r} 不一致")
            native_version = (
                native_data.get("project", {}).get("version")
                if build["backend"] == "uv"
                else native_data.get("version")
            )
            if native_version != component["version"]:
                raise ManifestError(f"{file}: version 与原生包版本 {native_version!r} 不一致")
            record(native)
        for export in component.get("exports", {}).values():
            _reference(component.get("entries", {}), export["entry"], f"{file}:exports")
            for key in ("descriptor", "definition"):
                document = _within(component_root, export[key])
                descriptor = _load(document, json_document=True)
                if key == "descriptor" and descriptor.get("id") != export["contract"]:
                    raise ManifestError(f"{document}: 接口 ID 与导出 contract 不一致")
                record(document)
        for entry in component.get("entries", {}).values():
            if entry["kind"] == "process" and not entry.get("argv"):
                raise ManifestError(f"{file}: process 入口必须提供 argv")
            if entry["kind"] == "in-process" and not entry.get("handler"):
                raise ManifestError(f"{file}: in-process 入口必须提供 handler")
        record(file)
        components[ident] = component
    for distro_id, distro in data["distro"].items():
        selected = distro["components"]
        if len(set(selected)) != len(selected):
            raise ManifestError(f"distro.{distro_id}: components 重复")
        for ident in selected:
            _reference(components, ident, distro_id)
        instances = distro["instances"]
        services = distro.get("services", {})
        startup: dict[str, set[str]] = {}
        for ident, instance in instances.items():
            if instance["component"] not in selected:
                raise ManifestError(f"{distro_id}.{ident}: 实例组件不在发行版中")
            component = components[instance["component"]]
            for entry_id in component.get("entries", {}):
                startup[f"{ident}/{entry_id}"] = set()
            if "config" in instance:
                config = _within(root, instance["config"])
                _load(config, json_document=config.suffix == ".json")
                record(config)
            if instance["host"] == "in-process" and "host_entry" not in instance:
                raise ManifestError(f"{distro_id}.{ident}: 缺少 host_entry")
        for ident, instance in instances.items():
            component = components[instance["component"]]
            bindings = instance.get("imports", {})
            imports = component.get("imports", {})
            for slot in bindings:
                _reference(imports, slot, f"{distro_id}.{ident}.imports")
            for slot, requirement in imports.items():
                if slot not in bindings:
                    if requirement.get("required", True):
                        raise ManifestError(f"{distro_id}.{ident}: 未绑定必需接口 {slot}")
                    continue
                service = _reference(services, bindings[slot], f"{distro_id}.{ident}.{slot}")
                if service["contract"] != requirement["contract"]:
                    raise ManifestError(f"{distro_id}.{ident}.{slot}: contract 不匹配")
            for entry_id, entry in component.get("entries", {}).items():
                node = f"{ident}/{entry_id}"
                for dep in entry.get("requires_ready", []):
                    if dep in bindings:
                        service = services[bindings[dep]]
                        provider = _reference(instances, service["instance"], distro_id)
                        export = _reference(
                            components[provider["component"]].get("exports", {}),
                            service["export"],
                            distro_id,
                        )
                        target = f"{service['instance']}/{export['entry']}"
                    else:
                        target = dep if "/" in dep else f"{ident}/{dep}"
                    _reference(startup, target, f"{node}.requires_ready")
                    startup[node].add(target)
            if "host_entry" in instance:
                _reference(startup, instance["host_entry"], f"{distro_id}.{ident}.host_entry")
        for ident, service in services.items():
            provider = _reference(instances, service["instance"], f"{distro_id}.{ident}")
            export = _reference(
                components[provider["component"]].get("exports", {}), service["export"], ident
            )
            if (export["scheme"], export["contract"]) != (service["scheme"], service["contract"]):
                raise ManifestError(f"{distro_id}.{ident}: scheme/contract 不匹配")
        for template in distro.get("namespaces", {}).values():
            for service in template["bindings"].values():
                _reference(services, service, f"{distro_id}.namespace")
        for entry in distro["roots"]:
            _reference(startup, entry, f"{distro_id}.roots")
        _acyclic(startup, f"{distro_id}.requires_ready")
        for name, version in distro.get("runtime", {}).items():
            try:
                SpecifierSet(version if version[0] in "<>=!~" else f"=={version}")
            except (InvalidSpecifier, IndexError) as exc:
                raise ManifestError(f"{distro_id}.runtime.{name}: 版本约束无效") from exc
    graph = {
        ident: set(c.get("build", {}).get("depends_on", [])) for ident, c in components.items()
    }
    for ident, dependencies in graph.items():
        for dep in dependencies:
            _reference(components, dep, f"{ident}.build.depends_on")
    _acyclic(graph, "build")
    canonical = json.dumps(
        {"manifest": data, "components": components, "inputs": inputs},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return WorkspaceManifest(manifest_path, data, components, digest)
