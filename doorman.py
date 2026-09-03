#!/usr/bin/env python3
"""
门房 (doorman) v7 —— 客厅的门卫：有新消息就把对应 runtime 拉起来。

v7.7（zhaozhao模板轮审的三条整改，2026-09-03）：
  - 成员级叫醒模板覆盖：members.<name>.prompt_template 存在就用它，否则用全局
    prompt_template——hermes 的传输层（curl/上传/编码）wrapper 已包，叫醒词裁到五条款
  - 双轨大括号钉注释：cmd/模板里 {x} 是门房 format 时替换的；写给 agent 看的
    JSON 示例必须 {{...}} 双大括号转义，渲染后才剩单层（见 doorman.json template_note）
  - 基础设施账号（门房/仵作）只在 agents 列表，不进 members：parse_mentions 只扫
    members，@ 它们匹配不到任何叫醒目标，自然落空；members 里挂 cmd:null 反而会
    混进「没有无头入口」的巡检日志噪音。说明落在 doorman.json infra_accounts

v7（游标台账进hearth；太阳哥审 v6 两条 P0 的整改，2026-08-30）：
  - 游标从门房本地 state 文件迁进 server 的 cursors 表。state 文件丢了
    只丢节流记录和报警去重，最坏多叫几轮——不再表现为「失忆装初来乍到」
    （v6 P0：virgin 判断把 state 丢失当成平生第一次上岗，全体游标静默跳最新）
  - 「合法新成员」和「游标丢失」是两条互不能伪装的路：join 事务里落
    cursor 行并记 start_cursor（出生证明）；花名册在册却缺行 =
    recovery_required——门房只报警不猜，等管理员 recover(target,reason)
    显式重建。首次上岗不会被误判成事故，事故也不能伪装成首次上岗
  - 本地不再代推无 cmd 成员的游标（v6 会 cur[member]=upto——共享台账下
    那是替对端门房丢信）。叫醒进程在飞时巡逻不重复点火（IN_FLIGHT）
  - @ 解析改在每个 @ 处按花名册+显式别名最长匹配（v6 P0：裸子串，
    @鸣 能误中mingming），长名优先；中文连写「@mingming和@zhaozhao」两个都认得出

v7.1（太阳哥验收挡回的三条 P0 + 两条 P1 的整改，2026-08-31）：
  - P0 积压封顶漏信：改分批投——一轮只装最早 wake_max_messages 条，
    ack 只认到本次实际投递的末条；没塞进包的旧消息游标不越过，
    下轮巡逻接着叫接着送（不再「投 mine[-cap:] 却 ack 整批 upto」）
  - P0 ack 不绑真实投递：写游标改成两段式——先 POST /api/cursors/prepare
    向hearth签发投递单（expected 取台账现值、upto 限在房源消息范围内，
    持久化进 deliveries 表），归位后 ack 只报 delivery_id；
    没签发过的单确认不了任何范围（404）。静默跟进（scan）同样走签发+确认
  - P0 升级迁移断档：上线前先跑 migrate_cursors_v7.py——备份 + 单事务把
    旧 doorman-state 的每房游标迁进 cursors 表，校验 open 房全员覆盖，
    失败回滚停机报警；迁完门房才准上岗
  - P1 recover 校验：房必须存在、成员必须在花名册或该房、0<=cursor<=房内 max
  - P1 归档房拒发：门房不巡归档房，发进去会静默积压——server 直接 409

v7.2（太阳哥复验挡回的一条 P0 + 两条 P1 的整改，2026-08-31）：
  - P0 迁移启动闸：上岗前先查 GET /api/cursors/status——migration_ready
    标记没落或 open 房覆盖有缺口，直接退出，绝不写 state、绝不 pop 旧游标、
    绝不碰心跳。否则切换时计划任务先拉起新门房，巡逻报警一落盘就把旧 state
    游标抹掉，迁移脚本无源可迁。launcher（chatroom-service.ps1）同查此闸。
    切换顺序钉死：停看门人→停旧 server/doorman→备份+迁移→校验/smoke→
    启 server→启 doorman→恢复看门人
  - P1 作废单不许复活：ack 只认 state='pending' 且 epoch 对得上台账的单——
    recover 递增 cursor_epoch、删房级联删单，旧 token 从此确认不了任何范围
  - P1 语义边界：prepare+ack 证明的是「服务端签发过的范围被确认」，
    不证明 agent 真实处理过消息——信任边界在门房，别写成服务端证明投递

v7.3（太阳哥 wrapper 链路复验的三条整改，2026-09-01）：
  - stderr 非空就留账，不再绑 exit!=0——wake-taiyang.py 兜底启用成功也是
    exit=0，警告原先被 capture 后静默吞掉，生产上看不见
  - 太阳哥的 prompt 改走 --prompt-file {prompt_file}：wrapper 见到 flag 必须
    读成功（缺失/失败 exit=2 绝不启动 codex，fail-closed），读 UTF-8 文件后
    把 flag 位置换成 "-" 以 stdin 喂给 codex——大积压不再撞 Windows 命令行
    上限（撞上是永久重试）
  - wake-taiyang.py 进 git：稳定入口不进仓库，换机/checkout 就消失

v6 沿用的规矩：
  - at-least-once：叫醒进程 exit=0 才 ack 落游标；失败/超时/被节流按住
    都不 ack，消息攒着随下次叫醒重投——重投可能重复，不漏
  - 叫醒包里每条消息带 #id，agent 按 id 幂等去重
  - 积压分批投：一轮最多 wake_max_messages 条（从最早的装），ack 只认到
    实际投递末条，剩下的下轮接着送（v7.1 起不再是「取最新、旧的要自己补」）
  - 续房摘要（门房发的「本房继承自…」）视同人类发言：换房后全员最先收到摘要
  - 约定（cfg["conventions"]）随每个叫醒包投递，谁醒来都读得到家规

v4/v5 沿用的规矩：
  - members 为空的房 = 开放房：人类发言叫醒所有已配置成员
  - members 非空 = 限定房：人类发言只叫房内成员；@非成员 = 邀请，
    门房会先 POST /api/rooms/join 把人拉进房再叫醒
  - state=closing 的房：叫醒 admin 做摘要（每个房只叫一次），
    admin 交摘要后 server 自动闭旧房开续房
  - state=closed / archived：不看不叫
  - 消息文本里 @名字 -> 叫醒被 @ 的成员（agent 之间唯一的叫醒通道，
    防止 A 回 B、B 回 A 的对话死循环空烧 token）
  - 成员自己发的消息永远不叫醒自己

节流：人类发言（含续房摘要）必须送达（太阳哥定的）；节流只限制 agent 之间的 @ 唤醒
（两次叫醒间隔 min_wake_interval_sec，每小时 max_wakes_per_hour 次）。
同一成员的唤醒由锁串行化，避免两个进程同时 resume 同一个会话。

用法: python doorman.py [--config doorman.json]
户口: Chatroom-Autostart 计划任务每 5 分钟巡检，缺了会被重新拉起。
"""

import json, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
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
IN_FLIGHT = set()          # (member, room) 叫醒进程在飞——巡逻不重复点火，消息攒着落地后下轮送
IN_FLIGHT_LOCK = threading.Lock()

SUMMARY_PREFIX = "本房继承自"  # 门房代发的续房摘要，视同人类发言


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


def fetch_cursors(server, room):
    """从hearth台账拉某房的游标表：{member: row}。v7 起这是唯一事实源。"""
    q = urllib.parse.urlencode({"room": room})
    with urllib.request.urlopen(f"{server}/api/cursors?{q}", timeout=10) as r:
        return {c["member"]: c for c in json.loads(r.read().decode("utf-8"))["cursors"]}


def post_json(server, path, payload):
    req = urllib.request.Request(
        server + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def prepare_delivery(cfg, room, member, upto, kind="wake"):
    """向hearth签发一张投递单（expected 取台账现值，持久化进 deliveries 表）。
    返回 delivery_id，失败返回 None。ack 只认签发过的单——没签发的范围确认不了
    （v7.1 P0：CAS 防竞态/倒退，不证明投递）。"""
    try:
        resp = post_json(cfg["server"], "/api/cursors/prepare", {
            "room": room, "member": member, "upto": upto, "kind": kind})
        return resp.get("delivery_id")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if e.code == 404:
            log(f"⚠ recovery_required：{member} 在「{room}」缺 cursor 行，签发被拒——等管理员 recover（{body}）")
        else:
            log(f"{member} 在「{room}」签发投递单被拒（以台账为准，下轮重算）: {e.code} {body}")
    except Exception as e:
        log(f"签发投递单失败（{member}/{room} upto={upto}）: {e}——游标没动，消息随下轮重投")
    return None


def ack_cursor(cfg, room, member, delivery_id):
    """归位后向台账确认一张签发过的投递单。冲突不算事故——以台账为准，下轮重拉重算。"""
    try:
        resp = post_json(cfg["server"], "/api/cursors/ack", {
            "room": room, "member": member, "delivery_id": delivery_id})
        if resp.get("duplicate"):
            log(f"{member} 在「{room}」的投递单已确认过（幂等重 ack）")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if e.code == 404:
            log(f"⚠ {member} 在「{room}」ack 被 404 拒（投递单未知或 cursor 行缺失）——等管理员 recover（{body}）")
        else:
            log(f"{member} 在「{room}」ack 被拒（以台账为准，下轮重算）: {e.code} {body}")
    except Exception as e:
        log(f"ack 失败（{member}/{room}）: {e}——游标没动，消息随下轮重投")


def advance_cursor(cfg, room, member, upto, kind="scan"):
    """静默跟进台账（没TA的事/没有新消息）：签发+确认一步走完，和叫醒投递同一条规矩。"""
    did = prepare_delivery(cfg, room, member, upto, kind)
    if did:
        ack_cursor(cfg, room, member, did)


def alert_recovery(state, room, member, detail):
    """游标事故要喊出来，但喊过一遍就够——去重存在本地 state（文件丢了大不了重喊，
    方向是安全的：多报警，不少报警）。行恢复健康后销账，下次再丢还会再喊。"""
    key = f"{room}|{member}"
    if key in state["recovery_alerted"]:
        return False
    state["recovery_alerted"].append(key)
    log(f"⚠ recovery_required（{room} / {member}）：{detail}")
    return True


def clear_recovery(state, room, member):
    key = f"{room}|{member}"
    if key in state["recovery_alerted"]:
        state["recovery_alerted"].remove(key)
        log(f"✔ recovery 销账：{member} 在「{room}」的 cursor 行已恢复健康")
        return True
    return False


def is_summary(m):
    return m["sender"] == "门房" and m["text"].startswith(SUMMARY_PREFIX)


def parse_mentions(text, cfg):
    """@ 解析（v7 P0 整改）：在每个 @ 处按花名册 + doorman.json 显式别名做最长匹配，
    彻底禁用裸子串——「@鸣 你好」里 mingming 不完整出现，误中不了；「@mingming和@zhaozhao」
    这种中文连写两个都认得出。重叠名字长名优先（@mingming 只算mingming，不再另算鸣）。
    返回被 @ 的成员名集合（别名已折算回正名）。"""
    names = {m: m for m in cfg["members"]}
    for alias, member in (cfg.get("aliases") or {}).items():
        names[alias] = member
    by_len = sorted(names, key=len, reverse=True)
    hits, i = set(), 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        rest = text[i + 1:]
        for name in by_len:
            if rest.startswith(name):
                hits.add(names[name])
                i += 1 + len(name)
                break
        else:
            i += 1
    return hits


def build_prompt(cfg, member, room, msgs=None, closing=False, overflow=0):
    spec = cfg["members"][member]
    server = spec.get("server") or cfg["server"]
    if closing:
        prompt = cfg["summary_template"].format(member=member, room=room, server=server)
    else:
        lines = []
        # 成员可用自己的叫醒模板（members.<name>.prompt_template）覆盖全局模板——
        # 不同 runtime 的通路层能力不同（hermes 的传输已由 wrapper 包了），
        # 同一条指令只活在一处，裁剪版也只活在那一个成员的 spec 里
        tpl = spec.get("prompt_template") or cfg["prompt_template"]
        if overflow:
            # 分批投：这轮只装最早的一批，ack 只认到本包末条——剩下的游标没越过，
            # 门房下轮会接着叫接着送，不用抢救；想一口气读完也可以自己拉
            lines.append(
                f"（积压分批投递中：本包是最早的 {len(msgs)} 条，之后还有 {overflow} 条，门房下轮接着送；想一次读完自己拉：\n"
                f"  curl -s --get \"{server}/api/messages\" --data-urlencode \"room={room}\" --data-urlencode \"since=0\"）")
        for m in msgs:
            # 每条带 #id：叫醒包可能重投（at-least-once），按 id 幂等去重
            s = f"#{m['id']} {m['ts']} [{m['sender']}] {m['text']}"
            if m.get("file"):
                f = m["file"]
                s += f"\n  （附件: {f['fname']} · {f['mime']} · {f['size']}B → 下载: curl -s -o 文件名 {server}/api/files/{f['id']}）"
            lines.append(s)
        prompt = tpl.format(member=member, room=room, server=server,
                            messages="\n".join(lines))
    conv = cfg.get("conventions")
    if conv:  # 约定随包投递：谁醒来都读得到家规。{server} 手工替换，不走 format（正文里有大括号）
        prompt += "\n\n【hearth约定】\n" + conv.replace("{server}", server)
    return prompt


def wake_once(cfg, member, room, msgs=None, closing=False, overflow=0):
    """叫醒一次。返回 True = 归位（exit=0），主流程才敢 ack 落游标（at-least-once 的确认）。"""
    spec = cfg["members"][member]
    prompt = build_prompt(cfg, member, room, msgs, closing, overflow)
    # prompt 同时落一份文件：WSL/跨机入口走 {prompt_file} 免引号转义
    pf = BASE / f".doorman-prompt-{member}.txt"
    pf.write_text(prompt, encoding="utf-8")
    cmd = [s.format(prompt=prompt, prompt_file=str(pf)) for s in spec["cmd"]]
    what = "关房摘要" if closing else f"{len(msgs)} 条"
    log(f"叫醒 {member}（房间「{room}」，{what}）")
    try:
        # 被叫醒的是控制台程序（kimi.exe / wsl.exe），不加 CREATE_NO_WINDOW
        # 每次叫醒都会给tiexin弹一个黑窗——门房自己住后台，叫醒也不能扰民
        p = subprocess.run(cmd, cwd=spec.get("cwd") or str(BASE),
                           timeout=spec.get("timeout_sec", 900),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        log(f"{member} 归位，exit={p.returncode}")
        if p.stdout and p.stdout.strip():
            log(f"{member} 说: {p.stdout.strip()[:200]}")
        if p.stderr and p.stderr.strip():
            # stderr 非空就留账，不看 exit code：wrapper 兜底成功也是 exit=0，
            # 警告不能只在失败时可见（太阳哥链路复验 2026-09-01）
            log(f"{member} stderr 末尾: {p.stderr.strip()[-300:]}")
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"{member} 超时未归（>{spec.get('timeout_sec', 900)}s）")
    except Exception as e:
        log(f"{member} 叫醒失败: {e}")
    return False


def wake(cfg, member, room, delivery_id, msgs=None, closing=False, overflow=0):
    # 同一成员一次只跑一个进程。用户连续发消息时，新唤醒排队等上一轮结束，
    # 避免两个进程同时 resume 同一个会话。
    key = (member, room)
    try:
        lock = WAKE_LOCKS.setdefault(member, threading.Lock())
        with lock:
            ok = wake_once(cfg, member, room, msgs, closing, overflow)
        if ok and delivery_id:
            # at-least-once 的 ack：归位才向台账确认投递单；确认前崩溃顶多重投，不会漏
            ack_cursor(cfg, room, member, delivery_id)
    finally:
        with IN_FLIGHT_LOCK:
            IN_FLIGHT.discard(key)


def spawn_wake(member, room, upto=None, msgs=None, closing=False, overflow=0, cfg=None):
    """点火前两道闸：一，同一 (member,room) 已有进程在飞就不重复点火——
    慢叫醒（冷启动约 1 分钟）期间巡逻照常转，新消息攒着，落地 ack 后下轮一起送；
    二，要落游标的叫醒先向hearth签发投递单（ack 只认签发过的单）——
    签发放在 IN_FLIGHT 占位之后，被拒/失败不点火也不留废单。"""
    key = (member, room)
    with IN_FLIGHT_LOCK:
        if key in IN_FLIGHT:
            log(f"{member} 在「{room}」的叫醒还在飞，本轮不重复点火（消息攒着）")
            return False
        IN_FLIGHT.add(key)
    delivery_id = None
    if upto is not None:
        delivery_id = prepare_delivery(cfg, room, member, upto)
        if not delivery_id:
            with IN_FLIGHT_LOCK:
                IN_FLIGHT.discard(key)
            return False
    threading.Thread(target=wake, kwargs={"cfg": cfg, "member": member, "room": room,
                                          "delivery_id": delivery_id, "msgs": msgs,
                                          "closing": closing, "overflow": overflow},
                     daemon=True, name=f"wake-{member}").start()
    return True


def due(state, member, cfg, now):
    last = state["last_wake"].get(member, 0)
    if now - last < cfg["min_wake_interval_sec"]:
        return False
    wakes = [t for t in state["wake_log"].get(member, []) if now - t < 3600]
    state["wake_log"][member] = wakes
    return len(wakes) < cfg["max_wakes_per_hour"]


def patrol(cfg, agents, state, now):
    """巡一轮所有房。返回是否有状态变更。任何单个房间的错都不该炸掉整轮。
    游标台账在 server 端：门房本地 state 只剩节流记录和报警去重，
    state 文件丢失的最坏结果是多叫几轮——永远不会表现为「失忆装初来乍到」。"""
    dirty = False
    try:
        rooms = fetch_rooms(cfg["server"])
    except Exception as e:
        log(f"拉房间列表失败: {e}")
        return False
    for r in rooms:
        room, members, rstate = r["room"], r["members"], r["state"]
        if rstate == "closed" or r.get("archived"):
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
                spawn_wake(admin, room, closing=True, cfg=cfg)
            else:
                state["closing_notified"].append(room)
                dirty = True
                log(f"「{room}」进入关闭流程，但管理员 {admin} 没有无头入口，挂了账")
            continue
        # open 房：从hearth台账拉游标
        try:
            cmap = fetch_cursors(cfg["server"], room)
        except Exception as e:
            log(f"拉「{room}」游标台账失败: {e}")
            continue
        if not cmap:
            # 新房创建时必落行（open_ledger）；整房无行 = 台账丢失，报警不猜
            dirty |= alert_recovery(state, room, "*",
                                    "整房没有任何 cursor 行——按台账丢失处理，本房跳过，等管理员 recover")
            continue
        # 只为自己服务的成员（cmd 非空）拉窗：无 cmd 成员由对端门房自理，
        # 本地不扫他们的窗、更不动他们的行（v6 代推游标 = 替别人丢信）
        served = [m for m, s in cfg["members"].items() if s.get("cmd")]
        if not served:
            continue
        rows = {m: cmap.get(m) for m in served}
        # 缺行报警不依赖有没有新消息——事故不该等有流量才被发现
        for m, row in rows.items():
            if row is None:
                # 限定房的非成员本来就没行（被 @ 邀请时 join 事务才落行）——
                # 那不是事故，跳过；下面拿到消息后会走邀请流程
                if members and m not in members:
                    continue
                # 花名册在册（或就在房里）却缺 cursor 行 = 游标丢失。不许静默跳最新——
                # 「合法新成员」在 join/建房事务里落过行，缺行只能是事故。
                dirty |= alert_recovery(state, room, m,
                                        "在册成员缺 cursor 行——按游标丢失处理，不送，等管理员 recover")
            else:
                dirty |= clear_recovery(state, room, m)
        ok_rows = [r for r in rows.values() if r]
        if not ok_rows:
            continue  # 全员缺行：上面已逐成员报警，本轮不送
        since = min(r["cursor"] for r in ok_rows)
        try:
            d = fetch_messages(cfg["server"], room, since)
        except Exception as e:
            log(f"巡「{room}」失败: {e}")
            continue
        msgs = d["messages"]
        if not msgs:
            continue
        upto = d["cursor"]
        log(f"「{room}」来了 {len(msgs)} 条新消息（{', '.join(m['sender'] for m in msgs)}）")
        # @ 解析一次扫全窗：精确边界匹配（@鸣 误中不了mingming），别名走显式映射
        mentioned_all = set()
        for m in msgs:
            mentioned_all |= parse_mentions(m["text"], cfg)
        # 限定房的 @邀请先行：join 事务落 cursor 行（出生证明），再进正常流程——
        # 缺行的邀请不报警，join 就是它的合法起点
        if members:
            for member in [m for m in served if m not in members and m in mentioned_all]:
                try:
                    post_json(cfg["server"], "/api/rooms/join", {"room": room, "member": member})
                    log(f"{member} 被 @ 邀请进「{room}」")
                    members.append(member)
                    cmap = fetch_cursors(cfg["server"], room)  # 重拉台账，拿到新落的行
                    rows = {m: cmap.get(m) for m in served}
                except Exception as e:
                    log(f"拉 {member} 进「{room}」失败: {e}")
        # 可观测性：该叫但叫不动的成员，留一行账（hui在hearth缺席就是这条线上的静默）
        human_any = any(m["sender"] not in agents for m in msgs)
        missing = [m for m, s in cfg["members"].items()
                   if not s.get("cmd") and (not members or m in members)]
        if human_any and missing:
            log(f"（{room}）{'、'.join(missing)} 没有无头入口，人类发言送不到他们")
        for member in served:
            spec = cfg["members"][member]
            row = rows[member]
            if not row:
                continue  # 缺行：上面已报警（或邀请失败已记账），不送不猜
            my_cur = row["cursor"]
            mine = [m for m in msgs if m["id"] > my_cur and m["sender"] != member]
            if not mine:
                if my_cur < upto:
                    advance_cursor(cfg, room, member, upto)
                continue
            mentioned = member in mentioned_all
            human_spoke = any(m["sender"] not in agents or is_summary(m) for m in mine)
            if not (human_spoke or mentioned):
                if my_cur < upto:
                    advance_cursor(cfg, room, member, upto)
                continue  # 没TA的事：静默跟进台账，不叫
            # 人类发言（含续房摘要）必须送达；节流只限制 agent 之间的 @ 唤醒。
            # 被按住的唤醒不 ack——消息不丢，攒着随下次叫醒一起送达。
            if not human_spoke and not due(state, member, cfg, now):
                log(f"{member} 该叫但被节流按住（间隔/时额），消息攒着下轮再送")
                continue
            state["last_wake"][member] = now
            state["wake_log"].setdefault(member, []).append(now)
            dirty = True
            cap = cfg.get("wake_max_messages", 40)
            # 分批投（v7.1 P0）：一轮只装最早 cap 条，upto 只到本次实际投递的末条——
            # 没塞进包里的消息游标绝不越过（旧版投 mine[-cap:] 却 ack 整批 upto = 永久漏信），
            # 剩下的下轮巡逻游标没到位会再叫，接着送
            deliver, overflow = (mine[:cap], len(mine) - cap) if len(mine) > cap else (mine, 0)
            # 并行叫醒：一个成员慢（冷启动约 1 分钟）不挡别人的门铃
            spawn_wake(member, room, upto=deliver[-1]["id"], msgs=deliver,
                       overflow=overflow, cfg=cfg)
    return dirty


def migration_gate(cfg):
    """v7.2 P0 迁移启动闸：上岗前先问hearth /api/cursors/status。
    migration_ready 标记没落 或 open 房覆盖有缺口 → 拒上岗，返回 False。
    拒的姿势钉死：绝不写 state、绝不 pop 旧游标、绝不碰心跳——切换窗口里
    计划任务会反复拉起门房，每一次误上岗都可能把迁移脚本要迁的源数据抹掉。"""
    try:
        with urllib.request.urlopen(f"{cfg['server']}/api/cursors/status", timeout=10) as r:
            st = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"⛔ 启动闸查询失败（server 没起或不通）: {e}——拒上岗，等看门人下轮再试")
        return False
    if st.get("ready"):
        return True
    log(f"⛔ 迁移启动闸未过（migration_ready={st.get('migration_ready')}，"
        f"覆盖缺口 {len(st.get('gaps') or [])} 个: {(st.get('gaps') or [])[:5]}）"
        f"——拒上岗，先跑 migrate_cursors_v7.py")
    return False


def main():
    cfg = load_cfg()
    agents = set(cfg["agents"])
    if not migration_gate(cfg):
        sys.exit(1)  # 闸不过：一行 state 不写、一下心跳不碰，原样退出
    # v7 起 state 只剩：节流记录（last_wake/wake_log）、关房通知去重、报警去重。
    # 游标在hearth台账（server 端 cursors 表）——这个文件整个丢了也不丢信。
    state = {"last_wake": {}, "wake_log": {}, "closing_notified": [], "recovery_alerted": []}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    for k in ("last_wake", "wake_log", "closing_notified", "recovery_alerted"):
        state.setdefault(k, {} if k in ("last_wake", "wake_log") else [])
    state.pop("cursors", None)  # v6 遗存：游标已由 migrate_cursors_v7.py 迁进hearth台账，本地这份从此作废

    log(f"门房 v7.7 上岗（游标台账在hearth，写游标 = prepare 签发投递单 + ack 确认单），房间动态从 server 拉取，每 {cfg['poll_seconds']}s 巡一次")
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
