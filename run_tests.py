#!/usr/bin/env python3
"""v7.1~v7.3 隔离测试：太阳哥+zhaozhao验收用例全过一遍（打 7799 端口的测试 server）。
v7.2：state 闸（只有 pending 能 ack，终态不可复活）、cursor_epoch 双保险、
删房级联删单、迁移启动闸 /api/cursors/status、40 号 epoch-clean 回位变体。
v7.3：迁移下界（type(v) is int 拦 bool、0<=cur<=mx）+ 同事务落 migration_ready。

用法：整个目录复制到隔离沙盒（如 _v7test/），在沙盒里起
  python server.py --port 7799
再跑本脚本。绝不在生产目录跑——套件会直改 chat.db 模拟第二写者。
跑前恢复基线：cp chat.db.bak-20260831-004534 chat.db（脏库跑出来的绿红都是假的）。"""
import json, sqlite3, urllib.request, urllib.error, urllib.parse

S = "http://localhost:7799"
PASS, FAIL = [], []

def call(method, path, payload=None):
    req = urllib.request.Request(S + path, method=method)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or b"{}")

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✓' if cond else '✗'} {name}  {detail}")

# --- P0-2：ack 必须绑定真实投递 ---
# 太阳哥复现：never-issued 的 delivery_id 能把 cursor 0→999999，HTTP 200
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "mingming",
                                          "delivery_id": "never-issued", "expected_cursor": 0, "new_cursor": 999999})
check("P0-2 未签发 delivery_id 拒（404）", st == 404, f"{st} {b.get('error','')[:50]}")
st, b = call("GET", "/api/cursors?room=" + urllib.parse.quote("大厅"))
cur0 = [c for c in b["cursors"] if c["member"] == "mingming"][0]["cursor"]
check("P0-2 游标没被动过", cur0 == 171, f"cursor={cur0}")

# prepare 正常签发：大厅 max_id=171，mingming cursor=171 → upto 必须 >171，先发条消息
st, b = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.1 隔离测试消息一"})
mid1 = b["id"]
st, b = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "mingming", "upto": mid1})
check("P0-2 prepare 签发成功", st == 200 and b.get("delivery_id"), f"{st}")
did1 = b.get("delivery_id")

# ack 确认签发过的单 → 游标前进到 upto
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "mingming", "delivery_id": did1})
check("P0-2 ack 已签发单 200，游标=upto", st == 200 and b.get("cursor") == mid1, f"{st} {b}")

# 同单重复确认 → duplicate 不报错
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "mingming", "delivery_id": did1})
check("幂等重 ack → duplicate", st == 200 and b.get("duplicate"), f"{st} {b}")

# delivery 不属于这个 member → 400
st, b = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.1 隔离测试消息二"})
mid2 = b["id"]
st, b = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "zhaozhao", "upto": mid2})
did_z = b.get("delivery_id")
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "mingming", "delivery_id": did_z})
check("别人的单不能确认 → 400", st == 400, f"{st}")

# 签发即作废（v7.2 更名：原「并发/CAS」名不副实——同成员签新单时旧单当场
# superseded，先签的那张 ack 必 409，后签的 200。真正的并发撞 CAS 由
# expected 对账兜底（cursor_moved），API 层面已构造不出来）
st, b = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.1 隔离测试消息三"})
mid3 = b["id"]
_, a = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "太阳哥", "upto": mid3})
_, c = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "太阳哥", "upto": mid3})
st1, _ = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "太阳哥", "delivery_id": a["delivery_id"]})
st2, b2 = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "太阳哥", "delivery_id": c["delivery_id"]})
check("签发即作废：先签单 409（superseded），后签单 200", st1 == 409 and st2 == 200, f"{st1}/{st2}")

# prepare 边界：upto <= 当前游标 → 409；upto > 房内 max → 400；缺行 → 404
st, b = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "mingming", "upto": mid1})
check("prepare upto<=游标 → 409", st == 409, f"{st}")
st, b = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "mingming", "upto": 999999})
check("prepare upto 超房内 max → 400", st == 400, f"{st}")
st, b = call("POST", "/api/cursors/prepare", {"room": "门房狠狠审", "member": "hui", "upto": 1})
check("prepare 缺 cursor 行 → 404 recovery_required", st == 404 and b.get("recovery_required"), f"{st} {b}")

# --- P1-a：recover 校验 ---
st, b = call("POST", "/api/cursors/recover", {"room": "不存在的房", "member": "ghost", "cursor": -5, "reason": "t", "by": "测试"})
check("P1 recover ghost 房 → 404", st == 404, f"{st}")
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "ghost", "cursor": -5, "reason": "t", "by": "测试"})
check("P1 recover ghost 成员 → 400", st == 400, f"{st}")
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "mingming", "cursor": -5, "reason": "t", "by": "测试"})
check("P1 recover cursor=-5 → 400", st == 400, f"{st}")
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "mingming", "cursor": 999999, "reason": "t", "by": "测试"})
check("P1 recover cursor 越界 → 400", st == 400, f"{st}")
st, b = call("POST", "/api/cursors/recover", {"room": "门房狠狠审", "member": "hui", "cursor": "latest", "reason": "v7.1 测试 recover", "by": "测试"})
check("P1 recover 合法（缺行成员 latest）→ 200", st == 200, f"{st} {b}")
st, b = call("POST", "/api/cursors/prepare", {"room": "门房狠狠审", "member": "hui", "upto": 1})
check("recover 后缺行已补（prepare 不再 404，upto 小于现值应 409）", st == 409, f"{st}")

# --- P1-b：归档房拒发 ---
call("POST", "/api/rooms", {"room": "归档测试房", "members": ["mingming"], "created_by": "测试"})
call("POST", "/api/send", {"room": "归档测试房", "sender": "测试", "text": "归档前能发"})
st, b = call("POST", "/api/rooms/archive", {"room": "归档测试房", "archived": True})
st, b = call("POST", "/api/send", {"room": "归档测试房", "sender": "测试", "text": "归档后拒发"})
check("P1 归档房拒发 → 409", st == 409, f"{st} {b.get('error','')[:40]}")
call("POST", "/api/rooms/archive", {"room": "归档测试房", "archived": False})
st, b = call("POST", "/api/send", {"room": "归档测试房", "sender": "测试", "text": "解档后恢复"})
check("P1 解档后恢复发言 → 200", st == 200, f"{st}")

# === v7.2：state 从记号升格成闸（zhaozhao 7799 复现 + 太阳哥 D1/D2 最小反例）===
# 判据：只有 pending 能 ack；superseded/cursor_moved/recovered 全是不可复活的终态。

# --- 启动闸：迁移过的库 ready=true ---
st, b = call("GET", "/api/cursors/status")
check("v7.2 启动闸 ready（迁移库自检过）", st == 200 and b.get("ready") and not b.get("gaps"), f"{b}")

# --- D1/D2 最小反例（太阳哥）：superseded 单不需要任何铺垫自己就能复活 ---
# Zcode 游标归 0 出发：D1 prepare → D2 prepare 把 D1 作废 → ack D1 必须 409
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "Zcode", "cursor": 0,
                                              "reason": "v7.2 测试归零", "by": "测试"})
check("v7.2 前置：Zcode 游标归零", st == 200, f"{st}")
_, m1 = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 D1/D2 消息一"})
_, m2 = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 D1/D2 消息二"})
_, d1 = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "Zcode", "upto": m1["id"]})
_, d2 = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "Zcode", "upto": m2["id"]})
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "Zcode", "delivery_id": d1["delivery_id"]})
check("v7.2 D1/D2：superseded 单 ack → 409 不复活", st == 409, f"{st} {b.get('error','')[:40]}")
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "Zcode", "delivery_id": d1["delivery_id"]})
check("v7.2 终态拒绝幂等：再 ack 还是 409，不二次转移", st == 409, f"{st}")
st, b = call("GET", "/api/cursors?room=" + urllib.parse.quote("大厅"))
zc = [c for c in b["cursors"] if c["member"] == "Zcode"][0]
check("v7.2 D1/D2：游标没被盗推（仍 0）", zc["cursor"] == 0, f"cursor={zc['cursor']}")
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "Zcode", "delivery_id": d2["delivery_id"]})
check("v7.2 D1/D2：新单 D2 正常 ack → 200", st == 200 and b.get("cursor") == m2["id"], f"{st} {b}")

# --- zhaozhao 7799 复现：recover 把游标倒回去，recovered 旧单借尸还魂 ---
# shandie：prepare 一张单 → recover 归零（旧单转 recovered + epoch++）→ ack 旧单必须 409
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "shandie", "cursor": 0,
                                              "reason": "v7.2 测试归零", "by": "测试"})
_, m3 = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 复活测试消息"})
_, d3 = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "shandie", "upto": m3["id"]})
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "shandie", "cursor": 0,
                                              "reason": "v7.2 复活测试：recover 倒游标", "by": "测试"})
check("v7.2 前置：shandie recover 归零", st == 200, f"{st}")
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "shandie", "delivery_id": d3["delivery_id"]})
check("v7.2 复活测试：recovered 单 ack → 409（epoch+state 双保险）", st == 409, f"{st} {b.get('error','')[:40]}")
st, b = call("GET", "/api/cursors?room=" + urllib.parse.quote("大厅"))
sd = [c for c in b["cursors"] if c["member"] == "shandie"][0]
check("v7.2 复活测试：游标没被偷推回（仍 0），epoch 已递增", sd["cursor"] == 0 and sd["cursor_epoch"] >= 2,
      f"cursor={sd['cursor']} epoch={sd['cursor_epoch']}")

# --- cursor_moved 后游标回到 expected 仍拒（太阳哥四条反例之三）---
# API 层面造不出 cursor_moved（prepare 先作废同成员旧 pending），直接改库
# 模拟「第二个写者」把游标推走；ack 撞 cursor_moved 后游标再回到 expected，
# 旧单仍必须 409——终态不可复活，不靠 expected 碰巧又对上
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "门房", "cursor": 0,
                                              "reason": "v7.2 测试归零", "by": "测试"})
check("v7.2 前置：门房游标归零", st == 200, f"{st}")
_, mA = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 cursor_moved 消息一"})
_, mB = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 cursor_moved 消息二"})
_, dA = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "门房", "upto": mA["id"]})
check("v7.2 前置：门房 D 单签发（expected=0）", "delivery_id" in dA, f"{dA}")
conn = sqlite3.connect("chat.db")  # 绕过 API 把游标推走——第二个写者
conn.execute("UPDATE cursors SET cursor=? WHERE room='大厅' AND member='门房'", (mB["id"],))
conn.commit(); conn.close()
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "门房", "delivery_id": dA["delivery_id"]})
check("v7.2 cursor_moved：游标被推走后 ack → 409", st == 409, f"{st} {b.get('error','')[:40]}")
st, b = call("POST", "/api/cursors/recover", {"room": "大厅", "member": "门房", "cursor": 0,
                                              "reason": "v7.2 测试：游标回到 expected", "by": "测试"})
check("v7.2 前置：游标 recover 回 expected=0", st == 200, f"{st}")
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "门房", "delivery_id": dA["delivery_id"]})
check("v7.2 cursor_moved 回位仍拒：终态不复活 → 409", st == 409, f"{st} {b.get('error','')[:40]}")

# --- 40 号：epoch-clean 回位变体（zhaozhao 2026-08-31 建议）---
# 上面反例之三的回位用的是 recover——recover 顺手 bump epoch，那笔 409 记在
# state 闸还是 epoch 闸账上分不清。这里回位全程直改库、epoch 一次不动：
# 游标数值和签发时一模一样，409 只能记在 state 闸头上。
st, b = call("GET", "/api/cursors?room=" + urllib.parse.quote("大厅"))
t0 = [c for c in b["cursors"] if c["member"] == "太阳哥"][0]
_, mC = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 epoch-clean 消息一"})
_, mD = call("POST", "/api/send", {"room": "大厅", "sender": "测试", "text": "v7.2 epoch-clean 消息二"})
_, dE = call("POST", "/api/cursors/prepare", {"room": "大厅", "member": "太阳哥", "upto": mC["id"]})
check("40 前置：太阳哥 E 单签发（expected=台账现值）", "delivery_id" in dE, f"{dE}")
conn = sqlite3.connect("chat.db")  # 第二写者：推走再推回，epoch 全程不碰
conn.execute("UPDATE cursors SET cursor=? WHERE room='大厅' AND member='太阳哥'", (mD["id"],))
conn.commit()
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "太阳哥", "delivery_id": dE["delivery_id"]})
check("40 epoch-clean：游标被推走后 ack → 409", st == 409, f"{st} {b.get('error','')[:40]}")
conn.execute("UPDATE cursors SET cursor=? WHERE room='大厅' AND member='太阳哥'", (t0["cursor"],))
conn.commit(); conn.close()
st, b = call("POST", "/api/cursors/ack", {"room": "大厅", "member": "太阳哥", "delivery_id": dE["delivery_id"]})
check("40 epoch-clean 回位仍拒：终态不复活 → 409", st == 409, f"{st} {b.get('error','')[:40]}")
st, b = call("GET", "/api/cursors?room=" + urllib.parse.quote("大厅"))
t1 = [c for c in b["cursors"] if c["member"] == "太阳哥"][0]
check("40 epoch-clean：epoch 全程没动、游标回原值（拒绝只挂在 state 闸上）",
      t1["cursor_epoch"] == t0["cursor_epoch"] and t1["cursor"] == t0["cursor"],
      f"epoch {t0['cursor_epoch']}→{t1['cursor_epoch']} cursor={t1['cursor']}")
conn = sqlite3.connect("chat.db")
dE_state = conn.execute("SELECT state FROM deliveries WHERE delivery_id=?",
                        (dE["delivery_id"],)).fetchone()[0]
conn.close()
check("40 台账：E 单停在 cursor_moved（epoch-clean 回位也没复活）",
      dE_state == "cursor_moved", f"state={dE_state}")

# --- 删房级联（太阳哥复验 P1）：旧房的单随房一起死 ---
call("POST", "/api/rooms", {"room": "级联测试房", "members": ["mingming"], "created_by": "测试"})
_, m4 = call("POST", "/api/send", {"room": "级联测试房", "sender": "测试", "text": "级联消息"})
_, d4 = call("POST", "/api/cursors/prepare", {"room": "级联测试房", "member": "mingming", "upto": m4["id"]})
st, b = call("POST", "/api/rooms/delete", {"room": "级联测试房"})
check("v7.2 前置：级联测试房删除", st == 200, f"{st}")
st, b = call("POST", "/api/cursors/ack", {"room": "级联测试房", "member": "mingming", "delivery_id": d4["delivery_id"]})
check("v7.2 删房级联：旧房单 ack → 404 不复活", st == 404, f"{st} {b.get('error','')[:40]}")
# 同名重建后旧 token 仍须是死 token（太阳哥四条反例之四）
call("POST", "/api/rooms", {"room": "级联测试房", "members": ["mingming"], "created_by": "测试"})
_, m5 = call("POST", "/api/send", {"room": "级联测试房", "sender": "测试", "text": "重建后的消息"})
st, b = call("POST", "/api/cursors/ack", {"room": "级联测试房", "member": "mingming", "delivery_id": d4["delivery_id"]})
check("v7.2 删房重建：旧 token 在新房里仍 404", st == 404, f"{st} {b.get('error','')[:40]}")

# --- 台账终验：绿不是返回码说的，是库说的 ---
conn = sqlite3.connect("chat.db")
rows = {r[0]: r[1] for r in conn.execute(
    "SELECT delivery_id, state FROM deliveries WHERE member IN ('Zcode','shandie','门房')").fetchall()}
check("v7.2 台账：D1 停在 superseded（没被复活成 acked）",
      rows.get(d1["delivery_id"]) == "superseded", f"state={rows.get(d1['delivery_id'])}")
check("v7.2 台账：shandie旧单停在 recovered",
      rows.get(d3["delivery_id"]) == "recovered", f"state={rows.get(d3['delivery_id'])}")
check("v7.2 台账：门房 D 单停在 cursor_moved（回位也没复活）",
      rows.get(dA["delivery_id"]) == "cursor_moved", f"state={rows.get(dA['delivery_id'])}")
n = conn.execute("SELECT COUNT(*) FROM deliveries WHERE room='级联测试房'").fetchone()[0]
check("v7.2 台账：级联测试房的单一张不剩", n == 0, f"剩 {n} 张")
conn.close()

# === v7.3：迁移下界 + 同事务落标（太阳哥上线票最后一处）===
# isinstance 放过 bool（True 是 int 子类）、只查上界放过 -1——两条都要拦；
# 失败后台账必须干净：cursors 空、meta 无 migration_ready（status 不 ready 的事实源）
import os, subprocess, sys, tempfile, shutil
from pathlib import Path

def scratch_migration(old_cursors, rooms=("大厅",)):
    """一次性沙盒里造最小库跑迁移脚本，返回 (proc, tmp_path)。"""
    tmp = Path(tempfile.mkdtemp(prefix="mig-test-"))
    c = sqlite3.connect(tmp / "chat.db")
    c.execute("CREATE TABLE rooms(room TEXT PRIMARY KEY, members TEXT, state TEXT DEFAULT 'open')")
    c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, sender TEXT, text TEXT, ts TEXT)")
    for r in rooms:
        c.execute("INSERT INTO rooms(room,members,state) VALUES(?,'[]','open')", (r,))
        c.execute("INSERT INTO messages(room,sender,text,ts) VALUES(?,'t','x','2026-08-31 00:00:00')", (r,))
    c.commit(); c.close()
    (tmp / "doorman-state.json").write_text(json.dumps({"cursors": old_cursors}, ensure_ascii=False), encoding="utf-8")
    (tmp / "roster.json").write_text(json.dumps({"agents": ["mingming"]}, ensure_ascii=False), encoding="utf-8")
    shutil.copy(Path(__file__).parent / "migrate_cursors_v7.py", tmp / "migrate_cursors_v7.py")
    env = {**os.environ, "PYTHONUTF8": "1"}  # GBK 控制台打印 ✗ 会炸，沙盒里先防
    p = subprocess.run([sys.executable, "migrate_cursors_v7.py"], cwd=tmp, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p, tmp

def ledger_state(tmp):
    c = sqlite3.connect(tmp / "chat.db")
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # 格式校验在连库之前就拒（bool 那条），表还没建——没表就是台账空、没 ready 标
    n = c.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] if "cursors" in tables else 0
    ready = "meta" in tables and \
        c.execute("SELECT 1 FROM meta WHERE key='migration_ready'").fetchone() is not None
    c.close()
    return n, ready

# cur=-1：排在后面的房越界——前面房已插入的行必须随事务一起回滚
p, tmp = scratch_migration({"大厅": 1, "审房": -1}, rooms=("大厅", "审房"))
n, ready = ledger_state(tmp)
check("v7.3 迁移下界：cur=-1 拒迁（exit≠0）", p.returncode != 0, f"exit={p.returncode}")
check("v7.3 -1 失败后台账干净：cursors 空（前房回滚）、无 ready 标", n == 0 and not ready, f"rows={n} ready={ready}")
shutil.rmtree(tmp, ignore_errors=True)

# cur=True：bool 不是游标——isinstance 会放，type is int 必须拦
p, tmp = scratch_migration({"大厅": True})
n, ready = ledger_state(tmp)
check("v7.3 bool 拒迁：True 被拦，台账干净", p.returncode != 0 and n == 0 and not ready,
      f"exit={p.returncode} rows={n} ready={ready}")
shutil.rmtree(tmp, ignore_errors=True)

# 正路：合法值迁完，migration_ready 必须和台账同一个事务落下（不靠 server 自愈）
p, tmp = scratch_migration({"大厅": 1})
n, ready = ledger_state(tmp)
check("v7.3 正路迁移：exit=0 且同事务落 migration_ready", p.returncode == 0 and n == 1 and ready,
      f"exit={p.returncode} rows={n} ready={ready}")
shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} 过 / {len(FAIL)} 挂" + (f"  挂了的: {FAIL}" if FAIL else ""))
