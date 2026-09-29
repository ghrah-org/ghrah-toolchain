# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""共享测试夹具：动态空闲端口 + 默认端口隔离。

单元测试一律使用动态端口，**永不占用产品默认端口**（4112/5173）。
默认端口是真实部署的约定；端口是进程外的全局资源，无法用 ``GHRAH_HOME``
隔离，测试若复用默认端口，就会与同机上运行中的实例互斥（被迫停机才能跑测试）。
"""

from __future__ import annotations

import socket

import pytest

_DEFAULT_PORTS = (4112, 5173)


def _bind_free() -> tuple[socket.socket, int]:
    """bind(0) 取系统分配的空闲 loopback 端口，返回（socket, 端口）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


@pytest.fixture
def free_port_factory():
    """工厂：每次调用产出互异且空闲的 loopback 端口。

    socket 取到端口后**即刻释放**（不可持有——否则被测代码无法 bind 该端口），
    故用 ``used`` 列表保证同一次测试内多次调用互异。
    """
    used: list[int] = []

    def make() -> int:
        for _ in range(100):
            sock, port = _bind_free()
            sock.close()
            if port not in used:
                used.append(port)
                return port
        raise RuntimeError("无法取得互异空闲端口")

    return make


@pytest.fixture
def free_port(free_port_factory) -> int:
    """一个当前空闲的 loopback TCP 端口。"""
    return free_port_factory()


@pytest.fixture
def free_ports(free_port_factory) -> tuple[int, int]:
    """一对互异且空闲的 loopback TCP 端口（subject, webui）。"""
    return free_port_factory(), free_port_factory()


@pytest.fixture(autouse=True)
def _no_default_port_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试沙箱不使用产品默认端口（4112/5173）：这两个端口一律视为空闲。

    端口是进程外全局资源，无法用 ``GHRAH_HOME`` 隔离；测试若复用产品默认端口，
    就会与同机上运行中的实例互斥。此处**仅屏蔽默认端口**，其余端口仍走真实探测
    （``test_lifecycle`` 的真实探测用例不受影响）。

    与 ``test_runtime_ref`` / ``test_install_packages`` 既有的 autouse 隔离同源，
    此处收敛为全局兜底，防未来新增用例再次踩坑。
    """
    from ghrah_bootloader import adopt, boot, lifecycle, runtime_ref

    real = lifecycle.port_in_use

    def probe(host: str, port: int) -> bool:
        if port in _DEFAULT_PORTS:
            return False
        return real(host, port)

    # 各模块以 ``from ... import port_in_use`` 绑定，须逐个 patch 模块内绑定名。
    monkeypatch.setattr(adopt, "port_in_use", probe)
    monkeypatch.setattr(boot, "port_in_use", probe)
    monkeypatch.setattr(runtime_ref, "port_in_use", probe)
