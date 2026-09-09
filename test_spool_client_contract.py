#!/usr/bin/env python3
"""test_spoor_client_contract.py —— hearth正本 spoor_client 的共用契约测试。

与 spoor repo 里zhaozhao版 tests/test_spoor_http_client.py（9b90392，契约参考
实现）同一套断言的正本移植（#352：契约测试移植到hearth正本；#353：正本过
同一套断言才算上岗完成）。接口从她的 SpoorWriteClient 类适配到正本的
SpoorClient 实例 API（#371 P0-3：一家一个实例，cfg_file/spool_dir 显式传）。

覆盖（契约：hui #335/#344，太阳哥 #339/#345/#352/#371，zhaozhao #343/#351）：
  1. ok 路径：append_event → 服务端记账 → state=confirmed → spool 清空
  2. 幂等：同 id 同 body → duplicate（可识别的幂等成功，确认+删）
  3. 冲突：同 id 不同 body → conflict 隔离留账，不挡队不重试
  4. 服务不可达 → 事件滞留 spool；恢复后冲走
  5. 先落盘再发送（A3 crash-safety）
  6. id 回显守门（#345）：错 id / 缺 id / 未知 status 一律留队+记账；
     conflict 回显对 id → 隔离，state=conflict 绝不装成 confirmed（#371 P0-2）；
     故障注入（#371 P0-1）：崩在「隔离账已落、主队列未删」两步中间，事件
     仍可追（隔离账留底），恢复重放 conflicts 去重不双记；
     conflicts 自身写法（#406 P0）：canonical 整条去重（同 id 不同 body
     各留一条、同 body 重放不双记），写前/写中暴毙主队列不删、旧账原样
  7. token 走 query（家规），不进 body 不进 header
  8. origin 校验（#352）：缺失/文件名不安全 → 视同关闭，不默认不撞片
  9. 实例级配置（#371 P0-3）：两家各传各的 cfg_file，独立 origin+spool，
     互不串队——不靠改模块全局
 10. 限界 drain（#371 P1）：条数上限/总预算到点即停，余下留队；
     慢请求 elapsed 不越预算（#406 P1：去掉 1s 地板，超时被剩余预算钳制）
 11. 锁外竞态（#421 P0）：单次 append_event 的入队→flush→判态收进同一
     实例锁（RLock）临界区——两线程复现「A 入队→B 入队→A 的 flush 代确认
     删 A/B」时序，修复后两次都 confirmed、确认/投影顺序落 B；flush 的
     verdicts 按 canonical 整条键（同 id 不同 body 不互相覆盖审计结果）

跑法：python test_spoor_client_contract.py（mock server 随机端口，测完自关）
"""

import json, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
import spoor_client  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


class Mock:
    """假写口：mode 切换各种响应人格。normal = 按 id 幂等去重 + status/id 回显。"""

    def __init__(self):
        self.store = {}       # id -> 事件规范形
        self.mode = "normal"  # normal/down/wrongid/noid/oddbody/conflict_echo/slow
        self.seen_paths = []
        self.slow_s = 0.0     # slow 人格：每条请求先睡这么久再正常处理

    def handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                mock.seen_paths.append(self.path)
                if not self.path.startswith("/append"):
                    return self._j(404, {"status": "notfound"})
                if mock.mode == "down":
                    return self._j(500, {"status": "error"})
                n = int(self.headers.get("Content-Length", 0))
                try:
                    ev = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return self._j(400, {"status": "bad"})
                eid = ev.get("id", "")
                if mock.mode == "wrongid":   # 代理串包/服务端回错事件
                    return self._j(200, {"status": "ok", "id": eid + "-other"})
                if mock.mode == "noid":      # 合法 200，body 无 id
                    return self._j(200, {"status": "ok"})
                if mock.mode == "oddbody":   # 200 但 body 不认识
                    return self._j(200, {"hello": "?"})
                if mock.mode == "conflict_echo":
                    return self._j(409, {"status": "conflict", "id": eid})
                if mock.mode == "slow":      # 慢请求：先睡再正常记账
                    time.sleep(mock.slow_s)
                h = json.dumps(ev, ensure_ascii=False, sort_keys=True)
                if eid in mock.store:
                    if mock.store[eid] == h:
                        return self._j(200, {"status": "duplicate", "id": eid})
                    return self._j(409, {"status": "conflict", "id": eid})
                mock.store[eid] = h
                self._j(200, {"status": "ok", "id": eid})

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


def mkclient(tmp, url, token="t0ken", origin="test", name="spoor-write.json"):
    """一家一个实例（#371 P0-3）：显式传 cfg_file + spool_dir，不动模块全局。"""
    cfg = tmp / name
    cfg.write_text(json.dumps({"url": url, "token": token, "origin": origin}),
                   encoding="utf-8")
    return spoor_client.SpoorClient(cfg_file=cfg, spool_dir=tmp)


def ev(eid, content="A"):
    return {"id": eid, "ts": "2026-09-06T20:00:00Z", "type": "workspace.set",
            "actor": "mingming", "room": "R", "payload": {"content": content}}


def main():
    mock = Mock()
    srv = HTTPServer(("127.0.0.1", 0), mock.handler())
    srv.handle_error = lambda *a, **k: None  # slow 测试客户端先超时，服务端写残管不刷屏
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)

        # ── 1. ok 路径 ──
        c = mkclient(tmp, url)
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "v1"})
        check("1a 服务端已记账", event["id"] in mock.store)
        check("1b 确认后 state=confirmed 且不排队",
              state == "confirmed" and c._read_spool("test") == [], state)

        # ── 2. 幂等：同 id 同 body → duplicate → 确认+删 ──
        cfg = c._load_cfg()
        check("2a 同id同body → duplicate",
              c._post_append(cfg, ev("c-e2")) == ("ok", "c-e2")
              and c._post_append(cfg, ev("c-e2")) == ("duplicate", "c-e2"))
        # crash 重放姿势：事件还在 spool，服务端已有 → flush 当确认删掉
        c._write_spool("test", [ev("c-e2")])
        confirmed, remaining, _ = c.flush_spool()
        check("2b 重放当确认出队", confirmed == 1 and remaining == 0,
              f"confirmed={confirmed} remaining={remaining}")

        # ── 3. 冲突：同 id 不同 body → 隔离留账，不挡队不重试 ──
        c._write_spool("test", [ev("c-e3", "第一版")])
        c.flush_spool()  # 服务端收下第一版
        c._write_spool("test", [ev("c-e3", "第二版"), ev("c-e4", "好的")])
        confirmed, remaining, verdicts = c.flush_spool()
        check("3a 毒消息隔离不挡队", confirmed == 1 and remaining == 0,
              f"confirmed={confirmed} remaining={remaining}")
        check("3a2 verdicts 逐条留痕（#421：按 canonical 整条键）",
              verdicts.get(spoor_client._canon(ev("c-e3", "第二版"))) == "conflict"
              and verdicts.get(spoor_client._canon(ev("c-e4", "好的"))) == "ok",
              str(verdicts))
        # 3a3. 同 id 不同 body 一轮批量 flush：按 id 键会互相覆盖审计结果，
        # 按 canonical 整条键各留各的（#421 P0 附带钉）
        c._write_spool("test", [ev("c-e5", "甲版"), ev("c-e5", "乙版")])
        mock.mode = "conflict_echo"
        _, _, verdicts2 = c.flush_spool()
        mock.mode = "normal"
        check("3a3 同id不同body verdicts 不互相覆盖",
              len(verdicts2) == 2
              and verdicts2.get(spoor_client._canon(ev("c-e5", "甲版"))) == "conflict"
              and verdicts2.get(spoor_client._canon(ev("c-e5", "乙版"))) == "conflict",
              str(verdicts2))
        cfile = tmp / "spool-conflicts-test.jsonl"
        check("3b 隔离留账（分片文件名）", cfile.exists()
              and "c-e3" in cfile.read_text(encoding="utf-8"))
        check("3c 冲突不重试（服务端仍只见过两版各一次）",
              mock.store["c-e3"].find("第一版") > 0)
        n_seen = len(mock.seen_paths)
        confirmed, remaining, _ = c.flush_spool()  # 再冲一轮：队列已空
        check("3d 隔离后不再重发", len(mock.seen_paths) == n_seen
              and confirmed == 0 and remaining == 0)

        # ── 4/5. 服务不可达 → 滞留（先落盘）；恢复 → 冲走 ──
        mock.mode = "down"
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "写口挂了"})
        check("5a 不可达时先落盘", state == "queued"
              and event["id"] in (tmp / "spoor-spool-test.jsonl").read_text(encoding="utf-8"),
              state)
        mock.mode = "normal"
        confirmed, remaining, _ = c.flush_spool()
        check("4a 恢复后冲走", confirmed == 1 and remaining == 0
              and event["id"] in mock.store)

        # ── 6. id 回显守门（太阳哥 #345）+ 四态（#371 P0-2）──
        # 6a. 回错 id → status=ok 也不删，留队+计 mismatch
        mock.mode = "wrongid"
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "串包"})
        check("6a 回错 id → 留队", state == "queued"
              and c._read_spool("test")[0]["id"] == event["id"], state)
        check("6b 回错 id → mismatch 计数", c.mismatches() == 1,
              str(c.mismatches()))
        # 6c. 缺 id 回显 → 留队+计 mismatch（#345：留队并记账）
        mock.mode = "noid"
        c._write_spool("test", [])  # 清掉 6a 的，单测这一条
        c._mismatches = 0
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "没回显"})
        check("6c 缺 id → 留队不删", state == "queued", state)
        check("6d 缺 id → 也计 mismatch", c.mismatches() == 1)
        # 6e. 未知 status → 留队不猜，但不算 mismatch（body 没承诺任何语义）
        mock.mode = "oddbody"
        c._write_spool("test", [])
        c._mismatches = 0
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "看不懂"})
        check("6e 未知 status → 留队", state == "queued", state)
        check("6f 未知 status → 不计 mismatch（按 error 回队）",
              c.mismatches() == 0)
        # 6g. conflict 回显对 id → 隔离停发，state=conflict（#371 P0-2：
        # 四态分开，被中央拒绝的绝不许装成 confirmed 去落投影）
        mock.mode = "conflict_echo"
        c._write_spool("test", [])
        event, state = c.append_event("workspace.set", "mingming", "R",
                                      {"content": "毒"})
        check("6g conflict → state=conflict", state == "conflict", state)
        check("6g2 conflict → 隔离留账不在队",
              event["id"] in (tmp / "spool-conflicts-test.jsonl").read_text(encoding="utf-8")
              and c._read_spool("test") == [])
        mock.mode = "normal"

        # 6h. 故障注入（#371 P0-1）：崩在「隔离账已落、主队列未删」两步中间，
        # 事件仍可追；恢复重放 → 服务端幂等兜住、conflicts 按 id 去重不双记
        fidir = tmp / "fi"
        fidir.mkdir()
        fci = mkclient(fidir, url, origin="fi")
        mock.mode = "conflict_echo"
        fci._write_spool("fi", [ev("fi-1"), ev("fi-2")])
        orig_write = fci._write_spool

        def boom(origin, events):
            raise RuntimeError("暴毙在两步中间")

        fci._write_spool = boom
        crashed = False
        try:
            fci.flush_spool()
        except RuntimeError:
            crashed = True
        check("6h 两步中间暴毙：异常上抛（不装没事）", crashed)
        cfl = (fidir / "spool-conflicts-fi.jsonl").read_text(encoding="utf-8")
        check("6h2 事件仍可追：隔离账已留底",
              "fi-1" in cfl and "fi-2" in cfl)
        check("6h3 主队列也还没删（两处至少剩一处）",
              [e["id"] for e in fci._read_spool("fi")] == ["fi-1", "fi-2"])
        fci._write_spool = orig_write
        confirmed, remaining, _ = fci.flush_spool()
        lines = [l for l in (fidir / "spool-conflicts-fi.jsonl")
                 .read_text(encoding="utf-8").splitlines() if l.strip()]
        check("6h4 恢复重放：conflicts 去重不双记、队列清空",
              len(lines) == 2 and remaining == 0,
              f"conflicts={len(lines)} remaining={remaining}")
        mock.mode = "normal"

        # 6i. conflicts 自身写法（#406 P0）：canonical 整条去重 + 原子重写 ──
        fi2 = tmp / "fi2"
        fi2.mkdir()
        f2 = mkclient(fi2, url, origin="fi2")
        mock.mode = "conflict_echo"
        cf2 = fi2 / "spool-conflicts-fi2.jsonl"

        def cf2_lines():
            return [l for l in cf2.read_text(encoding="utf-8").splitlines()
                    if l.strip()]

        # ① 同 id 不同 body：两条都是要留存的冲突证据，按 id 去重会吞后一条
        f2._write_spool("fi2", [ev("fi2-x", "甲版")])
        f2.flush_spool()
        f2._write_spool("fi2", [ev("fi2-x", "乙版")])
        f2.flush_spool()
        ls = cf2_lines()
        check("6i 同id不同body各留一条",
              len(ls) == 2 and any("甲版" in l for l in ls)
              and any("乙版" in l for l in ls), str(ls))
        # ② 同一 body 崩溃重放：canonical 整条去重，不双记
        f2._write_spool("fi2", [ev("fi2-x", "乙版")])
        f2.flush_spool()
        check("6i2 同body重放不双记", len(cf2_lines()) == 2, str(cf2_lines()))
        # ③ 故障注入：conflict 写前失败 → 主队列不删
        f2._write_spool("fi2", [ev("fi2-y", "丙版")])
        orig_ac = f2._append_conflicts

        def boom_pre(origin, events):
            raise RuntimeError("暴毙在隔离账写入前")

        f2._append_conflicts = boom_pre
        crashed = False
        try:
            f2.flush_spool()
        except RuntimeError:
            crashed = True
        check("6i3 写前暴毙：异常上抛", crashed)
        check("6i4 写前暴毙：主队列不删",
              [e["id"] for e in f2._read_spool("fi2")] == ["fi2-y"])
        # ④ 故障注入：conflict 写中暴毙（tmp 里留了半行，replace 没走到）→
        #    主队列不删，旧隔离账整份原样
        def boom_mid(origin, events):
            p = f2._conflicts_path(origin)
            p.with_name(p.name + ".tmp").write_text('{"id":"fi2-y","半截',
                                                    encoding="utf-8")
            raise RuntimeError("暴毙在半行写入")

        f2._append_conflicts = boom_mid
        crashed = False
        try:
            f2.flush_spool()
        except RuntimeError:
            crashed = True
        check("6i5 写中暴毙：异常上抛", crashed)
        check("6i6 写中暴毙：主队列不删、旧隔离账原样",
              [e["id"] for e in f2._read_spool("fi2")] == ["fi2-y"]
              and len(cf2_lines()) == 2)
        # ⑤ 恢复重放：每个不同 body 各一条、同一 body 不双记、队列清空
        f2._append_conflicts = orig_ac
        confirmed, remaining, _ = f2.flush_spool()
        ls = cf2_lines()
        check("6i7 恢复：不同body各一条同body不双记、队列清空",
              len(ls) == 3 and any("丙版" in l for l in ls) and remaining == 0,
              f"conflicts={len(ls)} remaining={remaining}")
        mock.mode = "normal"

        # ── 7. token 走 query（家规），不进 body 不进 header ──
        tokdir = tmp / "tok"
        tokdir.mkdir()
        ct = mkclient(tokdir, url, token="tok-secret")
        ct.append_event("workspace.set", "mingming", "R", {"content": "查票"})
        check("7a token 在 query", any("token=tok-secret" in p
                                       for p in mock.seen_paths[-1:]),
              mock.seen_paths[-1] if mock.seen_paths else "")

        # ── 8. origin 校验（#352）：缺失/文件名不安全 → 视同关闭 ──
        for bad, label in (("", "缺 origin"), ("../evil", "路径分隔符"),
                           ("a/b", "斜杠"), (" ", "空白")):
            cb = mkclient(tmp, url, token="t", origin=bad, name=f"bad-{label}.json")
            check(f"8 origin {label} → 关闭", not cb.enabled())
        cg = mkclient(tmp, url, token="t", origin="mingming-win", name="good.json")
        check("8b 合法 origin → 开着", cg.enabled())

        # ── 9. 实例级配置（#371 P0-3）：两家各传各的 cfg，独立 origin+spool ──
        d9 = tmp / "shards"
        d9.mkdir()
        ca = mkclient(d9, url, origin="house-a", name="a.json")
        cb9 = mkclient(d9, url, origin="house-b", name="b.json")
        mock.mode = "down"
        ea, sa = ca.append_event("workspace.set", "mingming", "R", {"content": "甲"})
        eb, sb = cb9.append_event("workspace.set", "zhaozhao", "R", {"content": "乙"})
        check("9a 各落各的分片", sa == "queued" and sb == "queued"
              and (d9 / "spoor-spool-house-a.jsonl").exists()
              and (d9 / "spoor-spool-house-b.jsonl").exists())
        mock.mode = "normal"
        ca.flush_spool()  # 只冲 a 家
        check("9b a 冲完 b 原样", ca._read_spool("house-a") == []
              and eb["id"] in (d9 / "spoor-spool-house-b.jsonl").read_text(encoding="utf-8"))
        check("9c 实例互不影响：mismatches 各记各的",
              ca.mismatches() == 0 and cb9.mismatches() == 0)

        # ── 10. 限界 drain（#371 P1）：条数上限/总预算到点即停 ──
        d10 = tmp / "drain"
        d10.mkdir()
        cd = mkclient(d10, url, origin="drain")
        cd._write_spool("drain", [ev("dr-1"), ev("dr-2"), ev("dr-3")])
        confirmed, remaining, _ = cd.flush_spool(max_events=1)
        check("10a 条数上限：一次只冲 1 条", confirmed == 1 and remaining == 2,
              f"confirmed={confirmed} remaining={remaining}")
        confirmed, remaining, _ = cd.flush_spool(max_seconds=0)
        check("10b 预算耗尽：一条不发全部留队", confirmed == 0 and remaining == 2,
              f"confirmed={confirmed} remaining={remaining}")
        confirmed, remaining, _ = cd.flush_spool()
        check("10c 下轮照常冲完", confirmed == 2 and remaining == 0,
              f"confirmed={confirmed} remaining={remaining}")
        # 10d. 慢请求（#406 P1）：预算是真上界——服务端每条睡 2s、预算 0.5s，
        # 旧地板 max(1.0, ...) 会让一次 drain 拖到 2s；去地板后请求被剩余
        # 预算钳在 ~0.5s 超时回队，elapsed 不越预算+容差
        d10d = tmp / "drain-slow"
        d10d.mkdir()
        cds = mkclient(d10d, url, origin="drainslow")
        cds._write_spool("drainslow", [ev("ds-1")])
        mock.mode = "slow"
        mock.slow_s = 2.0
        t_start = time.monotonic()
        confirmed, remaining, _ = cds.flush_spool(max_seconds=0.5)
        elapsed = time.monotonic() - t_start
        mock.mode = "normal"
        check("10d 慢请求不越预算：elapsed 钳在预算+容差内",
              elapsed < 0.5 + 0.6 and confirmed == 0 and remaining == 1,
              f"elapsed={elapsed:.2f}s confirmed={confirmed} remaining={remaining}")
        check("10d2 超时回队：恢复后冲走",
              cds.flush_spool()[1] == 0)

        # ── 11. 锁外竞态（#421 P0）：入队→flush→判自己状态在同一临界区 ──
        # 复现太阳哥的最短时序：A 入队→B 入队→A 的 flush 顺带确认并删 A/B→
        # A 落投影；B 自己 flush 见空、verdicts 也没自己 → 报 queued 不落
        # 投影，中央最新是 B、本地面板永久停在 A。
        # 手法：给实例公开 flush_spool 装闸门（旧实现 append_event 入队放锁后
        # 才调它），让 A 的 flush 停在门口等 B 挤进队列；修复后 append_event
        # 全程持 RLock、内部直调 _flush_spool_locked，公开入口的闸门根本不在
        # 临界区里，B 只能在锁外等 A 走完。B 的出发信号用首次 _write_spool
        # （A 的入队落盘）——两种实现里它都必然先于 B 该行动的时点。
        d11 = tmp / "race"
        d11.mkdir()
        cr = mkclient(d11, url, origin="race")
        gate_b_done = threading.Event()
        a_enqueued = threading.Event()
        orig_flush = cr.flush_spool
        orig_write = cr._write_spool
        first_flush = {"v": True}
        writes = {"n": 0}

        def gated_flush(*a, **k):
            if first_flush["v"]:
                first_flush["v"] = False
                gate_b_done.wait(5)  # A 的 flush 停在门口，等 B 挤进队
            return orig_flush(*a, **k)

        def spy_write(origin, events):
            writes["n"] += 1
            if writes["n"] == 1:
                a_enqueued.set()     # A 已入队落盘（两种实现共有的时点）
            return orig_write(origin, events)

        cr.flush_spool = gated_flush
        cr._write_spool = spy_write
        bar = threading.Barrier(2)
        out, projection = {}, []

        def ta():
            bar.wait()
            ea, sa = cr.append_event("workspace.set", "mingming", "R",
                                     {"content": "A 版"})
            out["a"] = (ea, sa)
            if sa == "confirmed":
                projection.append(ea["id"])

        def tb():
            bar.wait()
            a_enqueued.wait(5)  # 保证 A 先入队，B 往「入队→flush」的缝里挤
            eb, sb = cr.append_event("workspace.set", "zhaozhao", "R",
                                     {"content": "B 版"})
            out["b"] = (eb, sb)
            if sb == "confirmed":
                projection.append(eb["id"])
            gate_b_done.set()

        tha = threading.Thread(target=ta)
        thb = threading.Thread(target=tb)
        tha.start(); thb.start()
        tha.join(10); thb.join(10)
        states = {k: v[1] for k, v in out.items()}
        check("11a 竞态收编：两次都 confirmed（旧实现 B 被代确认后报 queued）",
              states.get("a") == "confirmed" and states.get("b") == "confirmed",
              str(states))
        ida, idb = out["a"][0]["id"], out["b"][0]["id"]
        order = [k for k in mock.store if k.startswith("race-")]
        check("11b 确认与投影顺序都落 B（中央与本地一致）",
              order == [ida, idb] and projection == [ida, idb],
              f"order={order} projection={projection}")
        check("11c spool 清空无残留", cr._read_spool("race") == [])

    srv.shutdown()
    failed = [n for n, c in results if not c]
    print(f"\n{'='*40}\n{len(results)-len(failed)}/{len(results)} 过" +
          (f"，挂：{failed}" if failed else "，全绿"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
