# ghrah 容器部署资产（deploy/）

本目录是容器部署资产的第一批落位：把 ghrah 的安装、启动与运行环境固化进一个容器镜像，
用 Compose 管理生命周期。

> 状态（2026-11-11）：**最小形态的容器运行时已实测通过**（crun，单 uid 映射 + host 网络：
> PID 1、健康检查、前端页面、WS 地址注入与握手、数据落卷、优雅停机）；
> **Dockerfile 多阶段构建本身尚未在正常 Docker/podman 主机上实测**。

## 形态

- **单容器 + host 网络**（当前选定形态）：容器内由工具链前台进程（`ghrah distro start --foreground`）作为容器主进程，
  直接监听宿主回环的 4112（后端）与 5173（前端静态页）。零产品代码改动。
- 数据卷：`ghrah-data` / `ghrah-logs` / `ghrah-ability`；模型配置 `./secrets/agentconf`（bind mount，含明文 API key，权限 600）。
- **升级模型**：重建镜像 + 保留数据卷；容器内**不**使用 `distro update`（运行镜像刻意不含 git/uv/pnpm/node，且升级要求停机）。

## 构建与运行

```sh
# 在 ghrah-toolchain 仓根执行；构建上下文是 deploy/，无需拷贝整个仓
docker build -f deploy/Dockerfile \
  --build-arg GHRAH_TOOLCHAIN_REF=<toolchain-commit> \
  --build-arg GHRAH_MONOREPO_REF=<monorepo-commit> \
  -t ghrah-runtime:0.2.0 deploy/

mkdir -p deploy/secrets/agentconf  # 放入已迁移到 schema 6 的 config.db，chmod 600
docker compose -f deploy/compose.yaml up -d
curl -fsS http://127.0.0.1:4112/health
# 浏览器：http://localhost:5173
```

停止（等待逆序收束，`stop_grace_period: 40s`）：

```sh
docker compose -f deploy/compose.yaml stop
```

## 安全基线

- 默认只适合**单机/内网**：Subject 的 WS/HTTP 无鉴权，`127.0.0.1` 监听。
- 远程访问请走 VPN/SSH 隧道；不要直接公网暴露 4112/5173。
- Agent 的 shell 在容器内以非特权用户运行；**禁止**挂载 `docker.sock`、`~/.ssh`、`~/.aws` 或宿主 home。
- 容器不是强沙箱：容器边界 = Agent 可达边界。

## 已知待办

1. 在正常 Docker 主机上实测构建 + compose 起停，并记录镜像大小/构建耗时。
2. 容器内 nginx 反代（`--no-webui` + `/ws` `/api` 代理 + 同源地址注入），支持 bridge 网络 8080 与 TLS 叠层。
3. 空数据卷首启的 `GHRAH_SUBJECT_PROJECT_BOOTSTRAP_WORKSPACE_LOCATOR` 已写入 Dockerfile `[runtime.env]`，勿删。
4. 与上游对齐：工具链 `--host/--public-ws-url` 与 Subject WS 鉴权。
