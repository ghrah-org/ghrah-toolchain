# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""健康检查：stdlib ``urllib.request`` 轮询 HTTP JSON 健康端点。"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5
_REQUEST_TIMEOUT = 2.0


async def wait_for_server(url: str, timeout: float = 15.0, name: str = "Server") -> dict:
    """轮询 HTTP 健康端点直到就绪或超时。

    Args:
        url: 健康端点 URL（如 ``http://127.0.0.1:4112/health``）。
        timeout: 总超时秒数。
        name: 服务名（日志用）。

    Returns:
        健康端点返回的 JSON 对象。

    Raises:
        TimeoutError: 超时未就绪。
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None

    while time.monotonic() < deadline:
        try:
            data = fetch_health(url)
            logger.info("%s is ready: %s", name, data)
            return data
        except Exception as e:  # noqa: BLE001 - 轮询需吞掉瞬时网络错误
            last_exc = e
            await _sleep(_POLL_INTERVAL)

    raise TimeoutError(
        f"{name} at {url} did not become ready within {timeout}s. Last error: {last_exc}"
    )


def fetch_health(url: str) -> dict:
    """单次请求健康端点，返回 JSON 对象；非 200 或解析失败抛异常。"""
    with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:  # noqa: S310
        if resp.status != 200:
            raise RuntimeError(f"health endpoint returned {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
