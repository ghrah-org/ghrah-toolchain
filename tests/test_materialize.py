# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""组合代物化测试（gen_id / canonical 指纹 / 物化机械 / 读取与完整性）。

不跑真实 uv/pnpm：子进程层 monkeypatch 拦截并记录命令，副作用以最小骨架
物化（venv 解释器 + node_modules）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ghrah_toolchain.errors import MaterializeError
from ghrah_toolchain.materialize import (
    FINGERPRINT_NAME,
    GENERATION_TOML,
    GenerationSpec,
    PluginRef,
    _install_python_env,
    generation_complete,
    generation_dir,
    generation_fingerprint,
    generation_id,
    materialize_generation,
    pnpm_command,
    read_generation,
)


def _spec(base_ver: str = "2026.09", commit: str | None = None) -> GenerationSpec:
    return GenerationSpec(
        base_version=base_ver,
        base_commit=commit or "a" * 40,
        pins_sha256="b" * 64,
    )


def _materialize_base(tree: Path) -> None:
    """最小基座产物树骨架（manifest 面 + wheels 面）。"""
    wheels = tree / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "ghrah_core-0.1.0-py3-none-any.whl").write_bytes(b"core-wheel")
    (wheels / "pins.txt").write_text("third-party-pins\n", encoding="utf-8")
    tarballs = tree / "tarballs"
    tarballs.mkdir()
    (tarballs / "ghrah-protocol-0.1.0.tgz").write_bytes(b"protocol-tgz")
    observer = tree / "observer-web"
    observer.mkdir()
    (observer / "package.json").write_text('{"name": "@ghrah/observer-web"}\n', encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "python": {"pins_sha256": "b" * 64, "pins_file": "wheels/pins.txt"},
        "web": {
            "tarballs": [
                {
                    "name": "@ghrah/protocol",
                    "version": "0.1.0",
                    "file": "tarballs/ghrah-protocol-0.1.0.tgz",
                    "sha256": "0" * 64,
                }
            ]
        },
    }
    (tree / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def fake_procs(monkeypatch: pytest.MonkeyPatch):
    """拦截 uv/pnpm 子进程：记录命令 + 物化最小 env 骨架；可注入失败。"""
    calls: list[list[str]] = []
    fail_first: dict[str, str | None] = {"match": None}

    def fake_run(cmd, *, cwd=None, env=None):
        argv = [str(c) for c in cmd]
        calls.append(argv)
        if fail_first["match"] is not None and any(fail_first["match"] in part for part in argv):
            raise MaterializeError(f"命令模拟失败: {argv[0]}")
        argv0 = argv[0]
        if argv0 == "uv" and "venv" in argv:
            # uv venv <path>：物化解释器骨架（bin/python 可执行）
            venv = Path(argv[-1])
            py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("#!/bin/sh\n", encoding="utf-8")
            if os.name != "nt":
                py.chmod(0o755)
        elif argv0 == "uv" and "install" in argv:
            pass  # wheels/pins 平铺（骨架无需真实副作用）
        elif argv0 in ("pnpm", "cmd"):
            assert cwd is not None
            (Path(cwd) / "node_modules").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import ghrah_toolchain.materialize as mz

    monkeypatch.setattr(mz, "_run", fake_run)
    return type("FakeProcs", (), {"calls": calls, "fail_first": fail_first})()


# ── M1 gen_id 确定性 ─────────────────────────────────────────


def test_m1_generation_id_deterministic() -> None:
    assert generation_id("2026.09") == "2026.09"  # 无插件 = 基座版本名
    assert generation_id("2026.09", ()) == "2026.09"
    plugins = (PluginRef(name="alpha", version="1.0.0", sha256="0" * 64),)
    beta = PluginRef(name="beta", version="2.0.0", sha256="1" * 64)
    gen_a = generation_id("2026.09", (beta, plugins[0]))  # 顺序不同于下行
    gen_b = generation_id("2026.09", (plugins[0], beta))
    assert gen_a.startswith("2026.09+p") and len(gen_a.split("+p")[1]) == 8
    assert gen_a == gen_b  # 排序后哈希 → 顺序无关
    other = generation_id("2026.09", (PluginRef("alpha", "1.0.1", "0" * 64),))
    assert other != gen_a  # 版本变化 → 不同代


# ── M2 指纹排除 built_at ─────────────────────────────────────


def test_m2_fingerprint_excludes_built_at() -> None:
    spec = _spec()
    fp_1 = generation_fingerprint(spec)
    fp_2 = generation_fingerprint(spec)
    assert fp_1 == fp_2  # 同 spec 恒等
    changed = GenerationSpec(
        base_version="2026.09",
        base_commit="a" * 40,
        pins_sha256="c" * 64,  # pins 变 → 组合变 → 新代
    )
    assert generation_fingerprint(changed) != fp_1


def test_m2b_render_canonical_vs_final() -> None:
    spec = _spec()
    from ghrah_toolchain.materialize import render_generation_toml

    canonical = render_generation_toml(spec, built_at=None)
    final = render_generation_toml(spec, built_at="2026-09-25T00:00:00Z")
    assert canonical in final  # canonical = 最终形态去 built_at 行
    assert "built_at" not in canonical
    assert "built_at" in final


# ── M3 物化全链路（fake 子进程） ─────────────────────────────


def test_m3_materialize_full_chain(tmp_path: Path, fake_procs) -> None:
    distro_root = tmp_path / "generic"
    base_tree = distro_root / "versions" / "2026.09"
    _materialize_base(base_tree)
    spec = _spec()

    result = materialize_generation(distro_root, base_tree, spec)

    gen = distro_root / "generations" / "2026.09"
    assert result.gen_dir == gen
    assert result.gen_id == "2026.09"
    assert result.fingerprint == generation_fingerprint(spec)
    # GENERATION + 指纹落位；staging 无残留
    assert (gen / GENERATION_TOML).is_file()
    assert (gen / FINGERPRINT_NAME).read_text(encoding="utf-8").strip() == result.fingerprint
    assert not list((distro_root / "generations").glob("*.tmp-*"))
    # uv venv → uv pip install（wheels + pins 平铺 + UV_CACHE_DIR 由 env 注入）
    venv_cmd = [c for c in fake_procs.calls if c[0] == "uv" and "venv" in c]
    install_cmd = [c for c in fake_procs.calls if c[0] == "uv" and "install" in c]
    assert venv_cmd and install_cmd
    assert venv_cmd[0][venv_cmd[0].index("--python") + 1] == "3.11"
    flat = install_cmd[0]
    assert any(p.endswith(".whl") for p in flat)
    assert "-r" in flat and any(p.endswith("pins.txt") for p in flat)
    # webui shim：observer-web 直接 file: 依赖；@ghrah/* tarball 经 overrides
    # 重写（workspace:* 协议脱离 monorepo 无法解析——实测）；pnpm store 钉在域内
    shim = json.loads((gen / "env" / "node" / "package.json").read_text(encoding="utf-8"))
    dep = shim["dependencies"]["@ghrah/observer-web"]
    assert dep.startswith("file:")
    assert (gen / "env" / "node" / dep.removeprefix("file:")).resolve().is_dir()
    # overrides 双写：package.json（pnpm <=9）+ pnpm-workspace.yaml（pnpm >=10
    # 权威位置；值单引号标量——双引号反斜杠转义在 Windows 路径上会炸）
    override = shim["pnpm"]["overrides"]["@ghrah/protocol"]
    ws_text = (gen / "env" / "node" / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    ws_override = next(
        line for line in ws_text.splitlines() if line.strip().startswith("'@ghrah/protocol'")
    )
    ws_value = ws_override.split(":", 1)[1].strip().strip("'")
    for value in (override, ws_value):
        assert value.startswith("file:")
        target = (gen / "env" / "node" / value.removeprefix("file:")).resolve()
        assert target.is_file() and target.parent == (base_tree / "tarballs").resolve()
    pnpm_cmds = [c for c in fake_procs.calls if c[0] in ("pnpm", "cmd")]
    assert pnpm_cmds and any(
        "store-dir" in part and "cache" in part for c in pnpm_cmds for part in c
    )
    assert (gen / "env" / "node" / "node_modules").is_dir()


def test_materialization_uses_artifact_python_pin(tmp_path: Path, fake_procs) -> None:
    base = tmp_path / "base"
    _materialize_base(base)
    path = base / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["python"]["interpreter"] = "3.14.8"
    path.write_text(json.dumps(manifest))
    materialize_generation(tmp_path / "demo", base, _spec())
    command = next(cmd for cmd in fake_procs.calls if "venv" in cmd)
    assert command[command.index("--python") + 1] == "3.14.8"


def test_explicit_python_does_not_require_base_manifest(tmp_path: Path, fake_procs) -> None:
    """dev 产物树没有 manifest.json；显式 python 必须优先于清单读取。"""
    base = tmp_path / "artifacts"
    _materialize_base(base)
    (base / "manifest.json").unlink()
    _install_python_env(tmp_path / "demo", base, tmp_path / "venv", python="3.12")
    command = next(cmd for cmd in fake_procs.calls if "venv" in cmd)
    assert command[command.index("--python") + 1] == "3.12"


# ── M3b 复用（零物化）与重建 ─────────────────────────────────


def test_m3b_reuse_and_rebuild(tmp_path: Path, fake_procs) -> None:
    distro_root = tmp_path / "generic"
    base_tree = distro_root / "versions" / "2026.09"
    _materialize_base(base_tree)
    spec = _spec()
    materialize_generation(distro_root, base_tree, spec)
    fp = distro_root / "generations" / "2026.09" / FINGERPRINT_NAME
    mtime = fp.stat().st_mtime_ns

    # 同 spec 复用：零子进程、指纹文件不动
    n_calls = len(fake_procs.calls)
    materialize_generation(distro_root, base_tree, spec)
    assert len(fake_procs.calls) == n_calls
    assert fp.stat().st_mtime_ns == mtime

    # rebuild=True：旧代移除重建（指纹文件重写）
    materialize_generation(distro_root, base_tree, spec, rebuild=True)
    assert len(fake_procs.calls) > n_calls
    assert read_generation(distro_root / "generations" / "2026.09") is not None


# ── M4 物化失败 = 目标不存在 ─────────────────────────────────


def test_m4_failure_leaves_no_target(tmp_path: Path, fake_procs) -> None:
    distro_root = tmp_path / "generic"
    base_tree = distro_root / "versions" / "2026.09"
    _materialize_base(base_tree)
    fake_procs.fail_first["match"] = "install"

    with pytest.raises(MaterializeError, match="命令模拟失败"):
        materialize_generation(distro_root, base_tree, _spec())

    gens = distro_root / "generations"
    assert not (gens / "2026.09").exists()
    assert not list(gens.glob("*.tmp-*")) if gens.is_dir() else True


def test_m4b_pins_mismatch_rejected(tmp_path: Path, fake_procs) -> None:
    """基座 manifest 的 pins_sha256 与代声明不符 → fail-closed。"""
    distro_root = tmp_path / "generic"
    base_tree = distro_root / "versions" / "2026.09"
    _materialize_base(base_tree)
    bad = GenerationSpec(base_version="2026.09", base_commit="a" * 40, pins_sha256="d" * 64)
    with pytest.raises(MaterializeError, match="pins_sha256 与代声明不符"):
        materialize_generation(distro_root, base_tree, bad)
    assert not (distro_root / "generations" / "2026.09").exists()


# ── M5 读取与完整性探针 ──────────────────────────────────────


def test_m5_read_generation_roundtrip(tmp_path: Path, fake_procs) -> None:
    distro_root = tmp_path / "generic"
    base_tree = distro_root / "versions" / "2026.09"
    _materialize_base(base_tree)
    spec = _spec(commit="e" * 40)
    gen = materialize_generation(distro_root, base_tree, spec).gen_dir

    info = read_generation(gen)
    assert info is not None
    assert info.base_version == "2026.09"
    assert info.base_commit == "e" * 40
    assert info.pins_sha256 == "b" * 64
    assert info.plugins == ()
    assert info.fingerprint == generation_fingerprint(spec)
    assert generation_complete(gen) is True

    # 缺 GENERATION.toml → 读不回（完整性探针独立：只看 venv/node_modules）
    (gen / GENERATION_TOML).unlink()
    assert read_generation(gen) is None
    assert generation_complete(gen) is True  # env 面仍在
    shutil.rmtree(gen / "env")
    assert generation_complete(gen) is False


def test_m5b_generation_dir_pure() -> None:
    """generation_dir 纯计算（不建目录）。"""
    d = generation_dir(Path("/tmp/xyz"), _spec())
    assert d == Path("/tmp/xyz/generations/2026.09")
    assert not d.exists()


def test_m5c_pnpm_command_platform() -> None:
    assert pnpm_command() == (["cmd", "/c", "pnpm"] if os.name == "nt" else ["pnpm"])
