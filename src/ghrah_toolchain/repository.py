# SPDX-License-Identifier: Apache-2.0
"""本地源码位置及工具链拥有的远程 Git 缓存。"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ghrah_toolchain.errors import RuntimeRefError

logger = logging.getLogger(__name__)


def require_git() -> None:
    if shutil.which("git") is None:
        raise RuntimeRefError(
            "从源码仓安装／更新需要 Git；请安装 Git 并确保 git 在 PATH 中。"
            "已安装实例的 start/stop/status/logs 不需要 Git。"
        )


@dataclass(frozen=True)
class RepositorySource:
    location: str
    remote: bool

    @classmethod
    def parse(cls, value: str | Path) -> RepositorySource:
        """保留远程身份；本地路径规范化，不将 URL 转成当前目录下的 Path。"""
        text = str(value)
        if not text or any(ord(char) < 32 for char in text) or text.startswith("-"):
            raise RuntimeRefError("源码仓位置无效")
        if "://" in text:
            try:
                parts = urlsplit(text)
                if parts.scheme not in {"https", "http", "ssh", "file"}:
                    raise ValueError("不支持的 Git URL 协议")
                if parts.query or parts.fragment or parts.password is not None:
                    raise ValueError("仓库 URL 不能包含密码、查询参数或片段")
                if parts.scheme in {"http", "https"} and parts.username is not None:
                    raise ValueError("HTTPS 认证请通过 Git credential helper 配置，不写入仓库 URL")
                if parts.scheme != "file" and (not parts.hostname or not parts.path.strip("/")):
                    raise ValueError("仓库 URL 缺少主机或路径")
                if parts.scheme == "file" and (
                    parts.netloc not in {"", "localhost"} or not parts.path.startswith("/")
                ):
                    raise ValueError("file URL 必须指向本机绝对路径")
            except ValueError as exc:
                raise RuntimeRefError(f"无效仓库 URL: {exc}") from None
            return cls(text, True)
        # scp 风格只识别显式 user@host:path，避免把 Windows 盘符误认成主机。
        if re.fullmatch(r"[^/@:\s]+@[^/:\s]+:.+", text):
            return cls(text, True)
        return cls(str(Path(text).expanduser().resolve()), False)


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    require_git()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        result = subprocess.run(
            ["git", *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeRefError("远程 Git 操作超过 300 秒，未切换安装版本；请检查网络或认证") from exc
    if result.returncode:
        raise RuntimeRefError(f"Git 操作失败，未切换安装版本: {result.stderr.strip()[-2000:]}")
    return result


@dataclass(frozen=True)
class RemoteRepository:
    home: Path
    source: RepositorySource

    @property
    def root(self) -> Path:
        digest = hashlib.sha256(self.source.location.encode()).hexdigest()
        return self.home / ".cache" / "repositories" / digest

    @property
    def path(self) -> Path:
        return self.root / "repo.git"

    def cached(self) -> Path:
        """只读路径，不创建目录、不克隆、不 fetch。"""
        if not (self.path / "HEAD").is_file():
            raise RuntimeRefError("远程仓库尚无本地缓存；请先安装，或执行 update --fetch 重建缓存")
        actual = _git(["-C", str(self.path), "config", "--get", "remote.origin.url"]).stdout.strip()
        if actual != self.source.location:
            raise RuntimeRefError("远程仓库缓存来源不匹配，拒绝复用")
        return self.path

    @contextlib.contextmanager
    def prepare(self, *, refresh: bool, allow_clone: bool):
        """同一 URL 的 clone／fetch／固定提交构建持同一锁，避免并发移动引用。"""
        from ghrah_toolchain.runtime_ref import acquire_distro_lock

        require_git()
        with acquire_distro_lock(self.root):
            created = False
            if not self.path.exists():
                if not allow_clone:
                    raise RuntimeRefError("远程缓存不存在；本次禁止联网准备，请使用 update --fetch")
                logger.info("克隆远程源码仓（首次准备缓存）: %s", self.source.location)
                staging = Path(tempfile.mkdtemp(prefix=".clone-", dir=self.root))
                try:
                    _git(["clone", "--mirror", "--", self.source.location, str(staging)])
                    os.replace(staging, self.path)
                    created = True
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            repo = self.cached()
            if refresh and not created:
                logger.info("同步远程源码仓: %s", self.source.location)
                # --atomic 确保失败时不留下半组更新的 ref；缓存没有工作树。
                _git(["-C", str(repo), "fetch", "--atomic", "--prune", "origin"])
                # fetch 不自动更新 bare HEAD。按远端默认分支单独核对，避免沿用已删除分支。
                advertised = _git(["-C", str(repo), "ls-remote", "--symref", "origin", "HEAD"])
                for line in advertised.stdout.splitlines():
                    if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                        branch = line.split("\t", 1)[0].removeprefix("ref: ")
                        _git(["-C", str(repo), "symbolic-ref", "HEAD", branch])
                        break
                else:
                    raise RuntimeRefError("远程仓库没有可解析的默认分支；未切换安装版本")
            yield repo
