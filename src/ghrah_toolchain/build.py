# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""发行物构建侧：源仓 commit → wheels/tarballs/observer-web/manifest/BOOTSTRAP。

包管理化第一步（只做构建侧，不动安装域与状态机）：

```
git archive <commit> → <staging>/src/          # 钉死 commit，零 worktree 元数据
  ├── Python 五包逐包 uv build --wheel → <staging>/wheels/
  ├── uv export --frozen（第三方 pin 集）→ <staging>/wheels/pins.txt
  ├── pnpm（workspace 根 = 源仓根）install --frozen-lockfile
  │   → 逐包 --filter build（按依赖序）→ pack 三包 → <staging>/tarballs/
  │   → observer-web（private）整包拷贝构建产物形态 → <staging>/observer-web/
  └── manifest.json + BOOTSTRAP.toml → os.replace(staging, out_dir)
```

纯 stdlib + ``uv``/``pnpm``/``git`` 子进程；失败整 staging 清理（out 要么完整要么不存在）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ghrah_toolchain import __version__
from ghrah_toolchain.errors import BuildError
from ghrah_toolchain.materialize import pnpm_command

logger = logging.getLogger(__name__)

# 包管理面常量（测试以 fixtures 小仓 monkeypatch 重定向）
PYTHON_PACKAGES = (
    "ghrah-core",
    "ghrah-protocol",
    "ghrah-plugin",
    "ghrah-taskstore",
    "ghrah-subject",
)
# webui 构建序 = 依赖拓扑序（protocol → plugin-registry → observer-core → observer-web）
WEBUI_PACKAGES = (
    "@ghrah/protocol",
    "@ghrah/plugin-registry",
    "@ghrah/observer-core",
    "@ghrah/observer-web",
)
WEBUI_PACK_TARBALLS = WEBUI_PACKAGES[:-1]  # observer-web 是 private，不 pack
OBSERVER_WEB_COPY = ("dist", "public", "locales", "package.json")
WEBUI_ROOT_DIR = "ghrah-observer-webui"
OBSERVER_WEB_PACKAGE_DIR = "observer-web"
MANIFEST_SCHEMA_VERSION = 1

# 验证面常量（fixtures 小仓映射到自有 import 面/入口）
VERIFY_IMPORTS = "import ghrah.core, ghrah.protocol, ghrah.subject, ghrah.plugin, ghrah.taskstore"
VERIFY_ENTRY_POINT = "ghrah-subject-server"


@dataclass(frozen=True)
class BuildResult:
    """构建产物摘要（CLI 展示与测试断言面）。"""

    out_dir: Path
    source_commit: str
    wheels: list[str]
    tarballs: list[str]
    pins_file: Path


# ── 子进程与哈希辅助 ─────────────────────────────────────────


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise BuildError(
            f"命令失败({cp.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"{cp.stderr.strip()[-2000:] or cp.stdout.strip()[-2000:]}"
        )
    return cp


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 构建流程 ─────────────────────────────────────────────────


def normalize_commit(repo: Path, ref: str) -> str:
    """归一化候选 ref → 完整 commit sha（``rev-parse --verify <x>^{commit}`` peel）。"""
    cp = _run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
    if cp.returncode != 0 or not cp.stdout.strip():
        raise BuildError(f"候选 ref 解析失败: {ref!r}（{cp.stderr.strip() or '未知 ref'}）")
    return cp.stdout.strip()


def read_source_commit(tree: Path) -> str | None:
    """读产物树 ``BOOTSTRAP.toml`` 的 ``source_commit``（纯文件身份，零 git）。

    文件缺失 / 解析失败 / 缺键 → ``None``（调用方决定回退策略）。
    """
    bootstrap = Path(tree) / "BOOTSTRAP.toml"
    if not bootstrap.is_file():
        return None
    try:
        data = tomllib.loads(bootstrap.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError, OSError:
        return None
    commit = data.get("bootstrap", {}).get("source_commit")
    return str(commit) if commit else None


def build_artifacts(repo: Path, commit: str, *, out_dir: Path) -> BuildResult:
    """从源仓固定 commit 构建发行物树并原子落位 ``out_dir``。

    staging 目录全程隔离，成功后 :func:`os.replace`；失败清理 staging、
    ``out_dir`` 保持不存在（要么完整要么不存在）。
    """
    repo = Path(repo)
    out_dir = Path(out_dir)
    sha = normalize_commit(repo, commit)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise BuildError(f"产物目录已存在且非空: {out_dir}（如需重建请先移除）")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f"{out_dir.name}.tmp-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        result = _build_into(repo, sha, staging)
        if out_dir.exists():  # 仅空目录（非空已在上方拒绝）；清掉再原子落位
            out_dir.rmdir()
        os.replace(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return replace(result, out_dir=out_dir)


def _build_into(repo: Path, sha: str, staging: Path) -> BuildResult:
    wheels_dir = staging / "wheels"
    tarballs_dir = staging / "tarballs"
    src = _archive_source(repo, sha, staging)
    wheel_files = _build_wheels(src, wheels_dir)
    pins_file = _export_pins(src, wheels_dir)
    tarball_entries = _build_webui(src, tarballs_dir, staging)
    shutil.rmtree(src)  # 产物树不含源码 checkout（纯产物）
    _write_bootstrap(staging, sha)
    _write_manifest(staging, sha, wheel_files, pins_file, tarball_entries)
    return BuildResult(
        out_dir=staging,
        source_commit=sha,
        wheels=[w.name for w in wheel_files],
        tarballs=[t.name for t, _, _ in tarball_entries],
        pins_file=pins_file.relative_to(staging),
    )


def _archive_source(repo: Path, sha: str, staging: Path) -> Path:
    """``git archive`` → ``<staging>/src/``（tarfile 解包，不依赖系统 tar）。"""
    src_tar = staging / "src.tar"
    _run(["git", "-C", str(repo), "archive", "--format=tar", f"--output={src_tar}", sha])
    src = staging / "src"
    src.mkdir(parents=True)
    with tarfile.open(src_tar) as tf:
        try:
            tf.extractall(src, filter="data")
        except TypeError:  # Python <3.12 无 filter 参数
            tf.extractall(src)
    src_tar.unlink()
    return src


def _build_wheels(src: Path, wheels_dir: Path) -> list[Path]:
    """五包逐包 ``uv build --wheel``（根 pyproject 是 legacy backend，整仓构建失败）。"""
    wheels_dir.mkdir(parents=True)
    for pkg in PYTHON_PACKAGES:
        pkg_dir = src / pkg
        if not pkg_dir.is_dir():
            raise BuildError(f"源树缺少 Python 包目录: {pkg_dir}")
        _run(
            [
                "uv",
                "build",
                str(pkg_dir),
                "--wheel",
                f"--out-dir={wheels_dir}",
                "--no-create-gitignore",
            ]
        )
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise BuildError(f"未产出任何 wheel: {wheels_dir}")
    return wheels


def _export_pins(src: Path, wheels_dir: Path) -> Path:
    """``uv export --frozen`` → ``wheels/pins.txt``（仅第三方闭包；安装期零解析）。

    ``--no-emit-project/--no-emit-workspace`` 排除根项目与五包自身（它们来自
    wheels）；``--no-dev`` 排除开发组——pin 集是运行时闭包。
    """
    pins = wheels_dir / "pins.txt"
    _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-emit-workspace",
            f"--project={src}",
            f"--output-file={pins}",
        ]
    )
    if not pins.is_file() or not pins.read_text(encoding="utf-8").strip():
        raise BuildError(f"pin 集导出为空: {pins}")
    return pins


def _build_webui(src: Path, tarballs_dir: Path, staging: Path) -> list[tuple[Path, str, str]]:
    """webui：install → 按依赖序逐包 build → pack 三包；observer-web 拷贝产物形态。

    pnpm workspace 根 = 源仓根（pnpm-workspace.yaml 在 monorepo 根，非 webui
    子目录）；不用根 ``build`` 脚本（含 vscode-extension，超出运行时闭包）。
    返回 [(tarball, name, version)]——name/version 从各包 package.json 权威读取
    （文件名解析对 ``0.2.0-alpha.0`` 类预发布版本不可靠）。
    """
    if not (src / "pnpm-workspace.yaml").is_file():
        raise BuildError(f"源树缺少 pnpm-workspace.yaml: {src}")
    webui = src / WEBUI_ROOT_DIR
    if not webui.is_dir():
        raise BuildError(f"源树缺少 webui 目录: {webui}")
    _run(pnpm_command() + ["install", "--frozen-lockfile"], cwd=src)
    for pkg in WEBUI_PACKAGES:
        _run(pnpm_command() + ["--filter", pkg, "run", "build"], cwd=src)
    tarballs_dir.mkdir(parents=True)
    entries: list[tuple[Path, str, str]] = []
    for pkg in WEBUI_PACK_TARBALLS:
        # pnpm pack 不支持 --filter（非递归命令）；--dir 直指包目录
        pkg_dir = webui / "packages" / pkg.rsplit("/", 1)[-1]
        _run(
            pnpm_command() + [f"--dir={pkg_dir}", "pack", f"--pack-destination={tarballs_dir}"],
            cwd=src,
        )
        meta = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
        produced = [t for t in tarballs_dir.glob("*.tgz") if t not in [e[0] for e in entries]]
        if len(produced) != 1:
            raise BuildError(f"pack 产物数量异常（{pkg}）: {[t.name for t in produced]}")
        entries.append((produced[0], str(meta["name"]), str(meta["version"])))
    observer_src = webui / "packages" / OBSERVER_WEB_PACKAGE_DIR
    observer_dst = staging / "observer-web"
    observer_dst.mkdir(parents=True)
    copied: list[str] = []
    for item in OBSERVER_WEB_COPY:
        s = observer_src / item
        if item == "package.json":
            if s.is_file():
                shutil.copy2(s, observer_dst / s.name)
                copied.append(s.name)
        elif s.is_dir():
            shutil.copytree(s, observer_dst / item)
            copied.append(f"{item}/")
    if "package.json" not in copied:
        raise BuildError(f"observer-web 拷贝失败：缺少 package.json: {observer_src}")
    tarballs = entries
    if not tarballs:
        raise BuildError(f"未产出任何 tarball: {tarballs_dir}")
    return tarballs


def _write_bootstrap(staging: Path, sha: str) -> None:
    built_at = datetime.now(UTC).isoformat(timespec="seconds")
    text = (
        "[bootstrap]\n"
        f'source_commit = "{sha}"\n'
        f'built_at = "{built_at}"\n'
        f'builder = "ghrah-toolchain {__version__}"\n'
    )
    (staging / "BOOTSTRAP.toml").write_text(text, encoding="utf-8")


def _write_manifest(
    staging: Path,
    sha: str,
    wheels: list[Path],
    pins_file: Path,
    tarballs: list[tuple[Path, str, str]],
) -> None:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source_commit": sha,
        "python": {
            "wheels": [
                {
                    "name": _wheel_name(w.name),
                    "version": _wheel_version(w.name),
                    "file": f"wheels/{w.name}",
                    "sha256": _sha256_file(w),
                }
                for w in wheels
            ],
            "pins_file": "wheels/pins.txt",
            "pins_sha256": _sha256_file(pins_file),
        },
        "web": {
            "tarballs": [
                {
                    "name": name,
                    "version": version,
                    "file": f"tarballs/{t.name}",
                    "sha256": _sha256_file(t),
                }
                for t, name, version in tarballs
            ],
            "observer_web": {"files": _observer_web_listing(staging)},
        },
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _wheel_name(whl: str) -> str:
    return whl.split("-", 1)[0].replace("_", "-")


def _wheel_version(whl: str) -> str:
    return whl.split("-")[1]


def _observer_web_listing(staging: Path) -> list[str]:
    observer = staging / "observer-web"
    return sorted(
        f"{child.name}/" if child.is_dir() else child.name for child in observer.iterdir()
    )


# ── 验证 ─────────────────────────────────────────────────────


def verify_artifacts(out_dir: Path) -> None:
    """把产物树当黑盒验证：清单完整 → 全新 venv 安装 → import 冒烟 + 入口存在。"""
    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BuildError(f"缺少 manifest.json: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["python"]["wheels"] + manifest["web"]["tarballs"]:
        p = out_dir / entry["file"]
        if not p.is_file():
            raise BuildError(f"清单条目缺失: {entry['file']}")
        actual = _sha256_file(p)
        if actual != entry["sha256"]:
            raise BuildError(
                f"sha256 不符: {entry['file']}（清单 {entry['sha256'][:12]}… 实测 {actual[:12]}…）"
            )
    pins = out_dir / manifest["python"]["pins_file"]
    if not pins.is_file():
        raise BuildError(f"pin 集缺失: {pins}")
    if _sha256_file(pins) != manifest["python"]["pins_sha256"]:
        raise BuildError(f"pin 集 sha256 不符: {pins}")

    with tempfile.TemporaryDirectory(prefix="bootloader-verify-") as td:
        venv = Path(td) / "venv"
        win = os.name == "nt"
        py = venv / ("Scripts/python.exe" if win else "bin/python")
        _run(["uv", "venv", str(venv)])
        install = ["uv", "pip", "install", f"--python={py}"]
        install += [str(w) for w in sorted((out_dir / "wheels").glob("*.whl"))]
        install += ["-r", str(pins)]
        _run(install)
        _run([str(py), "-c", VERIFY_IMPORTS])
        server = venv / (
            f"Scripts/{VERIFY_ENTRY_POINT}.exe" if win else f"bin/{VERIFY_ENTRY_POINT}"
        )
        if not server.exists():
            raise BuildError(f"entry point 缺失: {server}")
        # 入口存在性校验即可：真实入口 main() 不解析参数（--help 会直接启动
        # 服务器占端口），拉起冒烟属 boot 域验证，不在构建验证面
    logger.info("产物验证通过: %s", out_dir)
