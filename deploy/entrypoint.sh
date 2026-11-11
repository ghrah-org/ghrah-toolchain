#!/bin/sh
# 单容器入口：工具链前台进程 = 容器主进程。
# host 网络模式下，浏览器访问 http://127.0.0.1:<webui_port>。
set -eu
export HOME=/home/ghrah
export GHRAH_HOME=/opt/ghrah-home
export PYTHONUNBUFFERED=1

mkdir -p "$GHRAH_HOME/generic/data" "$GHRAH_HOME/generic/logs" "$GHRAH_HOME/generic/ability"

# SIGKILL/OOM 会残留会话记录；不清理会让下一次 start 误判“实例仍在运行”而拒绝启动
rm -f "$GHRAH_HOME/generic/.boot.pid" "$GHRAH_HOME/generic/.session.json"

# --foreground：前台运行并接管 SIGTERM，逆序收束（webui → OS）。
# 不要去掉 --foreground，否则 start 会后台化并立即返回，容器主进程退出。
exec ghrah distro start generic --foreground
