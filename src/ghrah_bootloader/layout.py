# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""~/.ghrah 用户级安装域布局读写（GPT）：boot.toml / distro.toml / 目录结构。

零 ghrah 依赖（tomllib 3.11+）；**缺省读容忍**——boot.toml / distro.toml
不存在时返回空世界，不抛异常。

含数据面路径助手（resolve_data_root / build_data_plane_env / ensure_data_dirs，
自 boot.py 迁入，避免 snapshot → boot → snapshot 循环导入）与配置写入底座
（BootToml.save 原子写、render_distro_toml 模板、snapshots_dir 规则）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ghrah_bootloader.errors import LayoutError

_PTH_MARKER = "ghrah-bootloader-managed"
_PACKAGE_DIRS = ("ghrah-core", "ghrah-protocol", "ghrah-subject")
INSTALL_MODES = ("packages", "worktree")
DEFAULT_INSTALL_MODE = "packages"

# 数据面五项 env 常量（自 boot.py 迁入；boot.py 保留同名 re-export 视图）
ENV_DB_PATH = "GHRAH_SUBJECT_DB_PATH"
ENV_TASKSTORE_DB = "GHRAH_SUBJECT_TASKSTORE_DB"
ENV_MANIFEST_ROOT = "GHRAH_SUBJECT_MANIFEST_ROOT"
ENV_PROJECT_ROOT_TEMPLATE = "GHRAH_SUBJECT_PROJECT_DEFAULT_ROOT_LOCATOR_TEMPLATE"
ENV_WORKSPACE_ROOT = "GHRAH_SUBJECT_WORKSPACE_ROOT"


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

    def save(self, root: Path | None = None) -> None:
        """手写三键 TOML + 原子写（tmp + ``os.replace``；不引入 TOML writer 依赖）。"""
        base = root or ghrah_root()
        base.mkdir(parents=True, exist_ok=True)
        lines = [
            "# ghrah-bootloader 引导菜单（GPT）；由 ghrah-bootloader 维护",
        ]
        for key in ("default", "fallback", "last_good"):
            value = getattr(self, key)
            if value is not None:
                lines.append(f"{key} = {_toml_str(value)}")
        content = "\n".join(lines) + "\n"
        tmp = base / f".boot.toml.tmp-{os.getpid()}"
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, base / "boot.toml")


def _toml_str(value: str) -> str:
    """最小 TOML 基本字符串转义（反斜杠 + 双引号）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class DistroToml:
    """发行版声明（五维 + repo/ref/plugins/runtime/data）。

    agent 期望状态由 Subject 侧 durable Project API 承载；引导器不含任何
    agent 注入路径（无 ``[distro.boot.agents]``）。
    """

    name: str = ""
    scenario: str = ""
    install_mode: str = DEFAULT_INSTALL_MODE
    repo_path: str = ""
    ref_source: str = "latest"
    ref_gate: str = "basic"
    ref_fetch_on_update: bool = False
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
        install_mode = str(distro.get("install_mode", DEFAULT_INSTALL_MODE))
        if install_mode not in INSTALL_MODES:
            raise LayoutError(
                f"非法 install_mode: {install_mode!r}（应为 {' | '.join(INSTALL_MODES)}）"
            )
        return cls(
            name=str(distro.get("name", "")),
            scenario=str(distro.get("scenario", "")),
            install_mode=install_mode,
            repo_path=str(repo.get("path", "")),
            ref_source=str(ref.get("source", "latest")),
            ref_gate=str(ref.get("gate", "basic")),
            ref_fetch_on_update=bool(ref.get("fetch_on_update", False)),
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

    @property
    def generations(self) -> Path:
        return self.root / "generations"

    def list_versions(self) -> list[str]:
        """列出已安装版本目录名（排序；目录不存在返回空）。"""
        if not self.versions.is_dir():
            return []
        return sorted(p.name for p in self.versions.iterdir() if p.is_dir())

    def list_generations(self) -> list[str]:
        """列出已物化组合代目录名（排序；目录不存在返回空）。"""
        if not self.generations.is_dir():
            return []
        return sorted(p.name for p in self.generations.iterdir() if p.is_dir())


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


# ── 数据面路径（自 boot.py 迁入） ────────────────────────────


def resolve_data_root(layout: DistroLayout, distro_toml: DistroToml) -> Path:
    """数据盘根：``[data] root`` 覆盖（可含 ``~``），默认 ``<distro>/data``。"""
    if distro_toml.data_root:
        return Path(os.path.expanduser(distro_toml.data_root))
    return layout.data


def build_data_plane_env(data_root: Path) -> dict[str, str]:
    """数据面五项 env：全部指向数据盘，不落任何版本的默认位。"""
    return {
        ENV_DB_PATH: str(data_root / "subject.db"),
        ENV_TASKSTORE_DB: str(data_root / "taskstore.sqlite3"),
        ENV_MANIFEST_ROOT: str(data_root / "manifests"),
        ENV_PROJECT_ROOT_TEMPLATE: str(data_root / "projects" / "{project_id}"),
        ENV_WORKSPACE_ROOT: str(data_root / "workspace"),
    }


def ensure_data_dirs(data_root: Path) -> None:
    """物化数据盘目录结构（库文件由 OS 自建；目录由引导器保证）。"""
    for sub in ("manifests", "projects", "workspace"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)


def snapshots_dir(layout: DistroLayout, distro_toml: DistroToml) -> Path:
    """快照目录：与数据盘根同级（默认 ``<distro>/snapshots``，外置时同级跟随）。"""
    return resolve_data_root(layout, distro_toml).parent / "snapshots"


def render_distro_toml(
    name: str,
    repo_path: str,
    source: str,
    gate: str,
    subject_port: int,
    webui_port: int,
    data_root: str,
    snapshot_keep: int,
) -> str:
    """首次 install 的 distro.toml 模板（手写序列化；``DistroToml.load`` 可读回）。"""
    lines = [
        "# ghrah-bootloader 发行版声明（首次 install 生成）",
        "",
        "[distro]",
        f"name = {_toml_str(name)}",
        'scenario = ""',
        f"install_mode = {_toml_str(DEFAULT_INSTALL_MODE)}",
        "",
        "[distro.repo]",
        f"path = {_toml_str(repo_path)}",
        "",
        "[distro.ref]",
        f"source = {_toml_str(source)}",
        f"gate = {_toml_str(gate)}",
        "fetch_on_update = false",
        "",
        "[distro.plugins]",
        "enabled = []",
        "",
        "[runtime]",
        f"subject_port = {subject_port}",
        f"webui_port = {webui_port}",
        "",
        "[data]",
    ]
    if data_root:
        lines.append(f"root = {_toml_str(data_root)}")
    lines.append(f"snapshot_keep = {snapshot_keep}")
    return "\n".join(lines) + "\n"


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
