#!/bin/bash
# 把监控程序装成 macOS 后台服务:开机/登录自动运行、出错自动重启、不用开终端。
#
# 用法(在 tools/ebay-ram-monitor 文件夹里运行):
#   bash launchagent.sh install     # 安装并立即启动(重复运行 = 按最新配置重装)
#   bash launchagent.sh status      # 看是否在运行 + 最近 20 行日志
#   bash launchagent.sh log         # 实时看日志(按 Control+C 退出查看,不影响程序)
#   bash launchagent.sh restart     # 改了 .env 或 git pull 之后重启
#   bash launchagent.sh uninstall   # 停止并卸载
set -e

LABEL="com.bluebridgewell.ebay-ram-monitor"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ebay-ram-monitor.log"
DOMAIN="gui/$(id -u)"

case "$1" in
install)
  PY="$(command -v python3 || true)"
  if [ -z "$PY" ]; then echo "❌ 找不到 python3"; exit 1; fi
  if [ ! -f "$DIR/.env" ]; then echo "❌ 找不到 $DIR/.env,请先按 README 填好配置"; exit 1; fi
  case "$DIR" in
    "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
      echo "⚠️  程序在 桌面/文稿/下载 文件夹里,macOS 可能不允许后台服务读取。"
      echo "   如果日志里出现 Operation not permitted,请把整个项目移到其他位置(例如 ~/ebay-work)后重装。";;
  esac
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$PY</string>
    <string>-u</string>
    <string>$DIR/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONWARNINGS</key><string>ignore</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "✅ 已安装并启动后台服务。以后开机登录会自动运行,出错 1 分钟后自动重启。"
  echo "   日志文件:$LOG"
  echo "   查看状态:bash launchagent.sh status"
  ;;
uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✅ 已停止并卸载后台服务(数据库和 .env 都保留)"
  ;;
restart)
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "✅ 已重启"
  ;;
status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code) =" || true
  else
    echo "❌ 后台服务没有安装"
  fi
  echo "----- 最近 20 行日志 -----"
  tail -n 20 "$LOG" 2>/dev/null || echo "(还没有日志)"
  ;;
log)
  tail -n 50 -f "$LOG"
  ;;
*)
  echo "用法: bash launchagent.sh install | status | log | restart | uninstall"
  exit 1
  ;;
esac
