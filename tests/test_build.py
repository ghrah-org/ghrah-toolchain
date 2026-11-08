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

import ghrah_toolchain.build as bld
from ghrah_toolchain.build import build_artifacts, normalize_commit, verify_artifacts
from ghrah_toolchain.catalog import catalog_digest
from ghrah_toolchain.cli import main
from ghrah_toolchain.errors import BuildError
from ghrah_toolchain.manifest import ManifestError

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


def _write_system_declarations(repo: Path) -> None:
    """给 fixtures 仓补上 system/identity 与 [kernel]/[providers]/[entries]。

    声明全部指向 fixtures 自己的包与入口（``pkg-a`` / ``fake-server``），
    这样 catalog 组装走的是真实代码路径，而入口名仍能在临时 venv 里找到。
    """
    component = repo / _PY_PKG / "component.toml"
    component.write_text(
        'schema = 1\nid = "pkg-a"\nversion = "0.1.0"\n\n'
        '[kernel]\nmodule = "ghrah_fake"\nentry = "app"\n\n'
        '[entries.host]\nkind = "process"\nargv = ["fake-server"]\n\n'
        '[providers.helper]\nmodule = "ghrah_fake.app"\nprovider = "helper"\n'
        'kind = "namespace"\norder = 0\nrestart = "never"\n\n'
        '[providers.helper.schemes.core]\ncontract = "ghrah.example/v0"\nactions = ["ping"]\n',
        encoding="utf-8",
    )
    (repo / "ghrah.toml").write_text(
        "schema = 1\n\n"
        '[workspace]\nname = "fake"\nversion = "0.1.0"\ndefault_distro = "demo"\n'
        "native_locks = []\n\n"
        '[components.pkg-a]\npath = "pkg-a"\n\n'
        '[distro.demo]\ncomponents = ["pkg-a"]\nroots = ["subject/host"]\n\n'
        '[distro.demo.instances.subject]\ncomponent = "pkg-a"\nhost = "process"\n'
        'host_entry = "subject/host"\n\n'
        '[distro.demo.system]\nhost_instance = "subject"\nkernel = "pkg-a"\n\n'
        '[[distro.demo.system.providers]]\nid = "pkg-a:helper"\n'
        'principal = \'["local","svc"]\'\n\n'
        '[distro.demo.identity]\ngrant_lifetime = { mode = "instance" }\n'
        "subjects = [\n"
        '    { issuer = "local", subject_id = "admin", kind = "user" },\n'
        '    { issuer = "local", subject_id = "svc", kind = "service" },\n'
        "]\ngroups = []\n"
        'namespaces = [{ namespace_id = "main", owner = \'["local","admin"]\' }]\n'
        'scopes = [{ scope_id = "main", namespaces = ["main"], '
        'subjects = [\'["local","admin"]\', \'["local","svc"]\'] }]\n'
        "access = []\n",
        encoding="utf-8",
    )


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """小 fixtures 仓：单 Python 包（含 entry）+ 根级 pnpm workspace。"""
    _require("git", "uv")
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _write_py_pkg(repo)
    _write_webui(repo)
    _write_system_declarations(repo)
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
    monkeypatch.setattr(bld, "LEGACY_ENTRY_POINT", "fake-server")
    return repo


def _build(repo: Path, out: Path) -> bld.BuildResult:
    return build_artifacts(repo, normalize_commit(repo, "HEAD"), out_dir=out)


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


# ── U1–U9 机械用例 ───────────────────────────────────────────


class TestBuildArtifacts:
    def test_runtime_python_pin_is_recorded(self, fake_repo: Path, tmp_path: Path) -> None:
        _require("pnpm")
        (fake_repo / ".python-version").write_text("3.14.8\n")
        _git(fake_repo, "add", ".python-version")
        _git(fake_repo, "commit", "-qm", "runtime python")
        out = tmp_path / "out"
        _build(fake_repo, out)
        assert _manifest(out)["python"]["interpreter"] == "3.14.8"

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

    @pytest.mark.parametrize("relative_repo", [False, True])
    def test_relative_paths_do_not_double_join(
        self,
        fake_repo: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        relative_repo: bool,
    ) -> None:
        """相对 out_dir/--repo 不得让 staging/src 的相对路径再叠一层。

        ``_build_webui`` 以 ``cwd=src`` 调 ``pnpm --dir=<pkg_dir>``；pkg_dir 若
        相对，会以 src 为基准再拼一次变成 ``src/src/...``。既有用例一律用
        ``tmp_path`` 绝对路径，照不出这条，故单列。
        """
        _require("pnpm")
        monkeypatch.chdir(tmp_path)
        repo = Path(fake_repo.name) if relative_repo else fake_repo
        out = Path("out-rel")
        result = _build(repo, out)
        assert (tmp_path / "out-rel" / "manifest.json").is_file()
        assert result.out_dir.is_absolute()
        assert (tmp_path / "out-rel" / "catalog.json").is_file()

    def test_u5_failure_cleans_staging(
        self, fake_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out"
        monkeypatch.setattr(bld, "PYTHON_PACKAGES", ("no-such-pkg",))
        with pytest.raises(BuildError):
            _build(fake_repo, out)
        assert not out.exists()
        assert not list(tmp_path.glob("out.tmp-*")), "staging 残留"

    @pytest.mark.parametrize("entrypoint", ["api", "cli"])
    def test_legacy_manifest_builds_without_catalog(
        self, fake_repo: Path, tmp_path: Path, entrypoint: str
    ) -> None:
        """旧清单可沿用默认构建，复验仍能找到旧入口。"""
        _require("pnpm")
        manifest = fake_repo / "ghrah.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").split("[distro.demo.system]", 1)[0],
            encoding="utf-8",
        )
        _git(fake_repo, "add", "-A")
        _git(fake_repo, "commit", "-qm", "legacy declarations")
        out = tmp_path / "out"
        if entrypoint == "api":
            result = build_artifacts(fake_repo, "HEAD", out_dir=out)
            assert result.catalog is None
            assert result.distro is None
            verify_artifacts(out)
        else:
            assert main(["build", "--repo", str(fake_repo), "--out", str(out)]) == 0
        assert not (out / "catalog.json").exists()
        assert "catalog" not in _manifest(out)

    def test_declared_system_produces_catalog(self, fake_repo: Path, tmp_path: Path) -> None:
        """清单声明 system/identity 后，代内目录必须产出并进入清单与验收。"""
        _require("pnpm")
        out = tmp_path / "out"
        result = _build(fake_repo, out)

        assert result.catalog is not None
        assert result.distro == "demo"
        catalog_path = out / "catalog.json"
        assert catalog_path.is_file()
        manifest = _manifest(out)
        assert manifest["catalog"]["file"] == "catalog.json"
        assert (
            manifest["catalog"]["sha256"] == hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        )

        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        # 入口从声明推导，不是工具链常量
        assert catalog["host"]["command"] == ["fake-server"]
        assert catalog["distro"] == "demo"
        verify_artifacts(out)

    def test_tampered_catalog_fails_verify(self, fake_repo: Path, tmp_path: Path) -> None:
        """篡改 catalog 后验收必须失败，不得静默通过。"""
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)

        catalog_path = out / "catalog.json"
        body = json.loads(catalog_path.read_text(encoding="utf-8"))
        body["distro"] = "tampered"
        catalog_path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with pytest.raises(BuildError):
            verify_artifacts(out)

    def test_missing_digest_match_fails_verify(self, fake_repo: Path, tmp_path: Path) -> None:
        """摘要不符同样拦下：校验方重算，不只信文件里的值。"""
        _require("pnpm")
        out = tmp_path / "out"
        _build(fake_repo, out)

        catalog_path = out / "catalog.json"
        body = json.loads(catalog_path.read_text(encoding="utf-8"))
        body["digest"] = "sha256:" + "0" * 64
        catalog_path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest = _manifest(out)
        manifest["catalog"]["sha256"] = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BuildError, match="digest 与重算值不符"):
            verify_artifacts(out)

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
        assert re.search(r'builder = "ghrah-toolchain [\d.]+"', text)

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
        expected = fake_repo / "dist" / "toolchain" / sha[:7]
        assert (expected / "manifest.json").is_file()
        assert "构建结果检查通过" in capsys.readouterr().out
        # 非法 ref → ToolchainError → rc=1、无产物
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
    @pytest.mark.parametrize("entry", [None, [], "catalog.json", {}, {"file": "../catalog.json"}])
    def test_malformed_catalog_entry_is_rejected(self, tmp_path: Path, entry: object) -> None:
        pins = tmp_path / "pins.txt"
        pins.write_bytes(b"")
        manifest = {
            "python": {"wheels": [], "pins_file": pins.name, "pins_sha256": bld._sha256_file(pins)},
            "web": {"tarballs": []},
            "catalog": entry,
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(BuildError, match="catalog 条目"):
            verify_artifacts(tmp_path)

    def test_existing_catalog_requires_a_manifest_entry(self, tmp_path: Path) -> None:
        (tmp_path / "catalog.json").write_text("{}", encoding="utf-8")
        with pytest.raises(BuildError, match="缺少现有 catalog.json 的条目"):
            bld._verify_catalog(tmp_path, {})

    def test_verify_rejects_invalid_identity_with_valid_hashes(self, tmp_path: Path) -> None:
        golden = Path(__file__).with_name("catalog_golden.json")
        catalog = json.loads(golden.read_text(encoding="utf-8"))
        del catalog["identity"]["access"]
        catalog["digest"] = catalog_digest(catalog)
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps(catalog), encoding="utf-8")
        manifest = {"catalog": {"file": path.name, "sha256": bld._sha256_file(path)}}
        with pytest.raises(ManifestError, match=r"catalog\.json:identity:.*'access'") as excinfo:
            bld._verify_catalog(tmp_path, manifest)
        assert excinfo.value.exit_code == 3

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
            "ghrah-kernel",
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


# ── CLI 面（fixtures 仓，不依赖真实 monorepo）───────────────


class TestBuildCli:
    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("unknown-distro", "E-CAT-DISTRO"),
            ("no-default", "E-CAT-DISTRO"),
            ("legacy-no-default", "E-CAT-DISTRO"),
            ("unparsable", "E-CAT-PARSE"),
            ("explicit-legacy", "E-CAT-SYSTEM"),
            ("partial-identity", "E-CAT-SYSTEM"),
            ("bad-system", "E-CAT-SCHEMA"),
        ],
    )
    def test_cli_rejects_invalid_catalog_inputs_before_packaging(
        self,
        fake_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        case: str,
        expected: str,
    ) -> None:
        manifest = fake_repo / "ghrah.toml"
        original = manifest.read_text(encoding="utf-8")
        legacy = original.split("[distro.demo.system]", 1)[0]
        body = {
            "unknown-distro": original,
            "no-default": original.replace('default_distro = "demo"\n', ""),
            "legacy-no-default": legacy.replace('default_distro = "demo"\n', ""),
            "unparsable": "schema = ",
            "explicit-legacy": legacy,
            "partial-identity": legacy + original[original.index("[distro.demo.identity]") :],
            "bad-system": legacy.replace(
                'roots = ["subject/host"]', 'roots = ["subject/host"]\nsystem = 7'
            ),
        }[case]
        if body != original:
            manifest.write_text(body, encoding="utf-8")
            _git(fake_repo, "add", "-A")
            _git(fake_repo, "commit", "-qm", case)
        monkeypatch.setattr(bld, "_build_wheels", lambda *_: pytest.fail("unexpected packaging"))
        out = tmp_path / "out"
        args = ["build", "--repo", str(fake_repo), "--out", str(out), "--no-verify"]
        if case == "unknown-distro":
            args.extend(["--distro", "typo"])
        elif case == "explicit-legacy":
            args.extend(["--distro", "demo"])
        assert main(args) == 3
        assert expected in capsys.readouterr().err
        assert not out.exists()
        assert not list(tmp_path.glob("out.tmp-*"))

    def test_cli_build_accepts_distro(
        self, fake_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`build --distro` 是发行版名（区别于 dev 的实例名）。"""
        _require("pnpm")
        out = tmp_path / "b"
        rc = main(["build", "--repo", str(fake_repo), "--out", str(out), "--distro", "demo"])
        assert rc == 0
        assert "代内目录: catalog.json（发行版 demo）" in capsys.readouterr().out

    def test_cli_build_reports_all_diagnostics(
        self, fake_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """清单错误走 exit 3，且逐行打印全部诊断（不折叠成一行）。"""
        # 造三处互相独立的错误：双管理员、namespace 重启策略、未知依赖目标
        manifest = fake_repo / "ghrah.toml"
        body = manifest.read_text(encoding="utf-8")
        body = body.replace(
            '{ issuer = "local", subject_id = "svc", kind = "service" },',
            '{ issuer = "local", subject_id = "svc", kind = "service" },\n'
            '    { issuer = "local", subject_id = "admin2", kind = "user" },',
        )
        manifest.write_text(body, encoding="utf-8")
        component = fake_repo / _PY_PKG / "component.toml"
        component.write_text(
            component.read_text(encoding="utf-8").replace(
                'restart = "never"', 'restart = "on_failure"\nafter = ["pkg-a:absent"]'
            ),
            encoding="utf-8",
        )
        _git(fake_repo, "add", "-A")
        _git(fake_repo, "commit", "-qm", "broken declarations")

        rc = main(["build", "--repo", str(fake_repo), "--out", str(tmp_path / "b")])
        captured = capsys.readouterr()
        assert rc == 3
        lines = [line for line in (captured.err + captured.out).splitlines() if "E-" in line]
        assert len(lines) >= 3, f"应一次报全部诊断，实际: {lines}"
        # 诊断格式是 ``<文件>:[<字段>] <错误码>: <说明>``，错误码后紧跟冒号
        codes = {
            token.rstrip(":") for line in lines for token in line.split() if token.startswith("E-")
        }
        assert {"E-SYS-CALLERS", "E-PROV-POLICY", "E-PROV-DEPENDENCY"} <= codes


# ── pnpm 调用统一（nt 模拟，纯 mock 命令形态） ────────


class TestW4PnpmCommandForm:
    def test_build_webui_nt_uses_pnpm_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """nt：_build_webui 的 pnpm 族命令全部经 cmd /c pnpm 前缀。"""
        import subprocess as real_subprocess

        import ghrah_toolchain.build as bld
        from ghrah_toolchain import materialize as mat

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

        from ghrah_toolchain.materialize import pnpm_command

        if real_os.name == "nt":  # 真机 Windows 不强断言（CI 面 posix）
            import pytest

            pytest.skip("posix 形态断言仅非 nt 平台")
        assert pnpm_command() == ["pnpm"]


# ── webui 包清单（回归：干净检出下 observer-web 类型检查依赖 mock-server） ──


class TestWebuiPackageLists:
    def test_mock_server_built_before_observer_web_but_not_packed(self) -> None:
        """observer-web 的 vue-tsc 覆盖 spec，spec import ``@ghrah/mock-server``。

        干净检出（git archive）里 mock-server 没有 dist/，因此构建序必须含它、
        且先于 observer-web；它又是 private，不进运行时 tarball 面。
        """
        assert "@ghrah/mock-server" in bld.WEBUI_PACKAGES
        assert bld.WEBUI_PACKAGES.index("@ghrah/mock-server") < bld.WEBUI_PACKAGES.index(
            "@ghrah/observer-web"
        )
        assert "@ghrah/mock-server" not in bld.WEBUI_PACK_TARBALLS
        assert bld.WEBUI_PACK_TARBALLS == (
            "@ghrah/protocol",
            "@ghrah/plugin-registry",
            "@ghrah/observer-core",
        )


def test_run_reports_stdout_diagnostics_alongside_stderr(tmp_path: Path) -> None:
    """脚本回显在 stderr 时，stdout 中的编译错误仍应对用户可见。"""
    import sys

    script = tmp_path / "compile.py"
    script.write_text(
        "import sys\n"
        "print('TS2307: missing module')\n"
        "print('$ vue-tsc --noEmit', file=sys.stderr)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    with pytest.raises(BuildError) as error:
        bld._run([sys.executable, str(script)])

    assert "命令失败(2)" in str(error.value)
    assert "$ vue-tsc --noEmit" in str(error.value)
    assert "TS2307: missing module" in str(error.value)
