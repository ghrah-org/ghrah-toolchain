# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""引导器纯性门禁。

boot 路径硬约束：``ghrah_bootloader.cli`` 的 import 图内零 ``ghrah.*``、
零第三方运行时依赖（子进程遍历 sys.modules 断言）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_boot_import_graph_has_no_ghrah() -> None:
    code = (
        "import sys\n"
        "import ghrah_bootloader.cli\n"
        "bad = [m for m in sys.modules if m == 'ghrah' or m.startswith('ghrah.')]\n"
        "print('BAD:' + ','.join(bad) if bad else 'CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(_SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("CLEAN"), proc.stdout


def test_boot_import_graph_no_third_party() -> None:
    """cli import 图零第三方运行时依赖（除 stdlib、自身与 venv 运行时注入外）。"""
    code = (
        "import sys\n"
        "import ghrah_bootloader.cli\n"
        "import sysconfig\n"
        "std = sysconfig.get_paths()['stdlib']\n"
        "allowed = {'ghrah_bootloader', '_virtualenv', '_distutils_hack'}\n"
        "bad = []\n"
        "for m, mod in list(sys.modules.items()):\n"
        "    top = m.split('.')[0]\n"
        "    if top in allowed:\n"
        "        continue\n"
        "    f = getattr(mod, '__file__', None)\n"
        "    if f is None:\n"
        "        continue\n"
        "    if not str(f).startswith(std):\n"
        "        bad.append(m)\n"
        "print('BAD:' + ','.join(sorted(set(bad))) if bad else 'CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(_SRC)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("CLEAN"), proc.stdout


def test_no_provisioning_surface() -> None:
    """退役约定：包内不得再有 provisioning/resources 路径与 ProvisioningError。"""
    pkg = _SRC / "ghrah_bootloader"
    assert not (pkg / "provisioning.py").exists()
    assert not (pkg / "resources").exists()
    for py in pkg.rglob("*.py"):
        content = py.read_text(encoding="utf-8")
        assert "provision" not in content.lower(), f"{py} 残留 provisioning 引用"
