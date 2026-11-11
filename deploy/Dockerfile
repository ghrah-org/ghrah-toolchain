# syntax=docker/dockerfile:1
# ghrah 容器部署（当前形态）：单容器 + host 网络，ghrah-toolchain 前台进程作为容器主进程。
# 形态、边界与用法见同目录 README.md。
#
# 构建（本目录即构建上下文；两个 ref 必须显式 pin 到精确 commit）：
#   docker build -f Dockerfile \
#     --build-arg GHRAH_TOOLCHAIN_REF=<commit> \
#     --build-arg GHRAH_MONOREPO_REF=<commit> \
#     -t ghrah-runtime:0.2.0 .
#
# 关键设计：
# - builder/runtime 使用同一 python:3.15.1-slim-trixie，保证代内 venv 的
#   bin/python 绝对符号链接在运行镜像里仍然有效（不额外携带 121M managed Python）。
# - builder 里从 node 镜像拷 node（node 26 官方镜像无 corepack，显式装 pnpm 11.25.0）。
# - install 阶段需要 git/uv/pnpm/node/gcc + 网络；runtime 只需要 python + venv + dist。
# - 容器入口必须 --foreground：默认 start 会后台化并立即返回（容器会秒退）。

ARG PYTHON_IMAGE=python:3.15.1-slim-trixie
ARG NODE_IMAGE=node:26.13-trixie-slim

FROM ${NODE_IMAGE} AS node-src

FROM ${PYTHON_IMAGE} AS builder

ARG GHRAH_TOOLCHAIN_REPO=https://github.com/ghrah-org/ghrah-toolchain.git
ARG GHRAH_TOOLCHAIN_REF=92eb95c
ARG GHRAH_MONOREPO_REPO=https://github.com/ghrah-org/ghrah-monorepo.git
ARG GHRAH_MONOREPO_REF=803fae9
# 正式部署端口；实验机如需避开已有实例，可用 --build-arg 覆盖。
ARG SUBJECT_PORT=4112
ARG WEBUI_PORT=5173

# node：只为前端构建；node 26 slim 镜像不带 corepack，用 npm 装固定 pnpm
COPY --from=node-src /usr/local/bin/node /usr/local/bin/node
COPY --from=node-src /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install --global pnpm@11.25.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git build-essential ca-certificates libatomic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# uv 走 PyPI wheel，避免额外拉 ghcr 镜像（实验环境更少外部依赖）
RUN pip install --no-cache-dir uv==0.13.7

ENV UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_TOOL_DIR=/opt/uv-tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    GHRAH_HOME=/opt/ghrah-home \
    XDG_RUNTIME_DIR=/tmp/xdg
RUN mkdir -m 700 -p /tmp/xdg   # pnpm store 锁放在 XDG_RUNTIME_DIR，受限环境必须可写

# 两个仓各自 pin 精确 commit；install 链使用 git archive，必须保留 .git
RUN git clone "$GHRAH_TOOLCHAIN_REPO" /src/ghrah-toolchain \
 && git -C /src/ghrah-toolchain checkout --detach "$GHRAH_TOOLCHAIN_REF" \
 && git clone "$GHRAH_MONOREPO_REPO" /src/ghrah-monorepo \
 && git -C /src/ghrah-monorepo checkout --detach "$GHRAH_MONOREPO_REF"

RUN uv tool install /src/ghrah-toolchain

# 安装发行版：basic 门禁只 import 六包 + 校验入口存在，不试运行起服。
RUN ghrah distro install generic \
      --repo /src/ghrah-monorepo --ref "commit:$(git -C /src/ghrah-monorepo rev-parse HEAD)" \
      --subject-port "$SUBJECT_PORT" --webui-port "$WEBUI_PORT" --gate basic

# 追加运行期业务 env（[runtime.env] 只接受 GHRAH_SUBJECT_ 前缀；不得覆盖工具链注入项）
RUN cat >> /opt/ghrah-home/generic/distro.toml <<'TOML'

[runtime.env]
GHRAH_SUBJECT_ABILITY_SOURCE = "builtin"
GHRAH_SUBJECT_ABILITY_INSTALL_ROOT = "/opt/ghrah-home/generic/ability"
GHRAH_SUBJECT_ABILITY_FILESYSTEM_ROOTS = '["/opt/ghrah-home/generic/data/workspace"]'
# 空数据卷首次启动必须有 bootstrap locator，否则 reconcile 会报
# "bootstrap_workspace_locator not configured"（容器空数据卷实测；生产实例因已有 default
# project 才没有暴露这个问题）。
GHRAH_SUBJECT_PROJECT_BOOTSTRAP_WORKSPACE_LOCATOR = "/opt/ghrah-home/generic/data/workspace/projects/default"
TOML
RUN python -c "import tomllib; tomllib.load(open('/opt/ghrah-home/generic/distro.toml','rb'))"

# 清掉构建缓存，避免带进镜像（体积估计可省 400M+）
RUN rm -rf /opt/ghrah-home/generic/cache /opt/ghrah-home/.cache \
           /root/.cache/uv /root/.local/share/pnpm /var/cache/apt/archives

# ── runtime ────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 1000 --create-home --home-dir /home/ghrah ghrah

COPY --from=builder /opt/ghrah-home /opt/ghrah-home
COPY --from=builder /opt/uv-tools /opt/uv-tools
COPY entrypoint.sh /usr/local/bin/ghrah-entrypoint

RUN chmod +x /usr/local/bin/ghrah-entrypoint \
 && mkdir -p /opt/ghrah-home/generic/data /opt/ghrah-home/generic/logs /opt/ghrah-home/generic/ability \
 && chown -R ghrah:ghrah /opt/ghrah-home /home/ghrah

ENV HOME=/home/ghrah \
    GHRAH_HOME=/opt/ghrah-home \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:/opt/uv-tools/ghrah-toolchain/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

USER ghrah
EXPOSE 4112 5173
ENTRYPOINT ["/usr/local/bin/ghrah-entrypoint"]
