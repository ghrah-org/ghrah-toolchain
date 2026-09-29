# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""build.py 机械正确性用例（fixtures 小仓）+ 真实 monorepo integration。

单元族（U1–U9）以临时 git 仓 + hatchling 单包 + pnpm 最小 workspace 验证
archive / 逐包构建 / pin 导出 / manifest 写入 / 失败清理的机械；真实构建
放 @integration（I1/I2）。

环境守卫：git/uv 缺失整文件 skip；pnpm 缺失仅 webui 族用例 skip（对齐
test_integration_sync 的 _require 风格）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import ghrah_bootloader.build as bld
from ghrah_bootloader.build import build_artifacts, normalize_commit, verify_artifacts
from ghrah_bootloader.cli import main
from ghrah_bootloader.errors import BuildError

# ── fixtures 小仓 ────────────────────────────────────────────

_PY_PKG = "pkg-a"
_PY_MODULE = "ghrah_fake"
_WEB_BUILT = "webui-lib"
_WEB_PRIVATE = "observer-web"


def _require(*bins: str) -> None:
    for b in bins:
        if shutil.which(b) is None:
            pytest.skip(f"环境缺少 {b}，跳过构建用例")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _write_py_pkg(root: Path) -> None:
    pkg = root / _PY_PKG
    (pkg / _PY_MODULE).mkdir(parents=True)
    (pkg / _PY_MODULE / "__init__.py").write_text("__version__ = '0.1.0'\n", encoding="utf-8")
    (pkg / _PY_MODULE / "app.py").write_text(
        "def main() -> int:\n    print('fake-server ok')\n    return 0\n", encoding="utf-8"
    )
    (pkg / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n'
        "\n"
        "[project]\n"
        f'name = "{_PY_PKG}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        "\n"
        "[project.scripts]\n"
        'fake-server = "ghrah_fake.app:main"\n'
        "\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{_PY_MODULE}"]\n',
        encoding="utf-8",
    )


def _node_build_script(out_rel: str, content: str) -> str:
    """生成 node build 脚本串（写文件式构建，验证 build 真跑了）。"""
    return f"node -e \"require('fs').writeFileSync('{out_rel}','{content}')\""


def _write_pkg_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_webui(root: Path) -> None:
    # pnpm workspace 根 = 源仓根（对齐真实 monorepo 布局）
    (root / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'ghrah-observer-webui/packages/*'\n", encoding="utf-8"
    )
    _write_pkg_json(
        root / "package.json",
        {"name": "fake-mono-root", "private": True},
    )
    webui = root / "ghrah-observer-webui"
    # 可构建可 pack 的库包（build 脚本产出 dist）
    lib = webui / "packages" / _WEB_BUILT
    (lib / "dist").mkdir(parents=True)
    (lib / "dist" / "index.js").write_text("export {}\n", encoding="utf-8")
    _write_pkg_json(
        lib / "package.json",
        {
            "name": f"@ghrah/{_WEB_BUILT}",
            "version": "0.1.0",
            "scripts": {"build": _node_build_script("dist/index.js", "/*built*/")},
            "files": ["dist"],
        },
    )
    # private 包（构建产物形态拷贝面）
    obs = webui / "packages" / _WEB_PRIVATE
    (obs / "dist").mkdir(parents=True)
    (obs / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (obs / "public").mkdir()
    (obs / "public" / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    (obs / "src").mkdir()
    (obs / "src" / "main.ts").write_text("// src\n", encoding="utf-8")
    _write_pkg_json(
        obs / "package.json",
        {
            "name": f"@ghrah/{_WEB_PRIVATE}",
            "version": "0.1.0",
            "private": True,
            "scripts": {"build": _node_build_script("dist/index.html", "<!--built-->")},
        },
    )


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """小 fixtures 仓：单 Python 包（含 entry）+ 根级 pnpm workspace。"""
    _require("git", "uv")
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _write_py_pkg(repo)
    _write_webui(repo)
    # 根 pyproject：uv workspace（uv export 消费）
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fake-mono-root"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["{_PY_PKG}"]\n'
        "\n"
        "[tool.uv.workspace]\n"
        f'members = ["{_PY_PKG}"]\n'
        "\n"
        "[tool.uv.sources]\n"
        f"{_PY_PKG} = {{ workspace = true }}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["uv", "lock", f"--project={repo}"],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@ghrah.org")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    # 常量重定向：包名/拷贝清单/验证面（fixtures 自有 import 与入口）
    monkeypatch.setattr(bld, "PYTHON_PACKAGES", (_PY_PKG,))
    monkeypatch.setattr(bld, "WEBUI_PACKAGES", (f"@ghrah/{_WEB_BUILT}", f"@ghrah/{_WEB_PRIVATE}"))
    monkeypatch.setattr(bld, "WEBUI_PACK_TARBALLS", (f"@ghrah/{_WEB_BUILT}",))
    monkeypatch.setattr(bld, "OBSERVER_WEB_COPY", ("dist", "public", "package.json"))
    monkeypatch.setattr(bld, "VERIFY_IMPORTS", f"import {_PY_MODULE}")
    monkeypatch.setattr(bld, "VERIFY_ENTRY_POINT", "fake-server")
    return repo


def _build(repo: Path, out: Path) -> bld.BuildResult:
    return build_artifacts(repo, normalize_commit(repo, "HEAD"), out_dir=out)


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


# ── U1–U9 机械用例 ───────────────────────────────────────────


class TestBuildArtifacts:
    def test_u1_artifact_tree_complete(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        result = _build(fake_repo, out)
        assert (out / "manifest.json").is_file()
        assert (out / "BOOTSTRAP.toml").is_file()
        assert (out / "wheels" / "pins.txt").is_file()
        assert list((out / "wheels").glob("*.whl")), "wheel 缺失"
        assert list((out / "tarballs").glob("*.tgz")), "tarball 缺失"
        assert (out / "observer-web" / "package.json").is_file()
        manifest = _manifest(out)
        files = [e["file"] for e in manifest["python"]["wheels"] + manifest["web"]["tarballs"]]
        for f in files:
            assert (out / f).is_file(), f
        assert manifest["schema_version"] == 1
        assert result.source_commit == normalize_commit(fake_repo, "HEAD")
        # 产物树是纯产物：无源码 checkout
        assert not (out / "src").exists()

    def test_u2_commit_pinned(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        result = _build(fake_repo, out)
        (fake_repo / "NEW.txt").write_text("new", encoding="utf-8")
        _git(fake_repo, "add", "-A")
        _git(fake_repo, "commit", "-qm", "new")
        assert not any(out.rglob("NEW.txt"))
        bootstrap = (out / "BOOTSTRAP.toml").read_text(encoding="utf-8")
        assert result.source_commit in bootstrap

    def test_u3_sha256_matches(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)
        for entry in _manifest(out)["python"]["wheels"] + _manifest(out)["web"]["tarballs"]:
            actual = hashlib.sha256((out / entry["file"]).read_bytes()).hexdigest()
            assert actual == entry["sha256"], entry["file"]

    def test_u4_verify_passes(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)
        verify_artifacts(out)  # 完整性 + venv 装配 + import 冒烟 + entry point
        assert (out / "manifest.json").is_file()

    def test_u5_failure_cleans_staging(
        self, fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out"
        monkeypatch.setattr(bld, "PYTHON_PACKAGES", ("no-such-pkg",))
        with pytest.raises(BuildError):
            _build(fake_repo, out)
        assert not out.exists()
        assert not list(tmp_path.glob("out.tmp-*")), "staging 残留"

    def test_u6_nonempty_out_rejected(self, fake_repo: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "junk").write_text("x", encoding="utf-8")
        with pytest.raises(BuildError, match="已存在且非空"):
            _build(fake_repo, out)

    def test_u7_bootstrap_fields(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        result = _build(fake_repo, out)
        text = (out / "BOOTSTRAP.toml").read_text(encoding="utf-8")
        assert f'source_commit = "{result.source_commit}"' in text
        m = re.search(r'built_at = "([^"]+)"', text)
        assert m and ("+00:00" in m.group(1) or m.group(1).endswith("Z"))
        assert re.search(r'builder = "ghrah-bootloader [\d.]+"', text)

    def test_u8_webui_pack_and_copy(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)
        tarballs = sorted(p.name for p in (out / "tarballs").glob("*.tgz"))
        assert len(tarballs) == 1 and _WEB_BUILT in tarballs[0]
        observer = out / "observer-web"
        assert (observer / "dist" / "index.html").is_file()
        assert (observer / "public" / "favicon.ico").is_file()
        names = {p.name for p in observer.iterdir()}
        assert names == {"dist", "public", "package.json"}  # src/ 被剥离
        assert "<!--built-->" in (observer / "dist" / "index.html").read_text(encoding="utf-8")
        # manifest 记 observer-web 目录清单
        files = _manifest(out)["web"]["observer_web"]["files"]
        assert "package.json" in files and any(f.startswith("dist") for f in files)

    def test_u9_cli_ref_and_default_out(
        self, fake_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _require("pnpm")
        rc = main(["build", "--repo", str(fake_repo), "--ref", "latest"])
        assert rc == 0
        sha = normalize_commit(fake_repo, "HEAD")
        expected = fake_repo / "dist" / "bootloader" / sha[:7]
        assert (expected / "manifest.json").is_file()
        assert "产物验证通过" in capsys.readouterr().out
        # 非法 ref → BootloaderError → rc=1、无产物
        rc2 = main(
            [
                "build",
                "--repo",
                str(fake_repo),
                "--ref",
                "commit:deadbeef",
                "--out",
                str(tmp_path / "o2"),
            ]
        )
        assert rc2 == 1
        assert not (tmp_path / "o2").exists()


# ── verify 面 ────────────────────────────────────────────────


class TestVerifyArtifacts:
    def test_verify_detects_tamper(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)
        wheel = next((out / "wheels").glob("*.whl"))
        data = bytearray(wheel.read_bytes())
        data[-1] ^= 0xFF
        (out / "wheels" / wheel.name).write_bytes(bytes(data))
        with pytest.raises(BuildError):
            verify_artifacts(out)

    def test_verify_missing_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(BuildError, match="manifest"):
            verify_artifacts(tmp_path)


# ── integration：真实 monorepo ──────────────────────────────

_MONOREPO_DEFAULT = Path(__file__).resolve().parent.parent.parent / "ghrah-monorepo"


def _monorepo() -> Path:
    repo = Path(os.environ.get("GHRAH_MONOREPO_ROOT", str(_MONOREPO_DEFAULT)))
    if not (repo / "ghrah-core" / "pyproject.toml").is_file():
        pytest.skip(f"monorepo 不可用: {repo}")
    return repo


@pytest.mark.integration
class TestBuildIntegration:
    def test_i1_real_build_and_verify(self, tmp_path: Path) -> None:
        _require("uv", "pnpm")
        repo = _monorepo()
        sha = normalize_commit(repo, "HEAD")
        out = tmp_path / "bootloader-build"
        result = build_artifacts(repo, sha, out_dir=out)
        manifest = _manifest(out)
        wheel_names = {w["name"] for w in manifest["python"]["wheels"]}
        assert wheel_names == {
            "ghrah-core",
            "ghrah-protocol",
            "ghrah-plugin",
            "ghrah-taskstore",
            "ghrah-subject",
        }
        tb_names = {t["name"] for t in manifest["web"]["tarballs"]}
        assert tb_names == {
            "@ghrah/protocol",
            "@ghrah/observer-core",
            "@ghrah/plugin-registry",
        }
        assert (out / "observer-web" / "package.json").is_file()
        assert not (out / "src").exists()
        assert result.source_commit == sha
        verify_artifacts(out)

    def test_i2_cli_build(self, tmp_path: Path) -> None:
        _require("uv", "pnpm")
        repo = _monorepo()
        rc = main(
            [
                "build",
                "--repo",
                str(repo),
                "--ref",
                "latest",
                "--out",
                str(tmp_path / "b"),
            ]
        )
        assert rc == 0
        assert (tmp_path / "b" / "BOOTSTRAP.toml").is_file()


# ── pnpm 调用统一（nt 模拟，纯 mock 命令形态） ────────


class TestW4PnpmCommandForm:
    def test_build_webui_nt_uses_pnpm_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """nt：_build_webui 的 pnpm 族命令全部经 cmd /c pnpm 前缀。"""
        import subprocess as real_subprocess

        import ghrah_bootloader.build as bld
        from ghrah_bootloader import materialize as mat

        class _OsProxy:
            def __init__(self) -> None:
                import os as real_os

                object.__setattr__(self, "_real", real_os)

            def __getattr__(self, item):
                return getattr(object.__getattribute__(self, "_real"), item)

            @property
            def name(self) -> str:
                return "nt"

        monkeypatch.setattr(bld, "os", _OsProxy(), raising=False)
        monkeypatch.setattr(mat, "os", _OsProxy(), raising=False)
        # 最小源树骨架：pnpm-workspace.yaml + webui 根 + 单包（含 package.json）
        src = tmp_path / "src"
        webui = src / "ghrah-observer-webui"
        pkg = webui / "packages" / "a"
        pkg.mkdir(parents=True)
        (src / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")
        (pkg / "package.json").write_text(
            '{"name": "@x/a", "version": "0.0.1", "private": false}\n', encoding="utf-8"
        )
        monkeypatch.setattr(bld, "WEBUI_ROOT_DIR", "ghrah-observer-webui", raising=False)
        monkeypatch.setattr(bld, "OBSERVER_WEB_PACKAGE_DIR", "a", raising=False)
        monkeypatch.setattr(bld, "WEBUI_PACKAGES", ("@x/a",), raising=False)
        monkeypatch.setattr(bld, "WEBUI_PACK_TARBALLS", ("@x/a",), raising=False)
        monkeypatch.setattr(bld, "OBSERVER_WEB_COPY", ("package.json",), raising=False)
        cmds: list[list[str]] = []

        def fake_run(cmd, *, cwd=None, check=True):
            cmds.append([str(c) for c in cmd])
            if "pack" in cmd:
                # pack 会因无产物在后续断言失败——提前透传产物断言路径
                raise bld.BuildError("stop-after-pack")
            return real_subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(bld, "_run", fake_run)
        with pytest.raises(bld.BuildError, match="stop-after-pack"):
            bld._build_webui(src, tmp_path / "tarballs", tmp_path / "staging")
        pnpm_cmds = [c for c in cmds if c and c[0] in ("pnpm", "cmd")]
        assert pnpm_cmds, "应至少发起一次 pnpm 族命令"
        for c in pnpm_cmds:
            assert c[:3] == ["cmd", "/c", "pnpm"], c

    def test_pnpm_command_posix_bare(self) -> None:
        import os as real_os

        from ghrah_bootloader.materialize import pnpm_command

        if real_os.name == "nt":  # 真机 Windows 不强断言（CI 面 posix）
            import pytest

            pytest.skip("posix 形态断言仅非 nt 平台")
        assert pnpm_command() == ["pnpm"]
