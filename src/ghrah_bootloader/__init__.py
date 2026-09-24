# SPDX-FileCopyrightText: 2026 chenxya <chenxya@ghrah.org>
#
# # SPDX-License-Identifier: Apache-2.0

"""ghrah AgentOS 安装/配置工具与引导器。

boot 路径硬约束：本包 import 图内零 ``ghrah.*``、零第三方运行时依赖（仅 stdlib）。
引导器与 agent 无关：agent 期望状态由 Subject 侧 durable Project API 承载，
引导器不含任何 agent 注入路径。
"""

__version__ = "0.1.0"
