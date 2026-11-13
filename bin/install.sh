#!/bin/zsh
# pdfinbox 安装脚本：注册为常驻服务 + 在系统里注册成"打印机"
# 不需要 sudo：当前用户属于 _lpadmin 组即可通过认证（实测 macOS 15.2）。
set -euo pipefail

HERE=${0:A:h}
APP="$HERE/pdfinbox.py"
PY=/usr/bin/python3
CONF="$HOME/.config/pdfinbox/config.json"
LABEL=com.pdfinbox.agent
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"

[[ -x $PY ]] || { echo "找不到 $PY"; exit 1; }
[[ -f $APP ]] || { echo "找不到 $APP"; exit 1; }

# 0) 卸载分支
if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$AGENT"
  if [[ -f $CONF ]]; then
    for q in ${=$(/usr/bin/python3 - "$CONF" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
qs = [cfg.get("printer") or "PDFInbox"] + list((cfg.get("routes") or {}).keys())
print(" ".join(dict.fromkeys(qs)))
EOF
)}; do lpadmin -x "$q" 2>/dev/null || true; done
  else
    lpadmin -x PDFInbox 2>/dev/null || true
  fi
  echo "已卸载服务与队列；已生成的 PDF 与配置保留。"
  exit 0
fi

# 1) 配置
mkdir -p "$HOME/.config/pdfinbox" "$HOME/Documents/PDFInbox" "$HOME/Library/Logs"
if [[ ! -f $CONF ]]; then
  cp "$HERE/../config.example.json" "$CONF"
  echo "已生成配置 $CONF"
fi

# 2) 端口 / 队列清单
read -r PORT QUEUES <<< "$("$PY" - "$CONF" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
queues = [cfg.get("printer") or "PDFInbox"]
queues += list((cfg.get("routes") or {}).keys())
print(cfg.get("port", 8631), " ".join(dict.fromkeys(queues)))
EOF
)"

echo "端口 $PORT，队列：$QUEUES"

# 3) launchd 常驻
cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$APP</string>
        <string>--config</string>
        <string>$CONF</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/pdfinbox.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/pdfinbox.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$UID" "$AGENT" 2>/dev/null; then
  if ! launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    echo "launchd 加载失败（受限终端/沙箱常见）。请在普通终端执行："
    echo "  launchctl bootstrap gui/$UID $AGENT"
    echo "或先临时前台运行：$PY $APP --config $CONF serve"
    exit 1
  fi
fi
echo "已启动 $LABEL"

# 4) 等端口就绪
for i in {1..20}; do
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then break; fi
  sleep 0.2
done

# 5) 注册 CUPS 队列（设备 URI 的 path 即路由键，每个队列一条）
for q in ${=QUEUES}; do
  lpadmin -p "$q" -E \
    -v "ipp://127.0.0.1:$PORT/printers/$q" \
    -m everywhere \
    -o printer-is-shared=false \
    -D "PDF Inbox ($q)" -L "local" 2>&1 | grep -v '已弃用' || true
  echo "队列 $q → ipp://127.0.0.1:$PORT/printers/$q"
done

echo
echo "完成。打印对话框里现在有："
lpstat -p | sed 's/^/  /'
echo
echo "自检：$PY $APP doctor"
echo "卸载：$HERE/install.sh --uninstall"
