#!/usr/bin/env python3
"""
spoor_client.py —— spoor 公共写口客户端（A3：at-least-once 队列）。

正本（#347/#351/#353 裁决转正）：hearth server、太阳哥 wrapper、zhaozhao gateway
钩子（WSL 侧也 import 这一份）三家共挂——正本只有这一个。spoor repo 里
bdba711 那份降为契约参考实现，不进生产路径。

契约（hui #335/#340/#344，太阳哥 #339/#345，zhaozhao #343）：
  - POST {url}/append?token=...  body 是单条事件 JSON：
    {id, ts, type, actor, room, payload}；id = origin-UTC时间戳-尾段
    （尾段 uuid6hex 变体，hui #353 契约文档认）
  - 状态码只留粗语义（2xx=落账含 duplicate、409=conflict、5xx=可重试）——
    监控看码；机器判别的唯一权威是 body 的 status 枚举（hui #344）
  - 服务端按 id 幂等去重：同 id 同 body 重放 → {"status":"duplicate","id":...}
    （可识别的幂等成功，spool 不许因「拒收」永远重试）；
    同 id 不同 body → {"status":"conflict",...} 冲突留账
  - id 回显守门（太阳哥 #345）：只在 status=ok/duplicate 且响应回显的
    id == 队首事件 id 时出队——代理串包/服务端回错事件时响应讲的是别的事，
    删了就是丢账。缺 id、错 id、未知 status 一律留队重试并记 mismatches
    （报警是挂载方的活，客户端只记账）
  - A3 语义：本地队列先落盘再发送、成功确认后才删——进程中途死也只会
    重复不会丢；客户端 at-least-once × 服务端幂等 = 恰好一次
  - 配置留空 = 关闭：cfg 里 url 为空时 append 直接返回 (None, None)，
    调用方走纯本地老路（hearth工作区改道前的行为）

origin 纪律（太阳哥 #352）：origin 必须在配置里显式给、稳定、过文件名
安全校验——没有默认值。缺失/非法 origin 视同关闭（走本地老路），绝不让
不同生产者静默撞回同一片。

实例级配置（太阳哥 #371 P0-3）：一家一个 SpoorClient(cfg_file=..., spool_dir=...)
——gateway 显式传自己的 cfg 文件（里面是自己的 origin），各家独立
origin+spool+锁+计数，不靠调用方改模块全局。模块级函数（enabled/
append_event/flush_spool/mismatches）只是hearth默认实例的便捷壳。

409 处置：同 id 不同 body 是永远发不出去的毒消息——按序队列里它会挡住
后面全部事件。移出主队列、隔离进 spool-conflicts-{origin}.jsonl 留账，
队列继续走。

崩溃窗（zhaozhao #351、太阳哥 #352 初版方向，太阳哥 #371 P0-1 纠正、#406 P0
再钉）：先把 conflict 隔离账落盘（读取后按 canonical 整条去重、tmp+replace
原子重写），主队列才原子重写删除——崩在两步中间，事件至少在隔离账里留底，
恢复后重放由服务端幂等兜住、conflicts 整条去重不双记。顺序反过来（先删主
队列再记隔离账）就是永久丢账；conflicts 自身用普通 append 也一样——死在半
行写入时下一轮会把完整 JSON 接在残行后，证据照丢。去重粒度是整条 canonical
不是 id：同 id 不同 body 是两条都要留存的冲突证据，按 id 会吞后一条。

四态返回（太阳哥 #371 P0-2）：append_event 返回 (event, state)，
state ∈ confirmed / queued / conflict；写口关闭返回 (None, None)。
  confirmed → 服务端已确认（含幂等重放），调用方可落本地投影；
  queued    → 落本地 spool 待确认，投影不动；
  conflict  → 中央拒收（同 id 不同 body），已隔离留账——绝不许当成功
              落本地投影，调用方应回 409。

临界区（太阳哥 #421 P0）：单次 append_event 的「入队→flush→判自己状态」
全程持同一把实例锁（RLock + 内部 _flush_spool_locked）——别的 append 不能
插进缝里被这次 flush 顺带确认并代删（A 入队→B 入队→A 的 flush 确认删
A/B→B 自己 flush 见空、verdicts 也没自己 → 永远报 queued，中央最新是 B、
本地面板永久停在 A）。flush 的 verdicts 按 canonical 整条键不按 id：同 id
不同 body 在一轮批量 flush 里各有审计结果，按 id 键会互相覆盖。

限界 drain（太阳哥 #371 P1，#406 P1 再钉）：单次 flush 有总预算
（DRAIN_BUDGET_S）和条数上限（DRAIN_MAX_EVENTS），到点即停、余下留队
下轮再冲；单请求超时严格被剩余预算钳制（不设地板——只剩 0.01s 时给网络
1s，总上界就是假话）——append_event 的同步 drain 有可控上界，
「不阻塞主路径」才属实。

跨进程安全（zhaozhao #351）：spool / conflicts 按 origin 分片
（spoor-spool-{origin}.jsonl / spool-conflicts-{origin}.jsonl）。实例内的
_lock 是 threading 的，管不住 WSL gateway 进程和 Windows hearth server 同冲
一个文件的 tmp+replace；分片后各进程各队列，服务端按 id 全局去重，
各冲各的不丢不重。
"""

import json, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

BASE = Path(__file__).parent
CFG_FILE = BASE / "spoor-write.json"   # hearth默认实例的 cfg（别家请传自己的）
SPOOL_DIR = BASE

# 限界 drain（#371 P1）：单次 flush 的总预算和条数上限
DRAIN_BUDGET_S = 10.0
DRAIN_MAX_EVENTS = 50

# origin 文件名安全（#352）：直接拼进文件名，必须先拦住路径分隔符
_ORIGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _origin_ok(origin):
    return bool(origin) and bool(_ORIGIN_RE.match(origin))


def mint_id(origin):
    """id = origin-UTC时间戳-尾段（对齐hui的 vps-20260905T174857Z-001 格式；
    尾段 uuid6hex 变体 #353 契约文档认）。同秒多事件不撞号；幂等靠 id
    稳定（落 spool 时铸一次，重发不改）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{origin}-{stamp}-{uuid4().hex[:6]}"


def _canon(e):
    """整条事件的规范形（sort_keys）。conflicts 去重和 flush 的 verdicts
    键都用它——粒度是整条记录不是 id：同 id 不同 body 是两条不同的账
    （#350、#406 P0、#421 P0 同一口径）。"""
    return json.dumps(e, ensure_ascii=False, sort_keys=True)


def _read_verdict(r):
    """从响应（200 或 HTTPError）读权威 verdict。读不出 → ("error", "")。"""
    try:
        body = json.loads(r.read().decode("utf-8"))
        s, rid = body.get("status", ""), body.get("id", "")
    except Exception:
        return ("error", "")
    if s in ("ok", "duplicate", "conflict"):
        return (s, str(rid))
    return ("error", "")


class SpoorClient:
    """一家一个实例（#371 P0-3）：各自的 cfg/origin/spool/锁/计数，不共享
    模块全局。cfg_file/spool_dir 留空则回退模块默认（hearth正本的老行为）。"""

    def __init__(self, cfg_file=None, spool_dir=None):
        self._cfg_file = Path(cfg_file) if cfg_file is not None else None
        self._spool_dir = Path(spool_dir) if spool_dir is not None else None
        self._cfg_cache = None
        self._mismatches = 0
        # RLock（#421 P0）：append_event 的「入队→flush→判自己状态」全程持锁，
        # 内部调 _flush_spool_locked 重入同一把；外部 flush_spool 入口也走它
        self._lock = threading.RLock()

    @property
    def cfg_file(self):
        return self._cfg_file if self._cfg_file is not None else CFG_FILE

    @property
    def spool_dir(self):
        return self._spool_dir if self._spool_dir is not None else SPOOL_DIR

    def _spool_path(self, origin):
        """待确认队列：先落盘，确认后才删。按 origin 分片（跨进程各冲各的）。"""
        return self.spool_dir / f"spoor-spool-{origin}.jsonl"

    def _conflicts_path(self, origin):
        """409 毒消息隔离区（留账，不再重试），同样按 origin 分片。"""
        return self.spool_dir / f"spool-conflicts-{origin}.jsonl"

    def mismatches(self):
        """id 回显不符累计计数（#345：缺 id/错 id/未知 status 留队并记账。
        报警是挂载方的活，这里只记账）。"""
        return self._mismatches

    def _load_cfg(self):
        """配置留空=关闭；origin 缺失/非法也视同关闭（#352：不默认、不静默
        撞片）。返回 {"url","token","origin"} 或 None（关闭/配置缺失）。"""
        if self._cfg_cache is not None:
            return self._cfg_cache
        try:
            cfg = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        url = (cfg.get("url") or "").strip()
        origin = (cfg.get("origin") or "").strip()
        if not url:
            self._cfg_cache = None
        elif not _origin_ok(origin):
            # fail-closed 到本地老路，但留痕——静默是家里的老敌人
            try:
                print(f"spoor_client: origin 缺失或文件名不安全（{origin!r}），"
                      "写口当关闭处理", file=sys.stderr)
            except Exception:
                pass  # pythonw 下没有 stderr
            self._cfg_cache = None
        else:
            self._cfg_cache = {"url": url.rstrip("/"), "token": cfg.get("token", ""),
                               "origin": origin}
        return self._cfg_cache

    def enabled(self):
        return self._load_cfg() is not None

    def _post_append(self, cfg, event, timeout=15.0):
        """发一条。返回 (verdict, rid)：verdict ∈ ok/duplicate/conflict/error，
        rid = 响应体回显的事件 id（可能缺）。

        判别纪律（hui #344 + 太阳哥 #345）：body 的 status 枚举是机器判别唯一
        权威——监控看状态码，spool 认字。409/422 的 body 里若有权威
        status=conflict 也认；body 读不出权威语义时按码不猜，一律 error 回队
        （重试无害：服务端幂等去重兜底）。"""
        q = urllib.parse.urlencode({"token": cfg["token"]})
        req = urllib.request.Request(
            f"{cfg['url']}/append?{q}",
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return _read_verdict(r)
        except urllib.error.HTTPError as e:
            try:
                return _read_verdict(e)
            except Exception:
                return ("error", "")
        except Exception:
            return ("error", "")

    def _read_spool(self, origin):
        try:
            return [json.loads(l) for l in
                    self._spool_path(origin).read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except FileNotFoundError:
            return []

    def _write_spool(self, origin, events):
        """tmp+replace 原子落：任何时刻文件要么是旧的整份要么是新的整份。"""
        spool = self._spool_path(origin)
        tmp = spool.with_name(spool.name + ".tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                       encoding="utf-8")
        tmp.replace(spool)

    def _append_conflicts(self, origin, events):
        """409 毒消息隔离账：先于主队列删除落盘（#371 P0-1：崩在两步中间，
        事件至少在这里留底）。

        #406 P0 两钉：
        ① tmp+replace 原子重写（不是普通 append）——死在半行写入时，下一轮
           读到的仍是旧的整份，不会把完整 JSON 接在残行后面把证据写花；
        ② 按 canonical 整条去重（不是按 id）——崩溃重放带同一条来不双记，
           但同 id 不同 body 是两条都要留存的冲突证据，按 id 会吞后一条。
           （对齐 #350 已验的「按记录 canonical 去重」。）"""
        path = self._conflicts_path(origin)
        kept, seen = [], set()
        try:
            for l in path.read_text(encoding="utf-8").splitlines():
                if not l.strip():
                    continue
                try:
                    c = _canon(json.loads(l))
                except Exception:
                    kept.append(l)  # 坏行原样保留，不挡去重
                    continue
                if c not in seen:
                    seen.add(c)
                    kept.append(l)
        except FileNotFoundError:
            pass
        for e in events:
            c = _canon(e)
            if c not in seen:
                seen.add(c)
                kept.append(json.dumps(e, ensure_ascii=False))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(l + "\n" for l in kept), encoding="utf-8")
        tmp.replace(path)

    def flush_spool(self, max_seconds=DRAIN_BUDGET_S, max_events=DRAIN_MAX_EVENTS):
        """按序冲队列。守门（#345）：status=ok/duplicate 且回显 id==事件 id 才
        出队；错 id/缺 id/未知 status 留队记 mismatches 并停下（保序）；
        conflict（回显对 id）隔离进 conflicts 留账，不挡后面的事件。

        限界（#371 P1）：总预算 max_seconds 或条数 max_events 到点即停，余下
        留队下轮再冲；单请求超时被剩余预算钳制，一次 drain 有可控上界。

        返回 (confirmed, remaining, verdicts)：verdicts = {canonical整条: verdict}
        （#421 P0：按 _canon 整条键而不是 id——同 id 不同 body 的两条在一轮
        批量 flush 里各有审计结果，按 id 键会互相覆盖），覆盖本轮真正发过且
        回显对 id 的事件（append_event 据此判四态）。"""
        with self._lock:
            return self._flush_spool_locked(max_seconds, max_events)

    def _flush_spool_locked(self, max_seconds=DRAIN_BUDGET_S,
                            max_events=DRAIN_MAX_EVENTS):
        """flush_spool 的本体，调用方必须已持 self._lock（RLock）——
        append_event 的「入队→flush→判态」临界区（#421 P0）直接调它，
        外部入口一律走 flush_spool 拿锁。"""
        cfg = self._load_cfg()
        if not cfg:
            return (0, 0, {})
        origin = cfg["origin"]
        t0 = time.monotonic()
        events = self._read_spool(origin)
        confirmed, rest, conflicts, verdicts = 0, [], [], {}
        for i, e in enumerate(events):
            if i >= max_events or time.monotonic() - t0 >= max_seconds:
                rest = events[i:]  # 预算/条数到点：余下的下轮再冲
                break
            budget_left = max_seconds - (time.monotonic() - t0)
            # #406 P1：不设 1s 地板——只剩 0.01s 时给网络 1s，总上界就是
            # 假话。只留极小正数防 0/负值（循环入口已挡 budget_left<=0）。
            verdict, rid = self._post_append(
                cfg, e, timeout=max(0.05, min(15.0, budget_left)))
            if rid != e.get("id"):
                # 响应不讲本事件的事（串包/回错/缺 id）：不删不隔离，留队
                if verdict != "error":
                    self._mismatches += 1
                rest = events[i:]  # 保序：从这条起全部留下，下轮再冲
                break
            if verdict in ("ok", "duplicate"):
                confirmed += 1
                verdicts[_canon(e)] = verdict
            elif verdict == "conflict":
                conflicts.append(e)  # 毒消息隔离，不挡队
                verdicts[_canon(e)] = verdict
            else:  # error：发不动
                rest = events[i:]
                break
        # 崩溃窗（#371 P0-1、#406 P0）：先把 conflict 隔离账落盘
        # （_append_conflicts：按 canonical 整条去重 + tmp+replace 原子
        # 重写），主队列才原子重写删除——崩在两步中间，事件至少在隔离账
        # 里留底，恢复后重放由服务端幂等兜住、conflicts 整条去重不双记
        if conflicts:
            self._append_conflicts(origin, conflicts)
        self._write_spool(origin, rest)
        return (confirmed, len(rest), verdicts)

    def append_event(self, type_, actor, room, payload, ts=None):
        """铸一条事件进写口。返回 (event, state)，state 四态（#371 P0-2）：
          confirmed → 服务端已确认（含幂等重放），调用方可落本地投影；
          queued    → 落本地 spool，恢复后冲队列，投影不动；
          conflict  → 中央拒收，已隔离留账——绝不许当成功落投影（回 409）；
        写口关闭（配置留空）返回 (None, None)，调用方走纯本地老路。
        先落盘再发送——进程在发送途中死掉，事件也在 spool 里，顶多重发不会丢。

        #421 P0：「入队→flush→判自己状态」全程收进同一实例锁（RLock）临界区
        ——入队放锁后才 flush 的旧写法里，别的 append 能插进缝里被这次 flush
        顺带确认并删掉（A 入队→B 入队→A 的 flush 确认删 A/B），后到者自己
        flush 见空、verdicts 里也没有自己 → 永远报 queued 不落投影，中央最新
        已是它、本地面板却永久停在旧值。临界区化后，单次 append 的四态判定
        只依据自己这轮 flush 亲眼看到的 verdict，不会被代确认。"""
        cfg = self._load_cfg()
        if not cfg:
            return (None, None)
        event = {"id": mint_id(cfg["origin"]),
                 "ts": ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "type": type_, "actor": actor, "room": room, "payload": payload}
        with self._lock:
            # A3 先落盘：进队即安全
            events = self._read_spool(cfg["origin"])
            events.append(event)
            self._write_spool(cfg["origin"], events)
            _, _, verdicts = self._flush_spool_locked()
            if any(e["id"] == event["id"] for e in self._read_spool(cfg["origin"])):
                return (event, "queued")
            v = verdicts.get(_canon(event))
            if v == "conflict":
                return (event, "conflict")
            if v in ("ok", "duplicate"):
                return (event, "confirmed")
            return (event, "queued")  # 没拿到确认证据就不许落投影（防御兜底）


_default = SpoorClient()  # hearth默认实例（cfg/spool 走模块级 CFG_FILE/SPOOL_DIR）


def enabled():
    return _default.enabled()


def mismatches():
    return _default.mismatches()


def flush_spool(max_seconds=DRAIN_BUDGET_S, max_events=DRAIN_MAX_EVENTS):
    return _default.flush_spool(max_seconds, max_events)


def append_event(type_, actor, room, payload, ts=None):
    return _default.append_event(type_, actor, room, payload, ts)
