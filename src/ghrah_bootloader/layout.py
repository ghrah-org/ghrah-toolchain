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

# 已退役的安装模式键值：worktree 形态（git worktree 版本树 + 发行级 env + 自管
# .pth）存量声明若仍写该值，load 显式报错而非静默容忍。
_RETIRED_INSTALL_MODE = "worktree"

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
        install_mode = distro.get("install_mode")
        if install_mode is not None:
            if str(install_mode) == _RETIRED_INSTALL_MODE:
                raise LayoutError(
                    f"install_mode = {_RETIRED_INSTALL_MODE!r} 已退役："
                    "worktree 安装形态不再支持；请删除该键（缺省即 packages 形态），"
                    "迁移步骤见 README「迁移」段"
                )
            raise LayoutError(
                f"install_mode 键已退役（收到 {install_mode!r}）；"
                "请删除该键（缺省即 packages 形态），迁移步骤见 README「迁移」段"
            )
        return cls(
            name=str(distro.get("name", "")),
            scenario=str(distro.get("scenario", "")),
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
