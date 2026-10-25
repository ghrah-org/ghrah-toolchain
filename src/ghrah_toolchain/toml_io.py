# SPDX-License-Identifier: Apache-2.0
"""人维护 TOML 的保留格式编辑与原子替换。调用者负责业务锁。"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit

from ghrah_toolchain.errors import LayoutError


def read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise LayoutError(f"读取配置失败: {path}: {exc}") from exc


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temp.chmod(path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def edit_toml(path: Path, changes: dict[str, Any], *, backup: bool = False) -> None:
    """点分键编辑；未修改字段的语义必须保持，发现外部并发编辑则拒绝写回。"""
    before = path.read_bytes() if path.exists() else None
    try:
        source = before.decode("utf-8") if before is not None else ""
        original = tomllib.loads(source)
        document = tomlkit.parse(source)
        if document.unwrap() != original:
            raise ValueError("编辑库与标准库的解析结果不一致")
        expected = tomllib.loads(source)
        for key, value in changes.items():
            parts = key.split(".")
            target, plain = document, expected
            for part in parts[:-1]:
                if part not in target:
                    target[part] = tomlkit.table()
                target = target[part]
                plain = plain.setdefault(part, {})
            if value is None:
                target.pop(parts[-1], None)
                plain.pop(parts[-1], None)
            else:
                target[parts[-1]] = value
                plain[parts[-1]] = value
        rendered = tomlkit.dumps(document)
        if tomllib.loads(rendered) != expected:
            raise ValueError("写回配置改变了未指定字段的语义")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LayoutError(f"配置无法安全编辑，原文件未改动: {path}: {exc}") from exc
    latest = path.read_bytes() if path.exists() else None
    if before != latest:
        raise LayoutError(f"配置被其他程序修改，请重试: {path}")
    if before is not None and rendered.encode("utf-8") == before:
        return
    if backup and before is not None:
        atomic_write(path.with_suffix(path.suffix + ".bak"), source)
    atomic_write(path, rendered)
