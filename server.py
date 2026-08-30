#!/usr/bin/env python3
"""
客厅 v2 —— 家庭群聊板（零依赖，stdlib only）

  python server.py [--port 7788]

房间模型：
  - rooms 表：members（空=开放房，全员可入铃）、state（open/closing/closed）、
    admin（关房时负责摘要的成员）、parent（继承自哪个旧房）、summary
  - 发言把房间的估算 token 推过 CLOSE_THRESHOLD 时，房间进入 closing：
    冻结发言，等 admin 交摘要（POST /api/rooms/summary），交房即 closed，
    同时开出 successor 续房（members/admin 继承，parent 指回旧房，
    摘要作为门房的第一条系统消息放进续房）

API:
  GET  /                                   前端页面
  GET  /api/messages?room=X&since=N        -> {"messages":[...], "cursor":N}
  POST /api/send        {"room","sender","text"}
  GET  /api/rooms                          -> {"rooms":[{room,members,state,admin,parent,archived,count,est_tokens,last_ts}]}
  POST /api/rooms       {"room","members","created_by","admin"}   建指定成员的房
  POST /api/rooms/join  {"room","member"}                          拉人入房
  POST /api/rooms/summary {"room","summary","successor"}           admin 交摘要→闭旧开新
  POST /api/rooms/archive {"room","archived"}                      归档/取消归档（数据保留，门房不巡）
  POST /api/rooms/delete  {"room"}                                 真删：房间+消息+工作区+游标从hearth抹掉
                                                                   （只动hearth存储，agent 各自 Session/记忆不受影响）
  GET  /api/workspace?room=X               -> {"room","content","updated_by","updated_ts"}
  POST /api/workspace   {"room","content","updated_by"}            谁决策谁更新
  GET  /api/cursors?room=X                 -> {"cursors":[{member,cursor,start_cursor,...}]}
  POST /api/cursors/prepare {"room","member","upto","kind"}
                                      签发投递单：expected 取台账现值（签发即快照），
                                      upto 必须在 (当前游标, 房内最大消息id] 里。
                                      返回 delivery_id——ack 只认签发过的单
  POST /api/cursors/ack     {"room","member","delivery_id"}
                                      写游标的唯一通道：确认一张签发过的投递单。
                                      单不存在→404（没签发的范围确认不了）；
                                      单已作废（superseded/cursor_moved/recovered）→409；
                                      单上 epoch≠台账 epoch（recover 过）→409；
                                      行不存在→404（recovery_required，绝不静默跳）；
                                      台账现值≠单上 expected→409（以台账为准）；
                                      同单重复确认→duplicate，不报错
  POST /api/cursors/recover {"room","member","cursor":N或"latest","reason","by"}
                                      管理员显式重建游标（recovery 的唯一出口），留事件账。
                                      校验：房必须存在、成员必须在花名册或该房、0<=cursor<=房内最大消息id。
                                      recover 递增该行的 cursor_epoch 并把未确认单作废——旧单从此 ack 不上
  GET  /api/cursors/status               -> {"ready":bool,"migration_ready":bool,"gaps":[...]}
                                      迁移启动闸（v7.2 P0）：migration_ready 标记未落 或
                                      open 房覆盖有缺口 → ready=false，门房/launcher 都拒上岗
  GET  /api/roster                         -> roster.json（全家福名单+默认管理员）

  语义边界（太阳哥 2026-08-31）：prepare+ack 证明的是「服务端签发过的范围被确认」，
  不是「agent 真实处理过消息」——签发→确认之间 agent 端发生了什么，服务端看不见。
  信任边界在门房；要服务端强保证得让 prepare 由服务端计算批次，那是另一个设计。
"""

import json, sqlite3, sys, threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

BASE = Path(__file__).parent
DB = BASE / "chat.db"
FILES = BASE / "files"
ROSTER = BASE / "roster.json"
_lock = threading.Lock()
MAX_UPLOAD = 25 * 1024 * 1024  # 25MB

# 粗估：中英混合聊天约 0.7 token/字符（CJK 偏高英文偏低，取保守值）。
# 「不写死」= 阈值是软的，且存储本来就是整条消息，不会截断在半个回合里。
TOKEN_PER_CHAR = 0.7
CLOSE_THRESHOLD_TOKENS = 80000

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT NOT NULL, sender TEXT NOT NULL,
        text TEXT NOT NULL, ts TEXT NOT NULL)""")
    # v3 迁移：消息可以带一个附件
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "file_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN file_id INTEGER")
    conn.execute("""CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fname TEXT NOT NULL, mime TEXT NOT NULL,
        size INTEGER NOT NULL, ts TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS rooms(
        room TEXT PRIMARY KEY,
        members TEXT NOT NULL DEFAULT '[]',
        state TEXT NOT NULL DEFAULT 'open',
        admin TEXT,
        parent TEXT,
        summary TEXT,
        created_ts TEXT NOT NULL, closed_ts TEXT)""")
    # v6 迁移：房间可归档（藏起来不巡逻，数据还在；删除才是真删）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rooms)").fetchall()]
    if "archived" not in cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    # v6：每房一份工作区（决定/待办/进度），约定是谁决策谁更新
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace(
        room TEXT PRIMARY KEY,
        content TEXT NOT NULL DEFAULT '',
        updated_by TEXT, updated_ts TEXT)""")
    # v7：游标台账——每个 (房间,成员) 一行的投递游标，at-least-once 的事实源。
    # 真相在这里，门房本地 state 只剩节流/报警去重，丢了不丢信。
    # cursor_epoch（v7.2）：recover 重建时递增，投递单签发时快照 epoch——
    # 重建前签的旧单即使文本上还活着，epoch 对不上也 ack 不了
    conn.execute("""CREATE TABLE IF NOT EXISTS cursors(
        room TEXT NOT NULL, member TEXT NOT NULL,
        cursor INTEGER NOT NULL, start_cursor INTEGER NOT NULL,
        cursor_epoch INTEGER NOT NULL DEFAULT 0,
        delivery_id TEXT, updated_ts TEXT,
        PRIMARY KEY(room, member))""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cursors)").fetchall()]
    if "cursor_epoch" not in cols:  # 老库补列
        conn.execute("ALTER TABLE cursors ADD COLUMN cursor_epoch INTEGER NOT NULL DEFAULT 0")
    # v7：游标事件账——recover/birth 留痕，审计游标有没有被人动过
    conn.execute("""CREATE TABLE IF NOT EXISTS cursor_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT, member TEXT, kind TEXT, reason TEXT, actor TEXT, ts TEXT)""")
    # v7.1：投递台账——ack 必须绑定一张 server 签发并持久化的投递单。
    # CAS 只防竞态/倒退，不证明 agent 真实处理；没签发过的 delivery_id 确认不了任何范围
    # （太阳哥验收 P0：never-issued 的 delivery_id 能把 cursor 0→999999，HTTP 200）
    # state 给 acked=0 一个记号（zhaozhao 2026-08-31）：pending=还没轮到；
    # superseded=同成员签了新单旧单作废；cursor_moved=ack 时台账已被别人动过；
    # recovered=recover 重置游标后旧单失效。acked=1 时 state='acked'。
    # v7.2：ack 只认 state='pending' 的单——作废的单不是「还没轮到」，是「永远轮不到」；
    # epoch 列快照签发时的 cursor_epoch，recover/delete 之后旧单双保险失效；
    # 删房级联删单（太阳哥复验 P1：旧房删掉重建同名房，旧 token 仍能 ack=200）
    conn.execute("""CREATE TABLE IF NOT EXISTS deliveries(
        delivery_id TEXT PRIMARY KEY,
        room TEXT NOT NULL, member TEXT NOT NULL,
        expected INTEGER NOT NULL, upto INTEGER NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0,
        kind TEXT, issued_ts TEXT NOT NULL,
        acked INTEGER NOT NULL DEFAULT 0, acked_ts TEXT,
        state TEXT NOT NULL DEFAULT 'pending')""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(deliveries)").fetchall()]
    try:  # 老库补列
        conn.execute("ALTER TABLE deliveries ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
    except sqlite3.OperationalError:
        pass
    if "epoch" not in cols:
        conn.execute("ALTER TABLE deliveries ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE deliveries SET state='acked' WHERE acked=1 AND state='pending'")
    # v7.2：迁移启动闸的标记位——migrate_cursors_v7.py 在覆盖校验末尾同事务落
    # migration_ready；全新部署（还没有 open 房）由 server 自己落。open 房有历史
    # 却没这标记 = v6 直升 v7 没迁移：门房必须拒上岗，绝不能把旧 state 游标
    # state.pop 抹掉
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    # 迁移：消息表里有但 rooms 表没有的房间，登记为开放房
    for (r,) in conn.execute("SELECT DISTINCT room FROM messages").fetchall():
        conn.execute("INSERT OR IGNORE INTO rooms(room, created_ts) VALUES(?,?)", (r, now()))
    return conn

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def est_tokens(conn, room):
    (n,) = conn.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM messages WHERE room=?", (room,)).fetchone()
    return int(n * TOKEN_PER_CHAR)

def get_room(conn, room):
    r = conn.execute("SELECT room,members,state,admin,parent,summary,archived FROM rooms WHERE room=?",
                     (room,)).fetchone()
    if not r:
        return None
    return {"room": r[0], "members": json.loads(r[1]), "state": r[2],
            "admin": r[3], "parent": r[4], "summary": r[5], "archived": bool(r[6])}

def default_admin():
    try:
        return json.loads(ROSTER.read_text(encoding="utf-8")).get("default_admin", "mingming")
    except Exception:
        return "mingming"

def roster_agents():
    try:
        return json.loads(ROSTER.read_text(encoding="utf-8")).get("agents", [])
    except Exception:
        return []

def open_ledger(conn, room, members, start):
    """建房/join 事务的一部分：给成员落 cursor 行——出生证明（start_cursor 记起点）。
    有行不碰（重进房不清历史）。花名册在册却缺行 = 游标丢失，那是 recover 的事，
    谁也别想用「第一次上岗」伪装事故。"""
    for m in members:
        conn.execute("""INSERT OR IGNORE INTO cursors(room,member,cursor,start_cursor,updated_ts)
                        VALUES(?,?,?,?,?)""", (room, m, start, start, now()))

def cursor_coverage(conn):
    """启动闸的实质校验：每个 open 房的应覆成员（限定房=房内成员，开放房=花名册全员）
    都必须有 cursor 行。返回缺口列表 ["房/成员", ...]，空 = 覆盖完整。"""
    gaps = []
    roster = roster_agents()
    for room, members in conn.execute("SELECT room,members FROM rooms WHERE state='open'").fetchall():
        targets = json.loads(members) or roster
        have = {r[0] for r in conn.execute("SELECT member FROM cursors WHERE room=?", (room,))}
        gaps += [f"{room}/{m}" for m in targets if m not in have]
    return gaps

def migration_ready(conn):
    """migration_ready 标记：迁移脚本在覆盖校验末尾同事务落。此处是自愈兜底——
    全新部署（还没有 open 房），或台账已有行且覆盖完整（比如全靠 recover 救回来的）。
    open 房有历史、没标记、覆盖又有缺口 = v6 直升 v7 没迁移：不许 ready。"""
    if conn.execute("SELECT 1 FROM meta WHERE key='migration_ready'").fetchone():
        return True
    gaps = cursor_coverage(conn)
    open_rooms = conn.execute("SELECT 1 FROM rooms WHERE state='open' LIMIT 1").fetchone()
    has_rows = conn.execute("SELECT 1 FROM cursors LIMIT 1").fetchone()
    if not open_rooms or (has_rows and not gaps):
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migration_ready',?)", (now(),))
        return True
    return False

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):  # 安静点
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            body = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/messages":
            room = q.get("room", ["大厅"])[0]
            since = int(q.get("since", ["0"])[0])
            with _lock, db() as c:
                rows = c.execute(
                    """SELECT m.id,m.room,m.sender,m.text,m.ts,m.file_id,f.fname,f.mime,f.size
                       FROM messages m LEFT JOIN files f ON f.id=m.file_id
                       WHERE m.room=? AND m.id>? ORDER BY m.id LIMIT 500""",
                    (room, since)).fetchall()
            msgs = [{"id": r[0], "room": r[1], "sender": r[2], "text": r[3], "ts": r[4],
                     "file": {"id": r[5], "fname": r[6], "mime": r[7], "size": r[8]} if r[5] else None}
                    for r in rows]
            self._json({"messages": msgs, "cursor": msgs[-1]["id"] if msgs else since})
        elif u.path.startswith("/api/files/"):
            fid = u.path.rsplit("/", 1)[1]
            if not fid.isdigit():
                return self._json({"error": "not found"}, 404)
            with _lock, db() as c:
                r = c.execute("SELECT fname,mime,size FROM files WHERE id=?", (fid,)).fetchone()
            fp = FILES / fid
            if not r or not fp.exists():
                return self._json({"error": "not found"}, 404)
            body = fp.read_bytes()
            disp = "inline" if r[1].startswith("image/") else "attachment"
            self.send_response(200)
            self.send_header("Content-Type", r[1])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             f"{disp}; filename*=UTF-8''{urllib.parse.quote(r[0])}")
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/rooms":
            with _lock, db() as c:
                rows = c.execute(
                    "SELECT room,members,state,admin,parent,archived FROM rooms ORDER BY rowid").fetchall()
                out = []
                for r in rows:
                    (cnt,) = c.execute("SELECT COUNT(*) FROM messages WHERE room=?", (r[0],)).fetchone()
                    last = c.execute("SELECT MAX(ts) FROM messages WHERE room=?", (r[0],)).fetchone()[0]
                    out.append({"room": r[0], "members": json.loads(r[1]), "state": r[2],
                                "admin": r[3], "parent": r[4], "archived": bool(r[5]), "count": cnt,
                                "est_tokens": est_tokens(c, r[0]), "last_ts": last})
            self._json({"rooms": out})
        elif u.path == "/api/workspace":
            room = q.get("room", ["大厅"])[0]
            with _lock, db() as c:
                r = c.execute("SELECT content,updated_by,updated_ts FROM workspace WHERE room=?",
                              (room,)).fetchone()
            self._json({"room": room, "content": r[0] if r else "",
                        "updated_by": r[1] if r else None, "updated_ts": r[2] if r else None})
        elif u.path == "/api/cursors":
            room = q.get("room", [""])[0]
            with _lock, db() as c:
                rows = c.execute(
                    "SELECT member,cursor,start_cursor,delivery_id,updated_ts,cursor_epoch FROM cursors WHERE room=?",
                    (room,)).fetchall()
            self._json({"cursors": [{"member": r[0], "cursor": r[1], "start_cursor": r[2],
                                     "delivery_id": r[3], "updated_ts": r[4], "cursor_epoch": r[5]}
                                    for r in rows]})
        elif u.path == "/api/cursors/status":
            # 迁移启动闸（v7.2 P0）：门房和 launcher 上岗前都查这里。
            # ready=false 时门房直接退出且绝不写 state——
            # 不许出现「新门房把旧 state 游标抹掉，迁移脚本无源可迁」
            with _lock, db() as c:
                ready_flag = migration_ready(c)
                gaps = cursor_coverage(c)
            self._json({"ready": ready_flag and not gaps,
                        "migration_ready": ready_flag, "gaps": gaps})
        elif u.path == "/api/roster":
            try:
                self._json(json.loads(ROSTER.read_text(encoding="utf-8")))
            except Exception:
                self._json({"agents": [], "humans": [], "default_admin": "mingming"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/send":
                return self._send()
            elif path == "/api/upload":
                return self._upload()
            elif path == "/api/rooms":
                return self._create_room()
            elif path == "/api/rooms/join":
                return self._join()
            elif path == "/api/rooms/update":
                return self._update_room()
            elif path == "/api/rooms/close":
                return self._close_room()
            elif path == "/api/rooms/summary":
                return self._submit_summary()
            elif path == "/api/rooms/archive":
                return self._archive_room()
            elif path == "/api/rooms/delete":
                return self._delete_room()
            elif path == "/api/workspace":
                return self._save_workspace()
            elif path == "/api/cursors/prepare":
                return self._prepare_delivery()
            elif path == "/api/cursors/ack":
                return self._ack_cursor()
            elif path == "/api/cursors/recover":
                return self._recover_cursor()
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _upload(self):
        # 原始字节流上传：curl -X POST --data-binary @file \
        #   "…/api/upload?filename=<url编码>" -H "Content-Type: <mime>"
        q = parse_qs(urlparse(self.path).query)
        fname = (q.get("filename", ["file"])[0]).strip().replace("/", "_").replace("\\", "_")[:100] or "file"
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return self._json({"error": "empty body"}, 400)
        if n > MAX_UPLOAD:
            return self._json({"error": "too large (>25MB)"}, 400)
        data = self.rfile.read(n)
        mime = self.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        with _lock, db() as c:
            cur = c.execute("INSERT INTO files(fname,mime,size,ts) VALUES(?,?,?,?)",
                            (fname, mime, n, now()))
            fid = cur.lastrowid
        FILES.mkdir(exist_ok=True)
        (FILES / str(fid)).write_bytes(data)
        self._json({"ok": True, "file_id": fid, "fname": fname})

    def _send(self):
        data = self._body()
        room = (data.get("room") or "大厅").strip()[:50]
        sender = (data.get("sender") or "无名").strip()[:30]
        text = (data.get("text") or "").strip()
        if not text:
            return self._json({"error": "empty text"}, 400)
        if len(text) > 8000:
            return self._json({"error": "too long"}, 400)
        file_id = data.get("file_id")
        with _lock, db() as c:
            if file_id is not None:
                f = c.execute("SELECT id FROM files WHERE id=?", (file_id,)).fetchone()
                if not f:
                    return self._json({"error": "no such file"}, 404)
                file_id = f[0]
            r = get_room(c, room)
            if r and r["archived"]:
                # 归档房拒发（P1）：门房不巡归档房，发进去的消息会静默积压——
                # 要发先显式解档（POST /api/rooms/archive archived=false）
                return self._json({"error": f"房间「{room}」已归档（门房不巡，发言会静默积压）——先解档再发"}, 409)
            if r and r["state"] != "open":
                return self._json({"error": f"房间「{room}」已{r['state'] == 'closing' and '进入关闭流程（等摘要）' or '关闭'}，请去续房"},
                                  409)
            if not r:  # 老习惯：发到不存在的房 = 开一个开放房
                c.execute("INSERT INTO rooms(room, created_ts) VALUES(?,?)", (room, now()))
                # 开放房出生即落全员游标行（start=0：开张消息会投递，新房开张不哑）
                open_ledger(c, room, roster_agents(), 0)
            cur = c.execute("INSERT INTO messages(room,sender,text,ts,file_id) VALUES(?,?,?,?,?)",
                            (room, sender, text, now(), file_id))
            mid = cur.lastrowid
            closed = False
            if est_tokens(c, room) >= CLOSE_THRESHOLD_TOKENS:
                c.execute("UPDATE rooms SET state='closing', admin=COALESCE(admin, ?) WHERE room=? AND state='open'",
                          (default_admin(), room))
                closed = c.execute("SELECT state='closing' FROM rooms WHERE room=?", (room,)).fetchone()[0]
        resp = {"ok": True, "id": mid}
        if closed:
            resp["closing"] = True
        self._json(resp)

    def _create_room(self):
        data = self._body()
        room = (data.get("room") or "").strip()[:50]
        if not room:
            return self._json({"error": "empty room name"}, 400)
        members = data.get("members") or []
        admin = (data.get("admin") or "").strip() or default_admin()
        with _lock, db() as c:
            if get_room(c, room):
                return self._json({"error": "房间已存在"}, 409)
            c.execute("INSERT INTO rooms(room, members, admin, created_ts) VALUES(?,?,?,?)",
                      (room, json.dumps(members, ensure_ascii=False), admin, now()))
            # 建房事务落 cursor 行：限定房落在册成员，开放房（members 空）落全员
            open_ledger(c, room, members or roster_agents(), 0)
        self._json({"ok": True, "room": room})

    def _join(self):
        data = self._body()
        room, member = data.get("room"), (data.get("member") or "").strip()
        with _lock, db() as c:
            r = get_room(c, room)
            if not r:
                return self._json({"error": "no such room"}, 404)
            if member and member not in r["members"]:
                r["members"].append(member)
                c.execute("UPDATE rooms SET members=? WHERE room=?",
                          (json.dumps(r["members"], ensure_ascii=False), room))
                # join 事务同时落 cursor 行（出生证明）——合法新成员和游标丢失
                # 从此是两条互不能伪装的路：有 join 记录的是新人，缺行的才是事故
                open_ledger(c, room, [member], 0)
        self._json({"ok": True})

    def _update_room(self):
        # 房间设置：换管理员、增/删成员。家庭信任模型，不做权限。
        data = self._body()
        room = (data.get("room") or "").strip()
        with _lock, db() as c:
            r = get_room(c, room)
            if not r:
                return self._json({"error": "no such room"}, 404)
            admin = (data.get("admin") or "").strip()
            if admin:
                c.execute("UPDATE rooms SET admin=? WHERE room=?", (admin, room))
            add = (data.get("add_member") or "").strip()
            if add and add not in r["members"]:
                r["members"].append(add)
                c.execute("UPDATE rooms SET members=? WHERE room=?",
                          (json.dumps(r["members"], ensure_ascii=False), room))
                open_ledger(c, room, [add], 0)  # 加成员 = join，同事务落 cursor 行
            rem = (data.get("remove_member") or "").strip()
            if rem and rem in r["members"]:
                r["members"].remove(rem)
                c.execute("UPDATE rooms SET members=? WHERE room=?",
                          (json.dumps(r["members"], ensure_ascii=False), room))
        self._json({"ok": True})

    def _close_room(self):
        # 手动触发关房流程：state -> closing，门房会去叫管理员写摘要
        data = self._body()
        room = (data.get("room") or "").strip()
        with _lock, db() as c:
            r = get_room(c, room)
            if not r:
                return self._json({"error": "no such room"}, 404)
            if r["state"] != "open":
                return self._json({"error": f"房间已是 {r['state']}"}, 409)
            c.execute("UPDATE rooms SET state='closing', admin=COALESCE(admin, ?) WHERE room=?",
                      (default_admin(), room))
        self._json({"ok": True, "closing": room})

    def _submit_summary(self):
        data = self._body()
        room = (data.get("room") or "").strip()
        summary = (data.get("summary") or "").strip()
        successor = (data.get("successor") or f"{room}·续").strip()[:50]
        if not summary:
            return self._json({"error": "empty summary"}, 400)
        with _lock, db() as c:
            r = get_room(c, room)
            if not r:
                return self._json({"error": "no such room"}, 404)
            c.execute("UPDATE rooms SET state='closed', summary=?, closed_ts=? WHERE room=?",
                      (summary, now(), room))
            if not get_room(c, successor):
                c.execute("INSERT INTO rooms(room, members, admin, parent, created_ts) VALUES(?,?,?,?,?)",
                          (successor, json.dumps(r["members"], ensure_ascii=False),
                           r["admin"], room, now()))
                # 续房出生即落行（start=0）：摘要作为第一条消息，全员 cursor 从 0 起，
                # 换房后每个人最先收到的就是管理员摘要
                open_ledger(c, successor, r["members"] or roster_agents(), 0)
            c.execute("INSERT INTO messages(room,sender,text,ts) VALUES(?,?,?,?)",
                      (successor, "门房", f"本房继承自「{room}」。前情摘要：\n{summary}", now()))
        self._json({"ok": True, "successor": successor})

    def _archive_room(self):
        # 归档 = 藏起来：数据全留（消息/工作区/附件都在），门房不巡、列表收进「已归档」。
        data = self._body()
        room = (data.get("room") or "").strip()
        archived = 1 if data.get("archived", True) else 0
        with _lock, db() as c:
            if not get_room(c, room):
                return self._json({"error": "no such room"}, 404)
            c.execute("UPDATE rooms SET archived=? WHERE room=?", (archived, room))
        self._json({"ok": True, "room": room, "archived": bool(archived)})

    def _delete_room(self):
        # 真删 = hearth存储里这间房的痕迹全抹掉：消息、工作区、没人引用的附件。
        # 只动hearth这一层——agent 各自的 Session 和记忆库跟这里零耦合，一根毛不动。
        data = self._body()
        room = (data.get("room") or "").strip()
        with _lock, db() as c:
            if not get_room(c, room):
                return self._json({"error": "no such room"}, 404)
            fids = [r[0] for r in c.execute(
                "SELECT DISTINCT file_id FROM messages WHERE room=? AND file_id IS NOT NULL",
                (room,)).fetchall()]
            c.execute("DELETE FROM messages WHERE room=?", (room,))
            c.execute("DELETE FROM workspace WHERE room=?", (room,))
            c.execute("DELETE FROM cursors WHERE room=?", (room,))
            c.execute("DELETE FROM cursor_events WHERE room=?", (room,))
            # 级联删投递单（v7.2 P1）：旧房的 pending 单不删，重建同名房后旧 token
            # 还能 ack=200 把新房游标推走——房没了，它的单一张都不许活
            c.execute("DELETE FROM deliveries WHERE room=?", (room,))
            c.execute("DELETE FROM rooms WHERE room=?", (room,))
            gone = []
            for fid in fids:  # 附件可能被别房的消息引用，只删没人要的
                still = c.execute("SELECT 1 FROM messages WHERE file_id=? LIMIT 1", (fid,)).fetchone()
                if not still:
                    c.execute("DELETE FROM files WHERE id=?", (fid,))
                    gone.append(fid)
        for fid in gone:
            try:
                (FILES / str(fid)).unlink(missing_ok=True)
            except Exception:
                pass
        self._json({"ok": True, "deleted": room})

    def _prepare_delivery(self):
        # 签发投递单——ack 的唯一合法对象（v7.1 P0：ack 必须绑定真实投递）。
        # expected 取台账现值（签发即快照）；upto 必须在 (当前游标, 房内最大消息id]，
        # 超出房源消息的范围签不出来。行不存在→404（recovery_required）。
        data = self._body()
        room, member = data.get("room"), (data.get("member") or "").strip()
        upto = data.get("upto")
        kind = (data.get("kind") or "wake")[:20]
        if not room or not member or not isinstance(upto, int) or isinstance(upto, bool):
            return self._json({"error": "need room/member/upto（整数）"}, 400)
        with _lock, db() as c:
            if not get_room(c, room):
                return self._json({"error": "no such room"}, 404)
            r = c.execute("SELECT cursor,cursor_epoch FROM cursors WHERE room=? AND member=?",
                          (room, member)).fetchone()
            if not r:
                return self._json({"error": "no cursor row", "recovery_required": True}, 404)
            expected, epoch = r
            (mx,) = c.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE room=?",
                              (room,)).fetchone()
            if upto <= expected:
                return self._json({"error": "upto 必须大于当前游标", "ledger_cursor": expected}, 409)
            if upto > mx:
                return self._json({"error": "upto 超出房内最大消息 id", "room_max": mx}, 400)
            did = uuid4().hex
            # 同成员签新单，旧 pending 单作废——它们从此不可能 ack（expected 对不上）
            c.execute("UPDATE deliveries SET state='superseded' WHERE room=? AND member=? AND state='pending'",
                      (room, member))
            c.execute("""INSERT INTO deliveries(delivery_id,room,member,expected,upto,epoch,kind,issued_ts)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (did, room, member, expected, upto, epoch, kind, now()))
            # 历史清理（v7.2 P1）：终态单留 7 天够审计了，台账不能只进不出
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("DELETE FROM deliveries WHERE state!='pending' AND issued_ts<?", (cutoff,))
        self._json({"ok": True, "delivery_id": did, "expected": expected, "upto": upto})

    def _ack_cursor(self):
        # 写游标的唯一通道：确认一张 server 签发过的投递单（单调 CAS 在单内完成）。
        # 单不存在→404（没签发过的范围确认不了）；单不属于这个房/成员→400；
        # 行不存在→404（recovery_required，门房只报警，等管理员 recover）；
        # 台账现值≠单上 expected→409（以台账为准）；同单重复确认→duplicate。
        data = self._body()
        room, member = data.get("room"), (data.get("member") or "").strip()
        did = (data.get("delivery_id") or "")[:80]
        if not room or not member or not did:
            return self._json({"error": "need room/member/delivery_id"}, 400)
        with _lock, db() as c:
            d = c.execute("SELECT room,member,expected,upto,acked,state,epoch FROM deliveries WHERE delivery_id=?",
                          (did,)).fetchone()
            if not d:
                return self._json({"error": "unknown delivery（没签发过的单确认不了任何范围）"}, 404)
            if d[0] != room or d[1] != member:
                return self._json({"error": "delivery 不属于这个 room/member"}, 400)
            expected, upto, acked, dstate, depoch = d[2], d[3], d[4], d[5], d[6]
            if acked:
                return self._json({"ok": True, "duplicate": True})  # 幂等重 ack：不报错
            # 只认 pending：作废的单（superseded/cursor_moved/recovered）不是「还没轮到」，
            # 是永远轮不到——旧单 ack 成功就等于死单复活推游标（v7.2 P1）
            if dstate != "pending":
                return self._json({"error": f"delivery 已作废（state={dstate}）——重新 prepare 一张"}, 409)
            r = c.execute("SELECT cursor,cursor_epoch FROM cursors WHERE room=? AND member=?",
                          (room, member)).fetchone()
            if not r:
                return self._json({"error": "no cursor row", "recovery_required": True}, 404)
            if r[1] != depoch:
                # epoch 对不上 = 签发之后 recover 重建过——旧单作废，别推游标
                c.execute("UPDATE deliveries SET state='recovered' WHERE delivery_id=?", (did,))
                return self._json({"error": "cursor epoch 已更迭（recover 过），此单作废", "ledger_epoch": r[1]}, 409)
            if r[0] != expected:
                c.execute("UPDATE deliveries SET state='cursor_moved' WHERE delivery_id=?", (did,))
                return self._json({"error": "cursor moved", "ledger_cursor": r[0]}, 409)
            # 落闸在 SQL 层再锁一道：先转单（只许 pending→acked 一次转移），rowcount
            # 兜底——万一有第二个写者绕过 Python 层改掉了 state，0 行命中就当场拒，
            # 游标根本不碰，绝不让一张非 pending 单推成游标（太阳哥复验 v7.2：
            # 闸不能只记在 Python 层的 if 里）
            cur = c.execute("UPDATE deliveries SET acked=1, acked_ts=?, state='acked'"
                            " WHERE delivery_id=? AND state='pending'", (now(), did))
            if cur.rowcount != 1:
                return self._json({"error": "delivery 状态在确认瞬间被改写——本次确认作废"}, 409)
            c.execute("UPDATE cursors SET cursor=?, delivery_id=?, updated_ts=? WHERE room=? AND member=?",
                      (upto, did, now(), room, member))
        self._json({"ok": True, "cursor": upto})

    def _recover_cursor(self):
        # 管理员显式重建游标——recovery_required 的唯一出口，留事件账可审计。
        # 也用于新成员入户（kind=birth）：首次上岗走这里，不许和事故共用一个表象。
        # v7.1 P1：校验实体和边界——房必须存在、成员必须在花名册或该房（ghost 拒）、
        # 0 <= cursor <= 房内最大消息 id（越界拒）。
        data = self._body()
        room, member = data.get("room"), (data.get("member") or "").strip()
        target = data.get("cursor")  # 整数，或 "latest" = 该房当前最大消息 id
        reason = (data.get("reason") or "")[:200]
        by = (data.get("by") or "管理员").strip()[:30]
        if not room or not member:
            return self._json({"error": "need room/member"}, 400)
        with _lock, db() as c:
            r = get_room(c, room)
            if not r:
                return self._json({"error": "no such room"}, 404)
            known = set(roster_agents()) | set(r["members"])
            if member not in known:
                return self._json({"error": f"未知成员「{member}」（不在花名册也不在该房）"}, 400)
            (mx,) = c.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE room=?",
                              (room,)).fetchone()
            if target == "latest":
                target = mx
            if not isinstance(target, int) or isinstance(target, bool):
                return self._json({"error": 'need cursor（整数或 "latest"）'}, 400)
            if not (0 <= target <= mx):
                return self._json({"error": f"cursor 越界（合法范围 0..{mx}）", "room_max": mx}, 400)
            existed = c.execute("SELECT 1 FROM cursors WHERE room=? AND member=?",
                                (room, member)).fetchone()
            # recover 递增 cursor_epoch：重建前签的投递单 epoch 对不上，双保险作废
            c.execute("""INSERT INTO cursors(room,member,cursor,start_cursor,cursor_epoch,updated_ts)
                         VALUES(?,?,?,?,1,?)
                         ON CONFLICT(room,member) DO UPDATE SET cursor=excluded.cursor,
                             cursor_epoch=cursors.cursor_epoch+1, updated_ts=excluded.updated_ts""",
                      (room, member, target, target, now()))
            c.execute("INSERT INTO cursor_events(room,member,kind,reason,actor,ts) VALUES(?,?,?,?,?,?)",
                      (room, member, "recover" if existed else "birth", reason, by, now()))
            # 游标被重置，旧 pending 单永远 ack 不上了——记为 recovered，别装成还没轮到
            c.execute("UPDATE deliveries SET state='recovered' WHERE room=? AND member=? AND state='pending'",
                      (room, member))
        self._json({"ok": True, "room": room, "member": member, "cursor": target})

    def _save_workspace(self):
        # 工作区：谁决策谁更新。家庭信任模型，不做权限，但记账（updated_by/ts）。
        data = self._body()
        room = (data.get("room") or "").strip()
        content = data.get("content") or ""
        by = (data.get("updated_by") or "无名").strip()[:30]
        if not room:
            return self._json({"error": "empty room"}, 400)
        if len(content) > 20000:
            return self._json({"error": "too long (>20000字)"}, 400)
        with _lock, db() as c:
            c.execute("""INSERT INTO workspace(room,content,updated_by,updated_ts) VALUES(?,?,?,?)
                         ON CONFLICT(room) DO UPDATE SET content=excluded.content,
                             updated_by=excluded.updated_by, updated_ts=excluded.updated_ts""",
                      (room, content, by, now()))
        self._json({"ok": True, "room": room})

if __name__ == "__main__":
    port = 7788
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    conn = db()
    ready, gaps = migration_ready(conn), cursor_coverage(conn)
    conn.close()
    try:
        print(f"客厅开着：http://localhost:{port}")
        if not (ready and not gaps):
            print(f"⚠ 迁移启动闸未过（migration_ready={ready}，覆盖缺口 {len(gaps)} 个）"
                  "——门房会被拒上岗，先跑 migrate_cursors_v7.py")
    except Exception:
        pass  # pythonw 下没有 stdout
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
