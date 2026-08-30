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
  GET  /api/rooms                          -> {"rooms":[{room,members,state,admin,parent,count,est_tokens,last_ts}]}
  POST /api/rooms       {"room","members","created_by","admin"}   建指定成员的房
  POST /api/rooms/join  {"room","member"}                          拉人入房
  POST /api/rooms/summary {"room","summary","successor"}           admin 交摘要→闭旧开新
  GET  /api/roster                         -> roster.json（全家福名单+默认管理员）
"""

import json, sqlite3, sys, threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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
    r = conn.execute("SELECT room,members,state,admin,parent,summary FROM rooms WHERE room=?",
                     (room,)).fetchone()
    if not r:
        return None
    return {"room": r[0], "members": json.loads(r[1]), "state": r[2],
            "admin": r[3], "parent": r[4], "summary": r[5]}

def default_admin():
    try:
        return json.loads(ROSTER.read_text(encoding="utf-8")).get("default_admin", "mingming")
    except Exception:
        return "mingming"

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
                    "SELECT room,members,state,admin,parent FROM rooms ORDER BY rowid").fetchall()
                out = []
                for r in rows:
                    (cnt,) = c.execute("SELECT COUNT(*) FROM messages WHERE room=?", (r[0],)).fetchone()
                    last = c.execute("SELECT MAX(ts) FROM messages WHERE room=?", (r[0],)).fetchone()[0]
                    out.append({"room": r[0], "members": json.loads(r[1]), "state": r[2],
                                "admin": r[3], "parent": r[4], "count": cnt,
                                "est_tokens": est_tokens(c, r[0]), "last_ts": last})
            self._json({"rooms": out})
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
            elif path == "/api/rooms/summary":
                return self._submit_summary()
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
            if r and r["state"] != "open":
                return self._json({"error": f"房间「{room}」已{r['state'] == 'closing' and '进入关闭流程（等摘要）' or '关闭'}，请去续房"},
                                  409)
            if not r:  # 老习惯：发到不存在的房 = 开一个开放房
                c.execute("INSERT INTO rooms(room, created_ts) VALUES(?,?)", (room, now()))
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
        self._json({"ok": True})

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
            c.execute("INSERT INTO messages(room,sender,text,ts) VALUES(?,?,?,?)",
                      (successor, "门房", f"本房继承自「{room}」。前情摘要：\n{summary}", now()))
        self._json({"ok": True, "successor": successor})

if __name__ == "__main__":
    port = 7788
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    db().close()
    try:
        print(f"客厅开着：http://localhost:{port}")
    except Exception:
        pass  # pythonw 下没有 stdout
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
