#!/usr/bin/env python3
"""v7.11 断线三件套三形态亲触发（hui侧验收）。

不动生产：独立临时目录 + 假hearth http.server + cmd=/bin/echo 的成员。
- 形态A 断线：关假hearth → patrol 连续零成功 → 断线台账起账、计数
- 形态B 点火：first_fail 拨回 25h 前 → 超阈值 → 本地点火成员（真 subprocess /bin/echo）
- 形态C 补投：重启假hearth → patrol 成功轮 → 断线账 POST 进hearth、销账
"""
import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
import doorman

tmp = Path(tempfile.mkdtemp(prefix="v711-livecheck-"))
doorman.BASE = tmp
doorman.LOG = tmp / "doorman.log"
doorman.STATE_FILE = tmp / "doorman-state.json"
doorman.HB = tmp / "doorman.heartbeat"

state = {"last_wake": {}, "wake_log": {}, "closing_notified": [], "recovery_alerted": [],
         "l3_alerted": {}, "blackout": None, "last_patrol_ok": None}
sent = []          # 假hearth收到的 POST /api/send（补投断线账会进这里）


class Hearth(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/rooms"):
            self._send({"rooms": [{"room": "大厅", "members": ["hui"], "state": "open", "admin": None}]})
        elif self.path.startswith("/api/cursors"):
            self._send({"cursors": [{"member": "hui", "room": "大厅", "cursor": 999}]})
        elif self.path.startswith("/api/messages"):
            self._send({"messages": [], "cursor": 999})
        else:
            self._send({"ok": False}, code=404)


    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if self.path.startswith("/api/send"):
            sent.append(payload)
            self._send({"ok": True})
        else:
            self._send({"ok": True})

    def log_message(self, *a):
        pass


def start_hearth():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hearth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


srv = start_hearth()
port = srv.server_address[1]
server_url = f"http://127.0.0.1:{port}"

cfg = {
    "server": server_url,
    "members": {
        "hui": {"cmd": ["/bin/echo", "woken"], "timeout_sec": 30},
    },
    "prompt_template": "叫醒 {member}：{messages}",
    "summary_template": "{member} 关房 {room}",
    "min_wake_interval_sec": 0, "max_wakes_per_hour": 99,
    "blackout_spawn_after_sec": 24 * 3600,
    "blackout_report_after_sec": 600,
    "blackout_report_room": "大厅",
    "rooms": ["大厅"],
}
agents = {"hui"}

ok = True
def check(name, cond, detail=""):
    global ok
    print(f"{'✓' if cond else '✗'} {name}  {detail}")
    if not cond:
        ok = False

# ---------- 形态A：断线 ----------
srv.shutdown()
srv.server_close()
time.sleep(0.3)
now = time.time()
dirty = doorman.patrol(cfg, agents, state, now)
bo = state["blackout"]
check("A1 拉名单失败 → 断线台账起账（零成功轮）", bo is not None and bo["count"] == 1,
      f"count={bo and bo['count']}")
check("A2 失败形态分类 conn-refused", bo and bo["forms"].get("conn-refused") == 1,
      f"forms={bo and bo['forms']}")

# 第二轮失败（起账后计数增长）
doorman.patrol(cfg, agents, state, now + 15)
check("A3 连续失败计数增长", state["blackout"]["count"] == 2,
      f"count={state['blackout']['count']}")

# ---------- 形态B：超阈值点火（hearth仍死着——报警必须在断链下也能点） ----------
state["blackout"]["first_fail"] = now - 25 * 3600  # 拨回 25h 前
doorman.IN_FLIGHT.clear()
doorman.patrol(cfg, agents, state, now + 30)  # fetch_rooms 仍挂 → 零成功轮 → 超阈值 → 本地点火
for _ in range(40):
    time.sleep(0.5)
    if any("断线报警" in l for l in (tmp / "doorman.log").read_text(encoding="utf-8").splitlines()):
        break
logtxt = (tmp / "doorman.log").read_text(encoding="utf-8")
check("B1 超阈值本地点火（日志见「断线报警」）", "断线报警" in logtxt)
check("B2 报警模板落地（prompt 文件含链路报警专用词）",
      "链路报警" in (tmp / ".doorman-prompt-hui.txt").read_text(encoding="utf-8"))
check("B3 last_alerted 记账（1h 重试门）", state["blackout"]["last_alerted"] > 0)
pf = tmp / ".doorman-prompt-hui.txt"
check("B4 报警词含形态与时长", "conn-refused" in pf.read_text(encoding="utf-8") and
      ("h" in pf.read_text(encoding="utf-8")))
check("B5 点过一次不再重复点（alerted 门）", state["blackout"]["alerted"] is True)

# ---------- 形态C：自愈补投 ----------
# hearth复活：patrol 成功轮 → blackout_round_ok → 跨度>10min → POST 断线账 → 销账
srv = start_hearth()
cfg["server"] = f"http://127.0.0.1:{srv.server_address[1]}"
state["blackout"]["last_success"] = now - 3 * 3600  # 跨度 3h > 10min
doorman.patrol(cfg, agents, state, now + 60)
check("C1 断线账补投进hearth（sent 里有 sender=门房）",
      any(p.get("sender") == "门房" for p in sent), f"sent={len(sent)}")
check("C2 销账（blackout=None）", state["blackout"] is None)
check("C3 last_patrol_ok 更新", state["last_patrol_ok"] is not None)

srv.shutdown(); srv.server_close()
print()
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
