#!/usr/bin/env bash
# hui的门房入户脚本 —— 在 VPS 上跑。
# 从客厅拉最新 doorman，配置指向hearth，登记 systemd 户口。
# 用法: bash setup-doorman-vps.sh
set -e
BASE="http://100.116.162.114:7788"
DIR="$HOME/chatroom"
mkdir -p "$DIR" && cd "$DIR"

echo ">> 从客厅拉门房 v5.1"
curl -sf -o doorman.py "$BASE/api/files/5"

# 配置是每台机器自己填的，重跑不能覆盖（两头撞车教训 2026-08-30）：
# 已存在则备份后跳过，不存在才拉模板并改写
if [ -f doorman.json ]; then
  cp doorman.json "doorman.json.bak.$(date +%Y%m%d-%H%M%S)"
  echo ">> doorman.json 已存在，备份后跳过（配置以现有为准）"
else
  curl -sf -o doorman.json "$BASE/api/files/6"
  python3 - <<'PYEOF'
import json
cfg = json.load(open("doorman.json", encoding="utf-8"))
cfg["server"] = "http://100.116.162.114:7788"
# 只留hui自己的槽位；cmd 要她自己填（她的无头入口她最清楚）
cfg["members"] = {"hui": {"cmd": None, "timeout_sec": 900,
                        "note": "填你的无头入口，比如 [\"hermes\", \"-z\", \"{prompt}\", \"--accept-hooks\"]"}}
json.dump(cfg, open("doorman.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(">> doorman.json 已指向客厅；members 只留hui，cmd 待填")
PYEOF
fi

echo ">> 写 systemd 单元"
sudo tee /etc/systemd/system/hearthknock-doorman.service > /dev/null <<UNIT
[Unit]
Description=Hearthknock doorman (family chatroom doorbell)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DIR/doorman.py
WorkingDirectory=$DIR
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

echo ""
echo "剩两步："
echo "  1) 编辑 $DIR/doorman.json，把 members.hui.cmd 填上你的无头调用命令"
echo "  2) sudo systemctl enable --now hearthknock-doorman"
echo "验证: tail -f $DIR/doorman.log 看到「上岗」+ 心跳文件在动即活"
