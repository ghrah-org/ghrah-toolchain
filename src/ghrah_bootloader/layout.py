# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""~/.ghrah 用户级安装域布局读写（GPT）：boot.toml / distro.toml / 目录结构。

零 ghrah 依赖（tomllib 3.11+）；**缺省读容忍**——boot.toml / distro.toml
不存在时返回空世界，不抛异常。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ghrah_bootloader.errors import LayoutError

_PTH_MARKER = "ghrah-bootloader-managed"
_PACKAGE_DIRS = ("ghrah-core", "ghrah-protocol", "ghrah-subject")


def ghrah_root() -> Path:
    """安装域根（``~/.ghrah``）；可用 env ``GHRAH_HOME`` 重定向（测试用）。"""
    env = os.environ.get("GHRAH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ghrah"


@dataclass
class BootToml:
    """引导菜单（GPT 头）：default / fallback / last_good（"<distro>/<version>" 对）。"""

    default: str | None = None
    fallback: str | None = None
    last_good: str | None = None

    @classmethod
    def load(cls, root: Path | None = None) -> BootToml:
        """读 boot.toml；文件不存在时返回空对象（缺省读容忍）。"""
        path = (root or ghrah_root()) / "boot.toml"
        if not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise LayoutError(f"boot.toml 解析失败: {path}: {e}") from e
        return cls(
            default=data.get("default"),
            fallback=data.get("fallback"),
            last_good=data.get("last_good"),
        )


@dataclass
class DistroToml:
    """发行版声明（五维 + repo/ref/plugins/runtime/data）。

    agent 期望状态由 Subject 侧 durable Project API 承载；引导器不含任何
    agent 注入路径（无 ``[distro.boot.agents]``）。
    """

    name: str = ""
    scenario: str = ""
    repo_path: str = ""
    ref_source: str = "latest"
    ref_gate: str = "basic"
    enabled_plugins: list[str] = field(default_factory=list)
    subject_port: int = 4112
    webui_port: int = 5173
    data_root: str = ""
    snapshot_keep: int = 3

    @classmethod
    def load(cls, distro_dir: Path) -> DistroToml:
        """读 <distro>/distro.toml；不存在时返回空对象（缺省读容忍）。"""
        path = distro_dir / "distro.toml"
        if not path.is_file():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise LayoutError(f"distro.toml 解析失败: {path}: {e}") from e
        distro = data.get("distro", {})
        repo = distro.get("repo", {})
        ref = distro.get("ref", {})
        plugins = distro.get("plugins", {})
        runtime = data.get("runtime", {})
        data_section = data.get("data", {})
        return cls(
            name=str(distro.get("name", "")),
            scenario=str(distro.get("scenario", "")),
            repo_path=str(repo.get("path", "")),
            ref_source=str(ref.get("source", "latest")),
            ref_gate=str(ref.get("gate", "basic")),
            enabled_plugins=[str(p) for p in plugins.get("enabled", [])],
            subject_port=int(runtime.get("subject_port", 4112)),
            webui_port=int(runtime.get("webui_port", 5173)),
            data_root=str(data_section.get("root", "")),
            snapshot_keep=int(data_section.get("snapshot_keep", 3)),
        )


@dataclass
class DistroLayout:
    """单个发行版的磁盘布局视图。"""

    root: Path  # ~/.ghrah/<distro>/

    @property
    def distro_toml(self) -> Path:
        return self.root / "distro.toml"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def env(self) -> Path:
        return self.root / "env"

    @property
    def venv(self) -> Path:
        return self.env / ".venv"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def last_good(self) -> Path:
        return self.root / "last-good"

    @property
    def versions(self) -> Path:
        return self.root / "versions"

    def list_versions(self) -> list[str]:
        """列出已安装版本目录名（排序；目录不存在返回空）。"""
        if not self.versions.is_dir():
            return []
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir())


def list_distros(root: Path | None = None) -> list[str]:
    """枚举安装域中的发行版目录（GPT 分区表读取）。

    跳过非目录项与 ``boot.toml``；根不存在返回空列表（空世界）。
    """
    base = root or ghrah_root()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def resolve_distro_root(distro: str, root: Path | None = None) -> Path:
    """发行版根目录路径。"""
    if not distro or "/" in distro or distro.startswith("."):
        raise LayoutError(f"非法发行版名: {distro!r}")
    return (root or ghrah_root()) / distro


# ── env 导入路径自管（裁定 11） ──────────────────────────────


def managed_pth_path(venv: Path) -> Path:
    """自管 .pth 在 venv site-packages 内的落点。"""
    site_packages = _find_site_packages(venv)
    return site_packages / "ghrah_bootloader_managed.pth"


def write_managed_pth(venv: Path, current_link: Path) -> Path:
    """写入/复核自管 .pth：使 venv 的导入路径经 ``current`` 稳定段指向运行树。

    不依赖 setuptools editable 的 .pth 解析（spike 实证其解析为版本绝对路径，
    重指 current 不生效）。内容为各包 src 目录（含 current 段的稳定路径）。
    """
    site_packages = _find_site_packages(venv)
    lines = [
        f"# {_PTH_MARKER}: do not edit; managed by ghrah-bootloader",
    ]
    for pkg in _PACKAGE_DIRS:
        lines.append(str(current_link / pkg / "src"))
    pth = site_packages / "ghrah_bootloader_managed.pth"
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth


def verify_managed_pth(venv: Path, current_link: Path) -> bool:
    """复核 .pth 与 current 段一致；缺失或不一致返回 False（调用方应重写）。"""
    pth = managed_pth_path(venv)
    if not pth.is_file():
        return False
    expected = {str(current_link / pkg / "src") for pkg in _PACKAGE_DIRS}
    actual = {
        line.strip()
        for line in pth.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    return actual == expected


def _find_site_packages(venv: Path) -> Path:
    """定位 venv 的 site-packages（posix/lib/pythonX.Y/site-packages | nt/Lib/site-packages）。"""
    if os.name == "nt":
        candidate = venv / "Lib" / "site-packages"
        if candidate.is_dir():
            return candidate
    else:
        lib = venv / "lib"
        if lib.is_dir():
            for entry in sorted(lib.iterdir(), reverse=True):
                candidate = entry / "site-packages"
                if entry.name.startswith("python") and candidate.is_dir():
                    return candidate
    raise LayoutError(f"venv site-packages 未找到: {venv}")
