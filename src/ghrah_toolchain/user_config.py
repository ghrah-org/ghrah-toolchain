# SPDX-License-Identifier: Apache-2.0
"""CLI 偏好和已安装实例设置；不管理 Agent 业务状态。"""

from __future__ import annotations

import contextlib
import tomllib
from pathlib import Path
from typing import Any

from ghrah_toolchain.errors import ToolchainError
from ghrah_toolchain.layout import DistroToml, ghrah_root, resolve_distro_root
from ghrah_toolchain.runtime_ref import RefSource, acquire_distro_lock
from ghrah_toolchain.toml_io import edit_toml, read_toml

# 用户能写的键是白名单；未知人工字段保留，但不经 config show 回显。
DISTRO_DEFAULTS = {
    "runtime.subject_port": 4112,
    "runtime.webui_port": 5173,
    "distro.ref.source": "latest",
    "distro.ref.gate": "basic",
    "distro.ref.fetch_on_update": False,
    "data.root": "",
    "data.snapshot_keep": 3,
}
SELF_DEFAULTS = {"self.output": "text", "self.log_tail": 100}


def _lookup(data: dict, key: str, default: Any) -> tuple[Any, bool]:
    target = data
    for part in key.split("."):
        if not isinstance(target, dict) or part not in target:
            return default, False
        target = target[part]
    return target, True


def validate_value(key: str, value: Any) -> None:
    default = (DISTRO_DEFAULTS | SELF_DEFAULTS).get(key)
    if default is None:
        raise ToolchainError(f"不支持的配置键: {key}")
    if type(value) is not type(default):
        raise ToolchainError(f"{key} 类型错误，需要 {type(default).__name__}")
    if key in {"runtime.subject_port", "runtime.webui_port"} and not 1 <= value <= 65535:
        raise ToolchainError(f"{key} 必须在 1–65535 之间")
    if key in {"data.snapshot_keep", "self.log_tail"} and not 0 <= value <= 10000:
        raise ToolchainError(f"{key} 必须在 0–10000 之间")
    if key == "distro.ref.source":
        RefSource.parse(value)
    if key == "distro.ref.gate" and value != "basic":
        raise ToolchainError("目前只支持 basic 门禁，full/data 尚未实现")
    if key == "self.output" and value not in {"text", "json"}:
        raise ToolchainError("self.output 只能是 text 或 json")
    if key == "data.root" and value and not Path(value).expanduser().is_absolute():
        raise ToolchainError("data.root 必须是绝对路径或以 ~ 开头；空字符串表示默认数据盘")


def parse_value(key: str, raw: str) -> Any:
    default = (DISTRO_DEFAULTS | SELF_DEFAULTS).get(key)
    if default is None:
        raise ToolchainError(f"不支持的配置键: {key}")
    if isinstance(default, str):
        value = raw
    else:
        try:
            value = tomllib.loads(f"value = {raw}")["value"]
        except (ValueError, KeyError) as exc:
            raise ToolchainError(f"{key} 需要 TOML 整数或布尔值") from exc
    validate_value(key, value)
    return value


def config_view(name: str | None = None) -> dict[str, dict]:
    path = resolve_distro_root(name) / "distro.toml" if name else ghrah_root() / "config.toml"
    if name and not path.is_file():
        raise ToolchainError(f"实例未安装: {name}")
    data = read_toml(path)
    defaults = DISTRO_DEFAULTS if name else SELF_DEFAULTS
    result = {}
    for key, default in defaults.items():
        value, explicit = _lookup(data, key, default)
        # 历史门禁可读以便修正；本轮不把它误报为可执行。
        if not (key == "distro.ref.gate" and value in ("full", "data")):
            validate_value(key, value)
        result[key] = {
            "value": value,
            "source": str(path) if explicit else "default",
            "effective": (
                "next-command"
                if not name
                else "next-install-or-update"
                if key.startswith("distro.ref.")
                else "next-update"
                if key == "data.snapshot_keep"
                else "next-start"
            ),
        }
    return result


def set_config(key: str, raw: str, *, name: str | None = None) -> dict:
    defaults = DISTRO_DEFAULTS if name else SELF_DEFAULTS
    if key not in defaults:
        raise ToolchainError(f"不支持的配置键: {key}")
    value = parse_value(key, raw)
    root = resolve_distro_root(name) if name else ghrah_root()
    path = root / ("distro.toml" if name else "config.toml")
    if name and not path.is_file():
        raise ToolchainError(f"实例未安装: {name}")
    with contextlib.ExitStack() as locks:
        locks.enter_context(acquire_distro_lock(root))
        if name and key == "data.root":
            locks.enter_context(acquire_distro_lock(ghrah_root()))
        if name:
            from ghrah_toolchain.instances import assert_instance_stopped

            assert_instance_stopped(root)
            config = DistroToml.load(root)
            from ghrah_toolchain.runtime_ref import RuntimeRefManager

            RuntimeRefManager()._guard_not_running(config)
            # 防止把已有数据静默切到空盘；数据迁移另设流程。
            if key == "data.root" and value != config.data_root:
                from ghrah_toolchain.layout import DistroLayout, resolve_data_root

                previous = resolve_data_root(DistroLayout(root), config)
                if previous.exists() and any(previous.iterdir()):
                    raise ToolchainError("已有数据，不能直接更改 data.root；请先执行独立的数据迁移")
                target = Path(value).expanduser().resolve() if value else (root / "data").resolve()
                if target.exists() and (not target.is_dir() or any(target.iterdir())):
                    raise ToolchainError("目标数据根非空或不是目录；数据认领／迁移需单独执行")
                if root.resolve().is_relative_to(target):
                    raise ToolchainError("数据根不能是实例根目录或其父目录")
                for directory in ("versions", "generations", "logs", "snapshots"):
                    protected = (root / directory).resolve()
                    if target.is_relative_to(protected) or protected.is_relative_to(target):
                        raise ToolchainError(f"数据根不能与受管目录重叠: {protected}")
                from ghrah_toolchain.layout import list_distros

                for other in list_distros():
                    if other == name:
                        continue
                    other_root = resolve_distro_root(other)
                    other_data = resolve_data_root(
                        DistroLayout(other_root), DistroToml.load(other_root)
                    )
                    for protected in (other_root.resolve(), other_data.resolve()):
                        if target.is_relative_to(protected) or protected.is_relative_to(target):
                            raise ToolchainError(f"数据根与另一实例重叠: {other}")
            ports = {
                "runtime.subject_port": config.subject_port,
                "runtime.webui_port": config.webui_port,
            }
            if key in ports:
                ports[key] = value
                if len(set(ports.values())) != 2:
                    raise ToolchainError("Subject 与 WebUI 端口不能相同")
        edit_toml(path, {key: value}, backup=True)
    return config_view(name)[key]
