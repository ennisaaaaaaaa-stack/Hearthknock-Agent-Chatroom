#!/usr/bin/env python3
"""doorman v7.11 断线三件套的冒烟测试（送审随包——对侧复跑的靶子）。

覆盖：
  ① 失败形态分类（timeout/conn-refused/conn-reset/dns/http-<code>，.reason 两层看）
  ② 整轮零成功起账/计数（first_fail 不动、last_error 截断 200）
  ③ 超阈值本地模板点火（专用报警模板、spawn 不签单、点不进 1h 重试、无可点火成员只留日志）
  ④ 自愈补投断线账（跨度按距上次成功巡逻、POST 成功才销账、200 轮弃账、partial 形态并入）
  ⑤ flapping 留痕（断 23h59+成功 1 次+再断：报警没凑满，断线账照留、报警计时重置）
  ⑥ patrol 集成（拉名单挂/全房拉失败=零成功轮，部分成功=成功轮，无该巡的房=成功轮）

跑法：python test_doorman_blackout.py
纯 monkeypatch——不碰网络、不碰 doorman-state.json、不碰生产 doorman.log。
"""
import socket
import sys
import tempfile
import urllib.error
from pathlib import Path

import doorman

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}  {detail}")


# log 别写生产 doorman.log
doorman.LOG = Path(tempfile.mkdtemp(prefix="doorman-blackout-test-")) / "doorman.log"

T0 = 1_700_000_000.0


def fresh_state():
    return {"last_wake": {}, "wake_log": {}, "closing_notified": [],
            "recovery_alerted": [], "l3_alerted": {}, "blackout": None,
            "last_patrol_ok": None}


def fresh_cfg(**over):
    cfg = {"server": "http://testserver",
           "members": {"mingming": {"cmd": ["true"]}},
           "rooms": ["大厅"],
           "agents": ["mingming", "hui", "zhaozhao"],
           "min_wake_interval_sec": 120, "max_wakes_per_hour": 12,
           "wake_max_messages": 40,
           "blackout_spawn_after_sec": 100,
           "blackout_report_after_sec": 50,
           "blackout_report_room": "大厅"}
    cfg.update(over)
    return cfg


class SpawnSpy:
    def __init__(self, ret=True):
        self.calls, self.ret = [], ret

    def __call__(self, member, room, upto=None, msgs=None, closing=False,
                 overflow=0, cfg=None, state=None, blackout=None):
        self.calls.append({"member": member, "room": room, "upto": upto,
                           "blackout": blackout})
        return self.ret


class PostSpy:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, server, path, payload):
        if self.fail:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        self.calls.append({"path": path, "payload": payload})
        return {}


_orig = {k: getattr(doorman, k) for k in
         ("fetch_rooms", "fetch_cursors", "fetch_messages", "spawn_wake", "post_json")}

# ---------- ① 失败形态分类 ----------
ue = urllib.error.URLError
check("classify：HTTPError → http-<code>",
      doorman.classify_failure(urllib.error.HTTPError("u", 500, "m", {}, None)) == "http-500")
check("classify：URLError(reason=TimeoutError) → timeout",
      doorman.classify_failure(ue(TimeoutError("t"))) == "timeout")
check("classify：URLError(reason=ConnectionRefusedError) → conn-refused",
      doorman.classify_failure(ue(ConnectionRefusedError("r"))) == "conn-refused")
check("classify：URLError(reason=ConnectionResetError) → conn-reset",
      doorman.classify_failure(ue(ConnectionResetError("r"))) == "conn-reset")
check("classify：URLError(reason=gaierror) → dns",
      doorman.classify_failure(ue(socket.gaierror("name or service not known"))) == "dns")
check("classify：裸 TimeoutError（本体层也看）→ timeout",
      doorman.classify_failure(TimeoutError("t")) == "timeout")
check("classify：未知异常 → 类型名兜底",
      doorman.classify_failure(ValueError("x")) == "ValueError")

# ---------- fmt_dur ----------
check("fmt_dur：45 → 45s", doorman.fmt_dur(45) == "45s")
check("fmt_dur：73 → 1m13s", doorman.fmt_dur(73) == "1m13s")
check("fmt_dur：303 → 5m3s", doorman.fmt_dur(303) == "5m3s")
check("fmt_dur：94440 → 26h14m", doorman.fmt_dur(94440) == "26h14m")

# ---------- ② 起账 / 计数 ----------
st = fresh_state()
st["last_patrol_ok"] = T0 - 30
doorman.blackout_round_failed(fresh_cfg(), st, ["timeout"], TimeoutError("t"), T0, False)
bo = st["blackout"]
check("起账：首次零成功轮建台账（count=1）", bo is not None and bo["count"] == 1)
check("起账：first_fail=now、last_success 取 last_patrol_ok",
      bo["first_fail"] == T0 and bo["last_success"] == T0 - 30)
check("起账：形态起记 timeout×1", bo["forms"] == {"timeout": 1})

st2 = fresh_state()  # last_patrol_ok 缺失
doorman.blackout_round_failed(fresh_cfg(), st2, ["dns"], socket.gaierror("g"), T0, False)
check("起账：last_patrol_ok 缺失 → last_success=now", st2["blackout"]["last_success"] == T0)

doorman.blackout_round_failed(fresh_cfg(), st, ["timeout", "dns"], socket.gaierror("g"), T0 + 60, False)
check("计数：count 累加、forms 聚合、first_fail 不动",
      bo["count"] == 2 and bo["forms"] == {"timeout": 2, "dns": 1} and bo["first_fail"] == T0)

doorman.blackout_round_failed(fresh_cfg(), st, ["timeout"], "x" * 500, T0 + 61, False)
check("last_error 截断 200", len(bo["last_error"]) == 200)

# ---------- ③ 点火 ----------
st = fresh_state()
st["last_patrol_ok"] = T0 - 10
spy = SpawnSpy()
doorman.spawn_wake = spy
cfg = fresh_cfg()
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0, False)
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 50, False)
check("点火：未达阈值（50s<100s）不点火", spy.calls == [])

doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 120, False)
check("点火：达阈值 spawn 恰好一次", len(spy.calls) == 1)
c = spy.calls[0] if spy.calls else {}
info = c.get("blackout") or {}
check("点火：不签投递单（upto=None）、报警对象=首个有 cmd 成员、房=blackout_report_room",
      c.get("upto") is None and c.get("member") == "mingming" and c.get("room") == "大厅")
check("点火：blackout info 五字段齐（dur/span/count/forms/last_error）",
      all(k in info for k in ("dur", "span", "count", "forms", "last_error")), str(info))
check("点火：成功后 alerted=True 不再重复点",
      st["blackout"]["alerted"] is True)
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 130, False)
check("点火：alerted 后第二轮仍只点过一次", len(spy.calls) == 1)

# 模板渲染：build_prompt 走专用报警模板
prompt = doorman.build_prompt(cfg, "mingming", "大厅", blackout=dict(
    dur="26h14m", span="26h14m", count=3, forms="timeout×3", last_error="x"))
check("模板：专用报警模板（明说不是新消息、不要去拉消息）",
      "这不是新消息叫醒" in prompt and "不要去拉消息" in prompt and "26h14m" in prompt)
cfg2 = fresh_cfg(blackout_template="ALARM {member} {dur}")
prompt2 = doorman.build_prompt(cfg2, "mingming", "大厅", blackout=dict(
    dur="26h14m", span="s", count=1, forms="f", last_error="e"))
check("模板：cfg blackout_template 覆盖生效", prompt2 == "ALARM mingming 26h14m")

# 点不进（成员在飞 spawn→False）：alerted 保持 False，1h 内不重试，过 1h 重试
st = fresh_state()
st["last_patrol_ok"] = T0 - 10
spy = SpawnSpy(ret=False)
doorman.spawn_wake = spy
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0, False)  # 起账
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 200, False)
check("重试：点不进 alerted=False", st["blackout"]["alerted"] is False and len(spy.calls) == 1)
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 200 + 600, False)
check("重试：点不进 1h 内不重试", len(spy.calls) == 1)
doorman.blackout_round_failed(cfg, st, ["timeout"], TimeoutError("t"), T0 + 200 + 3700, False)
check("重试：过 1h 重试一次", len(spy.calls) == 2)

# 本侧没有可点火的成员：不炸、alerted=True（只留日志账）
st = fresh_state()
spy = SpawnSpy()
doorman.spawn_wake = spy
cfg_none = fresh_cfg(members={"hui": {}})  # 无 cmd、未配 blackout_notify_member
doorman.blackout_round_failed(cfg_none, st, ["timeout"], TimeoutError("t"), T0, False)  # 起账
doorman.blackout_round_failed(cfg_none, st, ["timeout"], TimeoutError("t"), T0 + 200, False)
check("点火：无可点火成员 → 不炸、不 spawn、alerted=True（只留日志账）",
      spy.calls == [] and st["blackout"]["alerted"] is True)
doorman.spawn_wake = _orig["spawn_wake"]

# ---------- ④ 自愈补投 / 销账 ----------
st = fresh_state()
post = PostSpy()
doorman.post_json = post
doorman.blackout_round_ok(fresh_cfg(), st, [], T0, False)
check("自愈：无断线账的成功轮只更新 last_patrol_ok、不 POST",
      st["last_patrol_ok"] == T0 and post.calls == [])

st = fresh_state()  # 短抖动：span 31s < 阈值 50s
st["last_patrol_ok"] = T0
doorman.blackout_round_failed(fresh_cfg(), st, ["timeout"], TimeoutError("t"), T0 + 1, False)
doorman.blackout_round_ok(fresh_cfg(), st, [], T0 + 31, False)
check("自愈：短于补投阈值的抖动销账不 POST", post.calls == [] and st["blackout"] is None)

st = fresh_state()  # 长断线：跨度 3700s ≥ 阈值，partial 形态并入
post.calls.clear()
st["last_patrol_ok"] = T0
doorman.blackout_round_failed(fresh_cfg(), st, ["timeout"], TimeoutError("t"), T0 + 10, False)
doorman.blackout_round_ok(fresh_cfg(), st, ["dns"], T0 + 3700, False)
check("自愈：长断线补投恰好 POST 一次到 /api/send", len(post.calls) == 1 and post.calls[0]["path"] == "/api/send")
p = post.calls[0]["payload"] if post.calls else {}
check("自愈：断线账 sender=门房、进 blackout_report_room、含跨度与形态（partial 的 dns 并入）",
      p.get("sender") == "门房" and p.get("room") == "大厅"
      and "断线账" in p.get("text", "") and "1h1m" in p.get("text", "")
      and "timeout×1" in p.get("text", "") and "dns×1" in p.get("text", ""),
      (p.get("text") or "")[:120])
check("自愈：POST 成功才销账（blackout=None）", st["blackout"] is None)

# 补投失败：不清账、ledger_attempts 累加、下轮成功才销账
st = fresh_state()
post.calls.clear()
st["blackout"] = {"last_success": T0, "first_fail": T0, "count": 5,
                  "forms": {"timeout": 5}, "last_error": "e", "alerted": False,
                  "last_alerted": 0, "ledger_attempts": 0}
post.fail = True
doorman.blackout_round_ok(fresh_cfg(), st, [], T0 + 3700, False)
check("补投失败：不清账、ledger_attempts=1",
      st["blackout"] is not None and st["blackout"]["ledger_attempts"] == 1)
post.fail = False
doorman.blackout_round_ok(fresh_cfg(), st, [], T0 + 3800, False)
check("补投失败：下轮成功才销账", st["blackout"] is None and len(post.calls) == 1)

# 补投连失败 200 轮弃账（report room 指去已关的房时防无限重试）
st = fresh_state()
st["blackout"] = {"last_success": T0, "first_fail": T0, "count": 5,
                  "forms": {"timeout": 5}, "last_error": "e", "alerted": False,
                  "last_alerted": 0, "ledger_attempts": 199}
post.calls.clear()
post.fail = True
doorman.blackout_round_ok(fresh_cfg(), st, [], T0 + 3700, False)
check("补投连失败 200 轮弃账（blackout=None）", st["blackout"] is None)
post.fail = False
doorman.post_json = _orig["post_json"]

# ---------- ⑤ flapping：报警凑不满 24h，断线账照留、报警计时重置 ----------
st = fresh_state()
st["last_patrol_ok"] = T0
spy = SpawnSpy()
post = PostSpy()
doorman.spawn_wake, doorman.post_json = spy, post
cfg_flap = fresh_cfg(blackout_spawn_after_sec=86400, blackout_report_after_sec=600)
doorman.blackout_round_failed(cfg_flap, st, ["timeout"], TimeoutError("t"), T0 + 1, False)
doorman.blackout_round_failed(cfg_flap, st, ["timeout"], TimeoutError("t"), T0 + 40000, False)
check("flapping：断 11h 未达 24h 阈值，报警未发", spy.calls == [])
doorman.blackout_round_ok(cfg_flap, st, [], T0 + 86399, False)  # 断 23h59 后成功 1 次
check("flapping：恢复按「距上次成功巡逻」的跨度补投一笔账（报警没发也留痕）",
      len(post.calls) == 1 and "23h59m" in post.calls[0]["payload"]["text"])
doorman.blackout_round_failed(cfg_flap, st, ["timeout"], TimeoutError("t"), T0 + 86410, False)
check("flapping：再断报警计时重置（first_fail=新一轮）",
      st["blackout"] is not None and st["blackout"]["first_fail"] == T0 + 86410
      and st["blackout"]["last_success"] == T0 + 86399)
doorman.spawn_wake, doorman.post_json = _orig["spawn_wake"], _orig["post_json"]

# ---------- ⑥ patrol 集成 ----------
# 拉名单挂 → 零成功轮进断线账
st = fresh_state()
doorman.fetch_rooms = lambda s: (_ for _ in ()).throw(ue(TimeoutError("t")))
doorman.patrol(fresh_cfg(), set(), st, T0)
check("patrol：fetch_rooms 挂 → 零成功轮进断线账",
      st["blackout"] is not None and st["blackout"]["count"] == 1
      and st["blackout"]["forms"] == {"timeout": 1})

# 本侧该巡的房全拉失败 → 零成功轮进账
st = fresh_state()
doorman.fetch_rooms = lambda s: [{"room": "大厅", "members": None, "state": "open"}]
doorman.fetch_cursors = lambda s, r: (_ for _ in ()).throw(ue(TimeoutError("t")))
doorman.patrol(fresh_cfg(), set(), st, T0)
check("patrol：该巡房全拉失败 → 零成功轮进账（fetch_rooms 绿着也算）",
      st["blackout"] is not None and st["blackout"]["forms"] == {"timeout": 1})

# 部分成功 → 成功轮：last_patrol_ok 更新、不进断线账
st = fresh_state()
doorman.fetch_rooms = lambda s: [{"room": "大厅", "members": None, "state": "open"},
                                 {"room": "账房", "members": None, "state": "open"}]
def _cursors(s, r):
    if r == "账房":
        raise ue(TimeoutError("t"))
    return {"mingming": {"member": "mingming", "cursor": 3}}
doorman.fetch_cursors = _cursors
doorman.fetch_messages = lambda s, r, since: {"messages": [], "cursor": 3}
doorman.patrol(fresh_cfg(), set(), st, T0)
check("patrol：部分成功 = 成功轮（last_patrol_ok 更新、blackout 不起账）",
      st["last_patrol_ok"] == T0 and st["blackout"] is None)

# 无该巡的房（本侧成员全无 cmd）→ 成功轮
st = fresh_state()
doorman.fetch_rooms = lambda s: [{"room": "大厅", "members": None, "state": "open"}]
doorman.fetch_cursors = lambda s, r: {"hui": {"member": "hui", "cursor": 1}}
doorman.patrol(fresh_cfg(members={"hui": {}}), set(), st, T0)
check("patrol：无该巡的房 = hearth通着但没活干，算成功轮",
      st["last_patrol_ok"] == T0 and st["blackout"] is None)

for k, v in _orig.items():
    setattr(doorman, k, v)

print(f"\n{len(PASS)} 绿 / {len(FAIL)} 红")
if FAIL:
    print("红的:", *FAIL, sep="\n  ")
    sys.exit(1)
