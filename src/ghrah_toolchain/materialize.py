# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""组合代物化：基座产物树 + （空）插件集 → generations/<gen-id>/（per-gen env）。

物化语义：

```
generations/<gen-id>/
  GENERATION.toml        # 基座 ver+commit、插件集（当前恒空）、pins_sha256、built_at
  .deps-fingerprint      # GENERATION.toml canonical 内容哈希（排除 built_at）
  env/.venv/             # uv venv + uv pip install 基座 wheels + pins 平铺（零解析）
  env/node/              # webui shim（file: 消费基座 tarballs + observer-web）+ pnpm install
```

- ``gen_id`` 确定性：无插件 = ``<base-ver>``；有插件 = ``<base-ver>+p<hash8>``
  （插件 pin 集 sha256 前 8 位）。同（基座 × 插件集）→ 同 id → 复用。
- 物化走 staging + ``os.replace``（要么完整要么不存在）；代目录落位即不可变。
- 缓存钉在域内：``UV_CACHE_DIR=<distro>/cache/uv``、pnpm ``--store-dir``
  （内容寻址缓存硬链接摊薄各代体量）。

纯 stdlib + ``uv``/``pnpm`` 子进程。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ghrah_toolchain.errors import MaterializeError

logger = logging.getLogger(__name__)

GENERATION_SCHEMA_VERSION = 1
FINGERPRINT_NAME = ".deps-fingerprint"
GENERATION_TOML = "GENERATION.toml"

_CACHE_SUBDIR_UV = "cache/uv"
_CACHE_SUBDIR_PNPM = "cache/pnpm-store"

# 代内 Python 钉死为「运行时下限版本」，而不是 toolchain 自身解释器。
# 运行时闭包里的 C 扩展（httptools 等）在最新 CPython 上常无预编译 wheel，
# 会退化成源码编译（Windows 需 MSVC Build Tools——实测 3.15 直接失败）；
# 下限版本 wheel 覆盖最好，且与 monorepo `requires-python = ">=3.11"` 对齐。
# 调高此值前先确认 pins.txt 里每个 C 扩展都有该版本的 wheel。
RUNTIME_PYTHON = "3.11"


def pnpm_command() -> list[str]:
    """跨平台 pnpm 调用形态（Windows 的 .cmd 无法被 create_process 直接 exec）。"""
    return ["cmd", "/c", "pnpm"] if os.name == "nt" else ["pnpm"]


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        # 子进程（pnpm/uv）输出 UTF-8；中文 Windows 缺省 GBK 解码会崩读线程
        # （stderr 变 None → 报错信息丢失），显式钉 UTF-8 + 容错替换。
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if cp.returncode != 0:
        raise MaterializeError(
            f"命令失败({cp.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"{cp.stderr.strip()[-2000:] or cp.stdout.strip()[-2000:]}"
        )
    return cp


# ── 代身份 ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginRef:
    """可用面插件 pin（当前恒不出现于物化链路）。"""

    name: str
    version: str
    sha256: str


@dataclass(frozen=True)
class GenerationSpec:
    """代的确定性身份输入（gen_id 与 canonical 内容都由它唯一决定）。"""

    base_version: str
    base_commit: str
    pins_sha256: str
    plugins: tuple[PluginRef, ...] = field(default=())

    def gen_id(self) -> str:
        return generation_id(self.base_version, self.plugins)

    def canonical_content(self) -> str:
        return render_generation_toml(self, built_at=None)


def generation_id(base_ver: str, plugins: tuple[PluginRef, ...] = ()) -> str:
    """确定性代名：无插件 = 基座版本名；有插件 = ``<base-ver>+p<hash8>``。"""
    if not plugins:
        return base_ver
    digest = hashlib.sha256()
    for plugin in sorted(plugins, key=lambda p: p.name):
        digest.update(plugin.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(plugin.version.encode("utf-8"))
        digest.update(b"\0")
        digest.update(plugin.sha256.encode("utf-8"))
        digest.update(b"\0")
    return f"{base_ver}+p{digest.hexdigest()[:8]}"


def generation_dir(distro_root: Path, spec: GenerationSpec) -> Path:
    """代的落位路径（纯计算，不触碰磁盘）。"""
    return Path(distro_root) / "generations" / spec.gen_id()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_generation_toml(spec: GenerationSpec, *, built_at: str | None) -> str:
    """GENERATION.toml 序列化；``built_at=None`` 输出 canonical 形态（指纹输入）。

    canonical 形态与最终形态逐字节一致，仅缺 ``built_at`` 行——排除物化时刻
    噪声，保证同组合指纹恒等。
    """
    lines = [
        "# ghrah-bootloader 组合代清单；由 materialize_generation 维护",
        "[generation]",
        f"schema_version = {GENERATION_SCHEMA_VERSION}",
        f'base_version = "{_escape(spec.base_version)}"',
        f'base_commit = "{_escape(spec.base_commit)}"',
        f'pins_sha256 = "{_escape(spec.pins_sha256)}"',
    ]
    if built_at is not None:
        lines.append(f'built_at = "{_escape(built_at)}"')
    for plugin in sorted(spec.plugins, key=lambda p: p.name):
        lines += [
            "",
            "[[plugins]]",
            f'name = "{_escape(plugin.name)}"',
            f'version = "{_escape(plugin.version)}"',
            f'sha256 = "{_escape(plugin.sha256)}"',
        ]
    return "\n".join(lines) + "\n"


def generation_fingerprint(spec: GenerationSpec) -> str:
    """指纹 = canonical GENERATION.toml 内容哈希（排除 built_at）。"""
    return hashlib.sha256(spec.canonical_content().encode("utf-8")).hexdigest()


# ── 代读取与完整性 ───────────────────────────────────────────


@dataclass(frozen=True)
class GenerationInfo:
    """从代目录读回的身份与指纹面。"""

    gen_dir: Path
    base_version: str
    base_commit: str
    pins_sha256: str
    plugins: tuple[PluginRef, ...]
    fingerprint: str | None


def read_generation(gen_dir: Path) -> GenerationInfo | None:
    """读代目录的 GENERATION.toml 与指纹文件；缺失/损坏 → ``None``。"""
    gen_dir = Path(gen_dir)
    toml_path = gen_dir / GENERATION_TOML
    if not toml_path.is_file():
        return None
    try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError, OSError:
        return None
    section = data.get("generation", {})
    plugins = tuple(
        PluginRef(
            name=str(p.get("name", "")),
            version=str(p.get("version", "")),
            sha256=str(p.get("sha256", "")),
        )
        for p in data.get("plugins", [])
    )
    fp_path = gen_dir / FINGERPRINT_NAME
    fingerprint = None
    if fp_path.is_file():
        try:
            fingerprint = fp_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            fingerprint = None
    return GenerationInfo(
        gen_dir=gen_dir,
        base_version=str(section.get("base_version", "")),
        base_commit=str(section.get("base_commit", "")),
        pins_sha256=str(section.get("pins_sha256", "")),
        plugins=plugins,
        fingerprint=fingerprint,
    )


def generation_complete(gen_dir: Path) -> bool:
    """完整性探针：venv 解释器与 webui node_modules 均在位。"""
    gen_dir = Path(gen_dir)
    py = _venv_python(gen_dir / "env" / ".venv")
    if not py.is_file():
        return False
    return (gen_dir / "env" / "node" / "node_modules").is_dir()


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_entry(venv: Path, name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv / sub / f"{name}{suffix}"


# ── 物化 ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class MaterializeResult:
    """物化产物摘要（测试断言面）。"""

    gen_dir: Path
    gen_id: str
    fingerprint: str


def materialize_generation(
    distro_root: Path,
    base_tree: Path,
    spec: GenerationSpec,
    *,
    rebuild: bool = False,
) -> MaterializeResult:
    """把基座产物树装配成组合代（staging 隔离 + ``os.replace`` 原子落位）。

    - 同代已存在且完整且指纹一致 → 直接复用（零物化）；
    - ``rebuild=True`` 或既有代不完整/指纹不符 → 重建（旧代先移除）；
    - 失败 = staging 清理，目标要么完整要么不存在。
    """
    distro_root = Path(distro_root)
    base_tree = Path(base_tree)
    gen_dir = generation_dir(distro_root, spec)
    fingerprint = generation_fingerprint(spec)
    if not rebuild:
        existing = read_generation(gen_dir)
        if (
            existing is not None
            and existing.fingerprint == fingerprint
            and generation_complete(gen_dir)
        ):
            logger.info("组合代复用（零物化）: %s", gen_dir)
            return MaterializeResult(gen_dir=gen_dir, gen_id=spec.gen_id(), fingerprint=fingerprint)
    if gen_dir.exists():
        shutil.rmtree(gen_dir)  # 重建路径：旧代整体移除后经 staging 重建
    staging = gen_dir.parent / f"{gen_dir.name}.tmp-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _materialize_into(distro_root, base_tree, spec, staging, fingerprint)
        gen_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, gen_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return MaterializeResult(gen_dir=gen_dir, gen_id=spec.gen_id(), fingerprint=fingerprint)


def _materialize_into(
    distro_root: Path,
    base_tree: Path,
    spec: GenerationSpec,
    staging: Path,
    fingerprint: str,
) -> None:
    manifest = _read_base_manifest(base_tree)
    pins_sha256 = manifest["python"]["pins_sha256"]
    if pins_sha256 != spec.pins_sha256:
        expected = spec.pins_sha256[:12]
        raise MaterializeError(
            f"基座 pins_sha256 与代声明不符: {base_tree}（{pins_sha256[:12]}… != {expected}…）"
        )
    _install_python_env(distro_root, base_tree, staging / "env" / ".venv")
    _install_webui_env(distro_root, base_tree, manifest, staging / "env" / "node")
    built_at = datetime.now(UTC).isoformat(timespec="seconds")
    (staging / GENERATION_TOML).write_text(
        render_generation_toml(spec, built_at=built_at), encoding="utf-8"
    )
    (staging / FINGERPRINT_NAME).write_text(fingerprint + "\n", encoding="utf-8")


def _read_base_manifest(base_tree: Path) -> dict:
    manifest_path = base_tree / "manifest.json"
    if not manifest_path.is_file():
        raise MaterializeError(f"基座产物树缺少 manifest.json: {base_tree}")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise MaterializeError(f"基座 manifest.json 不可读: {manifest_path}: {e}") from e


def _uv_env(distro_root: Path) -> dict[str, str]:
    return {**os.environ, "UV_CACHE_DIR": str(distro_root / _CACHE_SUBDIR_UV)}


def runtime_python(manifest: dict) -> str:
    """运行时解释器来自构建产物；旧版 ghrah 产物沿用 ``RUNTIME_PYTHON`` 基线。"""
    request = manifest.get("python", {}).get("interpreter", RUNTIME_PYTHON)
    if not isinstance(request, str) or re.fullmatch(r"3\.\d+(?:\.\d+)?", request) is None:
        raise MaterializeError("产物 python.interpreter 必须是明确的 Python 3 数字版本")
    return request


def _install_python_env(
    distro_root: Path, base_tree: Path, venv: Path, *, python: str | None = None
) -> None:
    """``uv venv`` + ``uv pip install`` 基座 wheels + pins 平铺（安装期零解析）。

    ``--relocatable``：代目录经 staging + ``os.replace`` 改名落位——绝对路径
    shebang 会钉死 staging 名（改名后入口全部失效）；relocatable 形态的入口
    脚本为自定位 wrapper（``#!/bin/sh`` + 相对解析），改名后照常执行。

    ``python`` 显式指定解释器时优先使用；dev 的产物树没有 manifest.json，
    由调用者直接传入 ``RUNTIME_PYTHON``。
    """
    wheels = sorted((base_tree / "wheels").glob("*.whl"))
    if not wheels:
        raise MaterializeError(f"基座产物树无 wheels: {base_tree}")
    pins = base_tree / "wheels" / "pins.txt"
    if not pins.is_file():
        raise MaterializeError(f"基座产物树无 pins.txt: {base_tree}")
    request = python or runtime_python(_read_base_manifest(base_tree))
    _run(
        ["uv", "venv", "--relocatable", "--python", request, str(venv)],
        env=_uv_env(distro_root),
    )
    py = _venv_python(venv)
    cmd = ["uv", "pip", "install", f"--python={py}"]
    cmd += [str(w) for w in wheels]
    cmd += ["-r", str(pins)]
    _run(cmd, env=_uv_env(distro_root))


def _install_webui_env(distro_root: Path, base_tree: Path, manifest: dict, node_dir: Path) -> None:
    """webui shim：``file:`` 消费基座 tarballs + observer-web → pnpm install。

    observer-web 的依赖声明是 ``workspace:*`` 协议——脱离 monorepo workspace
    经 ``file:`` 消费时 pnpm 无法解析（实测 ERR_PNPM_WORKSPACE_PKG_NOT_FOUND）。
    故 shim 只声明对 observer-web 的直接 ``file:`` 依赖，其余 @ghrah/* 经
    ``pnpm-workspace.yaml`` 的 overrides 重写到基座 tarball（相对路径以 shim 目录解析；tarball
    文件名从基座 manifest 权威读取——文件名解析不可靠）。第三方
    依赖由 pnpm 现场解析并以生成的 pnpm-lock.yaml 固化在代内（同代复用零
    解析；漂移窗口仅换代时）。
    """
    node_dir.mkdir(parents=True)
    observer = base_tree / "observer-web"
    if not (observer / "package.json").is_file():
        raise MaterializeError(f"基座 observer-web 缺失: {observer}")
    overrides: dict[str, str] = {}
    for entry in manifest["web"]["tarballs"]:
        tarball = base_tree / entry["file"]
        if not tarball.is_file():
            raise MaterializeError(f"基座 tarball 缺失: {tarball}")
        rel = os.path.relpath(tarball, node_dir)
        overrides[str(entry["name"])] = f"file:{rel}"
    rel_observer = os.path.relpath(observer, node_dir)
    shim = {
        "name": "ghrah-generation-webui",
        "version": "0.0.0",
        "private": True,
        "dependencies": {"@ghrah/observer-web": f"file:{rel_observer}"},
        "pnpm": {"overrides": dict(sorted(overrides.items()))},
    }
    (node_dir / "package.json").write_text(
        json.dumps(shim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # pnpm >=10：overrides 权威位置已迁至 pnpm-workspace.yaml（package.json 的
    # pnpm 字段被忽略并告警）；<=9 仍读 package.json——双写覆盖全版本。
    # 值用单引号标量：YAML 双引号处理反斜杠转义，Windows 相对路径会炸。
    ws_lines = ["packages: []", "overrides:"]
    for name in sorted(overrides):
        safe_name = name.replace("'", "''")
        safe_value = overrides[name].replace("'", "''")
        ws_lines.append(f"  '{safe_name}': '{safe_value}'")
    (node_dir / "pnpm-workspace.yaml").write_text("\n".join(ws_lines) + "\n", encoding="utf-8")
    cmd = pnpm_command() + [
        f"--store-dir={distro_root / _CACHE_SUBDIR_PNPM}",
        "install",
    ]
    _run(cmd, cwd=node_dir)
    if not (node_dir / "node_modules").is_dir():
        raise MaterializeError(f"pnpm install 未物化 node_modules: {node_dir}")
