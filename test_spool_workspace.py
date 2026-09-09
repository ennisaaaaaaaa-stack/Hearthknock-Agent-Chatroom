#!/usr/bin/env python3
"""test_spoor_workspace.py —— 工作区写改道（v8）+ A3 客户端的可重复验收。

覆盖（契约：hui #335/#340，太阳哥验收口径 #339/#371）：
  1. 关闭态回归：spoor-write.json 留空，POST /api/workspace 行为与改道前一致
  2. 写口活着：POST 铸 workspace.set 事件 append 成功才落投影（带 last_event_id）
  3. 写口挂掉：事件先进本地 spool（A3 先落盘），响应带 queued，投影不动
  4. 写口恢复：下一次 POST 连带冲掉 spool 里的积压，按序到达，投影补上；
     中央拒收（conflict）→ 409、投影绝不动、事件隔离留账（#371 P0-2）
  5. 幂等重放：同 id 同 body 重发 → 200 {"status":"duplicate"}，客户端当确认（不双记不卡队）
  6. 409 毒消息：同 id 不同 body → 隔离进 conflicts 留账，不挡后面的事件
  7. origin 分片（#351）：spool/conflicts 文件名带 origin，各进程各冲各的队列

客户端契约（body status 判别 + id 回显守门 + TokenSpy + origin 校验 +
四态返回 + 崩溃窗故障注入 + 实例级配置 + 限界 drain）的逐条断言在
test_spoor_client_contract.py——与 spoor repo 里zhaozhao版
（tests/test_spoor_http_client.py 9b90392）同一套断言的hearth正本移植。

跑法：python test_spoor_workspace.py
server 侧用临时目录里的副本起子进程（真 server.py 的 DB 固定在脚本旁，
直接起会动到生产 chat.db）；spoor_client 单测直接 import 后改路径指向临时目录。
"""

import json, shutil, subprocess, sys, tempfile, time, threading
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).parent
PORT_SERVER = 17891
PORT_MOCK = 17892


class MockMouth:
    """假写口：按 id 幂等去重。down=True 时全部 500（模拟写口挂掉）。
    响应按 #344 契约：body 的 status 枚举 + id 回显是机器判别唯一权威。"""

    def __init__(self):
        self.events = {}   # id -> body 原文（字节级比对用）
        self.order = []    # 到达顺序
        self.replayed = 0
        self.down = False
        self.reject = False  # True 时对任何 id 回 409 conflict（#371 P0-2 测试用）

    def handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                if not self.path.startswith("/append"):
                    return self._j(404, {"status": "notfound"})
                if mock.down:
                    return self._j(500, {"status": "error"})
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                e = json.loads(raw.decode("utf-8"))
                if mock.reject:
                    return self._j(409, {"status": "conflict", "id": e["id"]})
                if e["id"] in mock.events:
                    if mock.events[e["id"]] == raw:
                        mock.replayed += 1
                        return self._j(200, {"status": "duplicate", "id": e["id"]})
                    return self._j(409, {"status": "conflict", "id": e["id"]})
                mock.events[e["id"]] = raw
                mock.order.append(e["id"])
                self._j(200, {"status": "ok", "id": e["id"]})

            def _j(self, code, obj):
                b = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        return H


def post(port, path, obj):
    req = urllib.request.Request(f"http://localhost:{port}{path}",
                                 data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get(port, path):
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_up(port, path="/api/rooms", tries=50):
    for _ in range(tries):
        try:
            get(port, path)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def start_server(tmp, with_cfg):
    """把 server.py + spoor_client.py 拷进临时目录起子进程（DB 落在临时目录）。"""
    for f in ("server.py", "spoor_client.py"):
        shutil.copy(BASE / f, tmp / f)
    if with_cfg:
        (tmp / "spoor-write.json").write_text(json.dumps({
            "url": f"http://localhost:{PORT_MOCK}", "token": "t0ken",
            "origin": "mingming-win"}), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(tmp / "server.py"), "--port", str(PORT_SERVER)],
                         cwd=tmp)
    assert wait_up(PORT_SERVER), "server 没起来"
    return p


def q(s):
    return urllib.parse.quote(s)


import urllib.parse

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def main():
    mock = MockMouth()
    mouth = ThreadingHTTPServer(("127.0.0.1", PORT_MOCK), mock.handler())
    threading.Thread(target=mouth.serve_forever, daemon=True).start()

    # ── 1. 关闭态回归：没有 spoor-write.json，行为与改道前一致 ──
    # Windows 上子进程 terminate 后 sqlite 句柄释放有延迟，清理目录前等一下
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        p = start_server(Path(td), with_cfg=False)
        try:
            r = post(PORT_SERVER, "/api/workspace",
                     {"room": "测试房", "content": "本地老路", "updated_by": "mingming"})
            check("1a 关闭态落本地", r.get("ok") and "event_id" not in r, json.dumps(r))
            g = get(PORT_SERVER, f"/api/workspace?room={q('测试房')}")
            check("1b 关闭态能读回", g["content"] == "本地老路" and g["updated_by"] == "mingming")
        finally:
            p.terminate(); p.wait(); time.sleep(0.5)

    # ── 2~4. 写口活 → 挂 → 恢复 ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        p = start_server(tmp, with_cfg=True)
        try:
            r = post(PORT_SERVER, "/api/workspace",
                     {"room": "测试房", "content": "v1", "updated_by": "mingming"})
            check("2a 确认后落投影", r.get("ok") and r.get("event_id") and not r.get("queued"),
                  json.dumps(r))
            ev = json.loads(list(mock.events.values())[-1].decode("utf-8"))
            check("2b 事件形状", ev["type"] == "workspace.set" and ev["actor"] == "mingming"
                  and ev["room"] == "测试房" and ev["payload"]["content"] == "v1"
                  and ev["id"].startswith("mingming-win-"), json.dumps(ev)[:200])
            dbfile = tmp / "chat.db"
            import sqlite3
            row = sqlite3.connect(dbfile).execute(
                "SELECT content,last_event_id FROM workspace WHERE room='测试房'").fetchone()
            check("2c 投影带 last_event_id", row == ("v1", ev["id"]), str(row))

            mock.down = True
            r = post(PORT_SERVER, "/api/workspace",
                     {"room": "测试房", "content": "v2-写口挂了", "updated_by": "mingming"})
            check("3a 挂掉时 queued", r.get("ok") and r.get("queued"), json.dumps(r))
            g = get(PORT_SERVER, f"/api/workspace?room={q('测试房')}")
            check("3b 投影不动仍 v1", g["content"] == "v1", g["content"])
            spool = (tmp / "spoor-spool-mingming-win.jsonl").read_text(encoding="utf-8").strip().splitlines()
            check("3c 事件已落 spool（先落盘）", len(spool) == 1, str(len(spool)))

            mock.down = False
            r = post(PORT_SERVER, "/api/workspace",
                     {"room": "测试房", "content": "v3", "updated_by": "mingming"})
            check("4a 恢复后连带冲积压", r.get("ok") and not r.get("queued"), json.dumps(r))
            check("4b 按序到达", len(mock.order) == 3
                  and json.loads(mock.events[mock.order[1]].decode())["payload"]["content"] == "v2-写口挂了"
                  and json.loads(mock.events[mock.order[2]].decode())["payload"]["content"] == "v3",
                  str(mock.order))
            g = get(PORT_SERVER, f"/api/workspace?room={q('测试房')}")
            check("4c 投影补到 v3", g["content"] == "v3", g["content"])
            check("4d spool 清空", not (tmp / "spoor-spool-mingming-win.jsonl").exists()
                  or not (tmp / "spoor-spool-mingming-win.jsonl").read_text(encoding="utf-8").strip())

            # ── 4e~4g. 中央拒收（#371 P0-2）：409，投影绝不动，事件隔离留账 ──
            mock.reject = True
            r409 = None
            try:
                post(PORT_SERVER, "/api/workspace",
                     {"room": "测试房", "content": "v4-被拒", "updated_by": "mingming"})
            except urllib.error.HTTPError as ex:
                r409 = ex
            check("4e 中央拒收 → 409", r409 is not None and r409.code == 409,
                  str(r409.code) if r409 else "没回 409")
            g = get(PORT_SERVER, f"/api/workspace?room={q('测试房')}")
            check("4f conflict 绝不落投影（仍 v3）", g["content"] == "v3", g["content"])
            cfile = tmp / "spool-conflicts-mingming-win.jsonl"
            check("4g 被拒事件隔离留账", cfile.exists()
                  and "v4-被拒" in cfile.read_text(encoding="utf-8"))
            mock.reject = False
        finally:
            p.terminate(); p.wait(); time.sleep(0.5)

    # ── 5/6/7. spoor_client 单测：幂等重放 + 409 隔离 + origin 分片 ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        sys.path.insert(0, str(BASE))
        import spoor_client
        # #371 P0-3：实例级 API——显式传 cfg_file/spool_dir，不动模块全局
        cfgf = tmp / "spoor-write.json"
        cfgf.write_text(json.dumps({
            "url": f"http://localhost:{PORT_MOCK}", "token": "t0ken",
            "origin": "test"}), encoding="utf-8")
        c = spoor_client.SpoorClient(cfg_file=cfgf, spool_dir=tmp)

        e1 = {"id": "test-20260906T200000Z-aaa111", "ts": "2026-09-06T20:00:00Z",
              "type": "workspace.set", "actor": "mingming", "room": "R", "payload": {"content": "A"}}
        cfg = c._load_cfg()
        check("5a 首发 ok", c._post_append(cfg, e1) == ("ok", e1["id"]))
        check("5b 同id同body重放 duplicate（幂等成功）",
              c._post_append(cfg, e1) == ("duplicate", e1["id"]))
        check("5c mock 认成 replayed", mock.replayed >= 1)

        e_conflict = dict(e1, payload={"content": "B"})
        check("6a 同id不同body → conflict",
              c._post_append(cfg, e_conflict) == ("conflict", e1["id"]))
        # 毒消息进队：前面堵一个 409，后面跟一个好的——冲完毒消息隔离、好的确认
        e_good = dict(e1, id="test-20260906T200001Z-bbb222", payload={"content": "good"})
        c._write_spool("test", [e_conflict, e_good])
        confirmed, remaining, _ = c.flush_spool()
        check("6b 毒消息不挡队", confirmed == 1 and remaining == 0,
              f"confirmed={confirmed} remaining={remaining}")
        cpath = tmp / "spool-conflicts-test.jsonl"
        check("6c 毒消息隔离留账（分片文件名）", cpath.exists()
              and "aaa111" in cpath.read_text(encoding="utf-8"))

        # 7. origin 分片（#351）：两个 origin 各走各的文件，互不串队
        e_other = dict(e1, id="other-20260906T200002Z-ccc333", payload={"content": "X"})
        c._write_spool("other", [e_other])
        check("7a 各 origin 各文件", (tmp / "spoor-spool-other.jsonl").exists()
              and "ccc333" not in (tmp / "spoor-spool-test.jsonl").read_text(encoding="utf-8"))
        confirmed, remaining, _ = c.flush_spool()  # 配置 origin=test，只冲自己的片
        check("7b 只冲自己那片", (tmp / "spoor-spool-other.jsonl").exists()
              and "ccc333" in (tmp / "spoor-spool-other.jsonl").read_text(encoding="utf-8"))

    mouth.shutdown()
    failed = [n for n, c, _ in results if not c]
    print(f"\n{'='*40}\n{len(results)-len(failed)}/{len(results)} 过" +
          (f"，挂：{failed}" if failed else "，全绿"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
