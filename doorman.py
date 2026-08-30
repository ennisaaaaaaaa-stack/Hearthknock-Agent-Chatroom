#!/usr/bin/env python3
"""
门房 (doorman) v4 —— 客厅的门卫：有新消息就把对应 runtime 拉起来。

v4 起房间不再写在配置里，每次巡逻从 server 拉 /api/rooms：
  - members 为空的房 = 开放房：人类发言叫醒所有已配置成员
  - members 非空 = 限定房：人类发言只叫房内成员；@非成员 = 邀请，
    门房会先 POST /api/rooms/join 把人拉进房再叫醒
  - state=closing 的房：叫醒 admin 做摘要（每个房只叫一次），
    admin 交摘要后 server 自动闭旧房开续房
  - state=closed：不看不叫
  - 消息文本里 @名字 -> 叫醒被 @ 的成员（agent 之间唯一的叫醒通道，
    防止 A 回 B、B 回 A 的对话死循环空烧 token）
  - 成员自己发的消息永远不叫醒自己

节流：人类发言必须送达（太阳哥定的）；节流只限制 agent 之间的 @ 唤醒
（两次叫醒间隔 min_wake_interval_sec，每小时 max_wakes_per_hour 次）。
同一成员的唤醒由锁串行化，避免两个进程同时 resume 同一个会话。

用法: python doorman.py [--config doorman.json]
户口: Chatroom-Autostart 计划任务每 5 分钟巡检，缺了会被重新拉起。
"""

import json, subprocess, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

# GBK 控制台打印非 GBK 字符会直接抛 UnicodeEncodeError（家里第四个换马甲的同一个坑）。
# 日志写文件是 utf-8 没问题，console print 需要 errors=replace 兜底。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).parent
LOG = BASE / "doorman.log"
STATE_FILE = BASE / "doorman-state.json"
HB = BASE / "doorman.heartbeat"
WAKE_LOCKS = {}


def load_cfg():
    global LOG, STATE_FILE, HB
    path = BASE / "doorman.json"
    if "--config" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--config") + 1]).resolve()
        LOG = path.parent / "doorman.log"
        STATE_FILE = path.parent / "doorman-state.json"
        HB = path.parent / "doorman.heartbeat"
    return json.loads(path.read_text(encoding="utf-8"))


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw 下没有 stdout，文件账照常记
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_messages(server, room, since):
    q = urllib.parse.urlencode({"room": room, "since": since})
    with urllib.request.urlopen(f"{server}/api/messages?{q}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_rooms(server):
    with urllib.request.urlopen(f"{server}/api/rooms", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))["rooms"]


def post_json(server, path, payload):
    req = urllib.request.Request(
        server + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def build_prompt(cfg, member, room, msgs=None, closing=False):
    spec = cfg["members"][member]
    server = spec.get("server") or cfg["server"]
    if closing:
        return cfg["summary_template"].format(member=member, room=room, server=server)
    lines = []
    for m in msgs:
        s = f"{m['ts']} [{m['sender']}] {m['text']}"
        if m.get("file"):
            f = m["file"]
            s += f"\n  （附件: {f['fname']} · {f['mime']} · {f['size']}B → 下载: curl -s -o 文件名 {server}/api/files/{f['id']}）"
        lines.append(s)
    return cfg["prompt_template"].format(member=member, room=room, server=server, messages="\n".join(lines))


def wake_once(cfg, member, room, msgs=None, closing=False):
    spec = cfg["members"][member]
    prompt = build_prompt(cfg, member, room, msgs, closing)
    # prompt 同时落一份文件：WSL/跨机入口走 {prompt_file} 免引号转义
    pf = BASE / f".doorman-prompt-{member}.txt"
    pf.write_text(prompt, encoding="utf-8")
    cmd = [s.format(prompt=prompt, prompt_file=str(pf)) for s in spec["cmd"]]
    what = "关房摘要" if closing else f"{len(msgs)} 条"
    log(f"叫醒 {member}（房间「{room}」，{what}）")
    try:
        p = subprocess.run(cmd, cwd=spec.get("cwd") or str(BASE),
                           timeout=spec.get("timeout_sec", 900),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        log(f"{member} 归位，exit={p.returncode}")
        if p.stdout and p.stdout.strip():
            log(f"{member} 说: {p.stdout.strip()[:200]}")
        if p.returncode != 0:
            log(f"{member} stderr 末尾: {p.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        log(f"{member} 超时未归（>{spec.get('timeout_sec', 900)}s）")
    except Exception as e:
        log(f"{member} 叫醒失败: {e}")


def wake(cfg, member, room, msgs=None, closing=False):
    # 同一成员一次只跑一个进程。用户连续发消息时，新唤醒排队等上一轮结束，
    # 避免两个进程同时 resume 同一个会话。
    lock = WAKE_LOCKS.setdefault(member, threading.Lock())
    with lock:
        wake_once(cfg, member, room, msgs, closing)


def due(state, member, cfg, now):
    last = state["last_wake"].get(member, 0)
    if now - last < cfg["min_wake_interval_sec"]:
        return False
    wakes = [t for t in state["wake_log"].get(member, []) if now - t < 3600]
    state["wake_log"][member] = wakes
    return len(wakes) < cfg["max_wakes_per_hour"]


def patrol(cfg, agents, state, now):
    """巡一轮所有房。返回是否有状态变更。任何单个房间的错都不该炸掉整轮。"""
    dirty = False
    try:
        rooms = fetch_rooms(cfg["server"])
    except Exception as e:
        log(f"拉房间列表失败: {e}")
        return False
    for r in rooms:
        room, members, rstate = r["room"], r["members"], r["state"]
        if rstate == "closed":
            continue
        if rstate == "closing":
            # 每个房只通知一次；admin 没配无头入口就记账等人来救
            if room in state["closing_notified"]:
                continue
            admin = r["admin"] or "mingming"
            spec = cfg["members"].get(admin)
            if spec and spec.get("cmd"):
                state["closing_notified"].append(room)
                dirty = True
                log(f"「{room}」进入关闭流程，通知管理员 {admin}")
                threading.Thread(target=wake, args=(cfg, admin, room),
                                 kwargs={"closing": True},
                                 daemon=True, name=f"close-{room}").start()
            else:
                state["closing_notified"].append(room)
                dirty = True
                log(f"「{room}」进入关闭流程，但管理员 {admin} 没有无头入口，挂了账")
            continue
        # open 房：扫新消息
        if room not in state["cursors"]:
            d = fetch_messages(cfg["server"], room, 0)
            state["cursors"][room] = d["cursor"]
            dirty = True
            log(f"房间「{room}」游标初始化到 {d['cursor']}（积压 {len(d['messages'])} 条不追）")
            continue
        try:
            d = fetch_messages(cfg["server"], room, state["cursors"][room])
        except Exception as e:
            log(f"巡「{room}」失败: {e}")
            continue
        msgs = d["messages"]
        if not msgs:
            continue
        state["cursors"][room] = d["cursor"]
        dirty = True
        log(f"「{room}」来了 {len(msgs)} 条新消息（{', '.join(m['sender'] for m in msgs)}）")
        for member, spec in cfg["members"].items():
            if not spec.get("cmd"):
                continue  # 还没配上无头入口的成员
            mine = [m for m in msgs if m["sender"] != member]
            if not mine:
                continue
            mentioned = any(f"@{member}" in m["text"] for m in mine)
            if members and member not in members:
                # 限定房：非成员不旁听；被 @ = 邀请，拉进房再叫醒
                if not mentioned:
                    continue
                try:
                    post_json(cfg["server"], "/api/rooms/join", {"room": room, "member": member})
                    log(f"{member} 被 @ 邀请进「{room}」")
                except Exception as e:
                    log(f"拉 {member} 进「{room}」失败: {e}")
            human_spoke = any(m["sender"] not in agents for m in mine)
            if not (human_spoke or mentioned):
                continue
            # 人类发言必须送达；节流只限制 agent 之间的 @ 唤醒。
            # 同一成员的并发由 wake() 里的锁串行化。
            if not human_spoke and not due(state, member, cfg, now):
                log(f"{member} 该叫但被节流按住（间隔/时额）")
                continue
            state["last_wake"][member] = now
            state["wake_log"].setdefault(member, []).append(now)
            # 并行叫醒：一个成员慢（冷启动约 1 分钟）不挡别人的门铃
            threading.Thread(target=wake, args=(cfg, member, room, mine),
                             daemon=True, name=f"wake-{member}").start()
    return dirty


def main():
    cfg = load_cfg()
    agents = set(cfg["agents"])
    state = {"cursors": {}, "last_wake": {}, "wake_log": {}, "closing_notified": []}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    state.setdefault("closing_notified", [])

    log(f"门房 v5 上岗（带心跳），房间动态从 server 拉取，每 {cfg['poll_seconds']}s 巡一次")
    while True:
        now = time.time()
        # 心跳是看门人的命：进程活着但循环死了（僵尸）这件事已经发生过。
        # chatroom-service.ps1 只认这个文件的新鲜度，不认进程存在。
        try:
            HB.write_text(str(now), encoding="utf-8")
        except Exception:
            pass
        try:
            dirty = patrol(cfg, agents, state, now)
        except Exception as e:
            log(f"这一轮巡逻整个炸了（已兜住，下轮继续）: {e}")
            dirty = True  # 至少把 state 落盘窗口保住
        if dirty:
            try:
                STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                log(f"state 落盘失败: {e}")
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
