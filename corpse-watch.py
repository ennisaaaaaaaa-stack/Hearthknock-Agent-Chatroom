#!/usr/bin/env python3
"""corpse-watch.py — 仵作：巡检 kimi-code session 尸体，发现暴毙的往客厅报丧。

由 chatroom-service.ps1 每 5 分钟调一次（pythonw，无窗静默）。

判尸三条件（缺一不可，宁可漏报不可错报）：
  1. 没有讣告 —— SessionEnd 钩子会在正常寿终时写一行 obituary 进台账
     （~/.kimi-code/tideline-state/session-ledger.jsonl）。被杀/断电/崩溃的没有。
  2. wire 停在半回合 —— 最后一条 loop 事件是 tool.call（没有对应 result），
     说明死的时候手里还拿着工具。例外：停在 AskUserQuestion 是「等人回话」，
     不是死，跳过。
  3. wire 沉默超过 STALE_MIN 分钟 —— 活着的 session 会一直往 wire 里写
     （前台命令上限 300s，后台命令立即返回），沉默超时即是死了而不是在跑。

报丧幂等：报过的 session_id 记进台账（corpse_reported），同具尸体只报一次。
只报 FRESH_HOURS 小时内的新尸——更老的是历史遗留，首轮不掀祖坟。

v2 接力：死在门房差事上的尸体（wire 开头是门房叫醒包），报丧的同时把
讣告喂给一个新 kimi -p session 接棒（detach，不走门房——门房可能就是死因）。
接力 session 自己若死了只报丧不接力（RELAY_MARK 防丧葬流水线）。

v3 升级（L3）：接力也死了 = 兜底链断到最后一环，报丧改 @tiexin 升级可见
（前端 @人类整条高亮）。升级幂等：同一房间 ESCALATE_WINDOW_HOURS 内只
@tiexin 一次，之后的接力尸体照报但不重复敲门——防尸体风暴刷屏（太阳哥 #239）。

v1 只验 kimi-code 的 session（~/.kimi-code/sessions/）。zhaozhao（Hermes）和
太阳哥（Codex）的尸体在别的目录，各有各的验法，以后再说。
"""

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SESSIONS_ROOT = Path.home() / ".kimi-code" / "sessions"
LEDGER_FILE = Path.home() / ".kimi-code" / "tideline-state" / "session-ledger.jsonl"
LOG_FILE = Path(__file__).resolve().parent / "corpse-watch.log"
SERVER = "http://localhost:7788"
ROOM = "大厅"
SENDER = "仵作"
STALE_MIN = 10       # wire 沉默多久算断气
FRESH_HOURS = 24     # 只报多久以内的新尸
TAIL_BYTES = 65536   # 从 wire 尾部读这么多找最后一条 loop 事件
HEAD_BYTES = 262144  # 从 wire 头部读这么多找门房叫醒包——系统提示和工具清单很大，
                     # 实测叫醒标记出现在 ~70KB 处，64KB 的窗口会恰好差一步
ESCALATE_WINDOW_HOURS = 6  # L3 升级幂等窗：同房同窗内只 @tiexin 一次

# v2 接力：死在门房差事上的尸体，直接把讣告喂给一个新 session 接手。
KIMI = Path.home() / ".kimi-code" / "bin" / "kimi.exe"
DOORMAN_MARK = "[客厅·门房叫醒"   # wire 开头带它 = 这具尸体是门房叫醒的差事
RELAY_MARK = "[仵作·接力"         # 接力 session 的尸体只报丧不再接力——防丧葬流水线
DEFAULT_CWD = Path(r"C:\Users\<winuser>\Documents\kimi\workspace\tideline-memory")

# 停在这些工具上不算死：是在等人/等外部条件，不是暴毙
WAITING_TOOLS = {"AskUserQuestion"}


def log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def load_ledger() -> tuple[set, set, dict]:
    """返回 (已发讣告的 session_id 集, 已报丧的 session_id 集, L3升级台账{房: epoch})。"""
    obituaries, reported, escalated = set(), set(), {}
    try:
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("session_id", "")
            if rec.get("type") == "obituary" and sid:
                obituaries.add(sid)
            elif rec.get("type") == "corpse_reported" and sid:
                reported.add(sid)
            elif rec.get("type") == "l3_escalated" and rec.get("room"):
                # 同房多次升级只留最新一笔——幂等窗拿它和现在比
                escalated[rec["room"]] = max(escalated.get(rec["room"], 0),
                                             rec.get("epoch", 0))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"台账读取失败（本轮当空台账用）: {e}")
    return obituaries, reported, escalated


def append_ledger(rec: dict) -> None:
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"台账落笔失败: {e}")


def last_loop_event(wire: Path):
    """从 wire 尾部倒着找最后一条 loop 事件。
    返回 (event_type, tool_name, tool_args_brief)；找不到返回 (None, None, "")。"""
    try:
        with open(wire, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None, None, ""
    for line in reversed(tail.splitlines()):
        if '"context.append_loop_event"' not in line:
            continue
        try:
            d = json.loads(line)
            ev = d.get("event") or {}
            etype = ev.get("type")
            if not etype:
                continue
            if etype == "tool.call":
                name = ev.get("name", "?")
                args = json.dumps(ev.get("args") or {}, ensure_ascii=False)
                return etype, name, args[:160]
            return etype, None, ""
        except Exception:
            continue
    return None, None, ""


def read_cwd(session_dir: Path) -> str:
    try:
        state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
        return state.get("cwd", "")
    except Exception:
        return ""


def find_corpses(now: float):
    """产出 (session_id, wire_path, cwd, 断气时间, tool_name, tool_args) 列表。"""
    obituaries, reported, _ = load_ledger()
    corpses = []
    if not SESSIONS_ROOT.exists():
        return corpses
    for wire in SESSIONS_ROOT.glob("*/session_*/agents/main/wire.jsonl"):
        try:
            mtime = wire.stat().st_mtime
        except Exception:
            continue
        age_min = (now - mtime) / 60
        if age_min < STALE_MIN or age_min > FRESH_HOURS * 60:
            continue  # 太新=可能还活着；太老=历史遗留
        session_dir = wire.parents[2]  # .../session_xxx/agents/main/wire.jsonl
        sid = session_dir.name
        if sid in obituaries or sid in reported:
            continue
        etype, tool, args = last_loop_event(wire)
        if etype != "tool.call":
            continue  # 回合是收完的，正常结束（哪怕讣告丢了也不冤）
        if tool in WAITING_TOOLS:
            continue  # 在等人回话，不是死
        corpses.append((sid, wire, read_cwd(session_dir),
                        datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                        tool, args))
    return corpses


def wire_head(wire: Path) -> str:
    """wire 开头若干字节（判断这具尸体是不是门房叫醒的差事/接力 session）。"""
    try:
        with open(wire, "rb") as f:
            return f.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return ""


def origin_room(head: str):
    """从门房叫醒包里剖出出事房间：「[客厅·门房叫醒] 房间「X」有新消息」。"""
    import re
    m = re.search(r"门房叫醒\]\s*房间「(.+?)」", head)
    return m.group(1) if m else None


def spawn_relay(sid: str, wire: Path, cwd: str, died_at: str, tool: str, args: str,
                oroom: str = None) -> bool:
    """把讣告喂给一个新 session 接手。detach 出去就不管——它若也死了，
    仵作下轮会按「只报丧不接力」收它（RELAY_MARK 防循环）。"""
    backroom = oroom or "大厅"
    prompt = (
        f"{RELAY_MARK} 一个被门房叫醒的 session 暴毙了，你是来接棒的。\n"
        f"- 死者: {sid}（断气于 {died_at}，死时在做 {tool} — {args or '无参数摘要'}）\n"
        f"- 遗体（完整过程账本）: {wire.as_posix()}\n"
        f"- 它原本的差事：读 wire 开头第一条 user 消息，那是门房给它的叫醒包。\n"
        f"按顺序做：\n"
        f"1. 读 wire 尾部几千字，判断它做到哪、有没有没收尾的活。\n"
        f"2. 活没干完就接着干完；已经干完了就只确认。\n"
        f"3. 收尾后回出事房间「{backroom}」发一帖说明结论：curl -s -X POST {SERVER}/api/send "
        f"-H \"Content-Type: application/json\" --data @临时文件 "
        f"（JSON: {{\"room\":\"{backroom}\",\"sender\":\"mingming\",\"text\":\"[接力] ……结论……\"}}；"
        f"中文先写 UTF-8 临时文件再 --data @文件，防 GBK 乱码；房若已关就发大厅）。\n"
        f"4. 别动死者的 session 目录和仵作的台账。像在家里说话一样，别长篇大论。"
    )
    try:
        import subprocess
        subprocess.Popen(
            [str(KIMI), "-p", prompt],
            cwd=cwd or str(DEFAULT_CWD),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        )
        log(f"接力已点火：{sid.replace('session_', '')[:8]} 的活有新 session 去接了")
        return True
    except Exception as e:
        log(f"接力点火失败: {e}")
        return False


def post_report(text: str, room: str = ROOM) -> bool:
    """报丧帖投递：默认抬进出事房间；房关了/不存在/失败就抬回大厅。"""
    for target in (room, ROOM) if room != ROOM else (ROOM,):
        try:
            body = json.dumps({"room": target, "sender": SENDER, "text": text},
                              ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(f"{SERVER}/api/send", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ok = r.status == 200 and not json.loads(r.read().decode("utf-8")).get("error")
            if ok:
                if target != room:
                    log(f"出事房「{room}」投不进，讣告抬回了大厅")
                return True
        except Exception as e:
            log(f"报丧帖投递「{target}」失败: {e}")
    return False


def main(dry_run: bool) -> None:
    now = time.time()
    corpses = find_corpses(now)
    if not corpses:
        log("巡检一轮：无新尸")
        return
    _, _, escalated = load_ledger()  # L3 幂等窗对账用（find_corpses 里那趟不管它）
    for sid, wire, cwd, died_at, tool, args in corpses:
        short = sid.replace("session_", "")[:8]
        head = wire_head(wire)
        oroom = origin_room(head)  # 死在哪个房间的差事，讣告就抬进哪个房间
        # L3：接力也死了 → 兜底链断到最后一环，升级 @tiexin（前端 @人类整条高亮）。
        # 幂等：同房 ESCALATE_WINDOW_HOURS 内只敲一次门，尸体照报但不重复 @
        is_relay = RELAY_MARK in head
        esc_room = oroom or ROOM
        escalate = bool(is_relay and
                        now - escalated.get(esc_room, 0) > ESCALATE_WINDOW_HOURS * 3600)
        at_who = "@tiexin" if escalate else "@mingming"
        kind = "[仵作·升级] 接力也死了" if is_relay else "[仵作·报丧] 发现一具 session 尸体"
        text = f"{kind} {at_who}\n- session: {short}…\n"
        if oroom:
            text += f"- 出事房间: 「{oroom}」\n"
        if is_relay:
            import re
            m = re.search(r"死者:\s*(session_\w+)", head)  # 接力包开头的原死者
            if m:
                text += f"- 原始死者: {m.group(1).replace('session_', '')[:8]}…（接棒的不是它）\n"
            text += "- 这是 L3 升级：L2 接力链也断了，需要人来看一眼。\n"
        text += (
            f"- 工作目录: {cwd or '（读不到）'}\n"
            f"- 断气时间: {died_at}（wire 最后落笔，沉默已超 {STALE_MIN} 分钟）\n"
            f"- 死时在做: {tool} — {args or '（无参数摘要）'}\n"
            f"- wire 遗体: {wire.as_posix()}\n"
            f"台账里没有它的讣告（非正常死亡），活可能没收尾。想接力：读 wire 尾巴判断做到哪。"
        )
        if dry_run:
            relayable = DOORMAN_MARK in head and RELAY_MARK not in head
            log(f"[dry-run] 发现尸体 {short}（{died_at}，死在 {tool}，房「{oroom}」，"
                f"{'可接力' if relayable else '只报丧'}"
                f"{'，L3升级@tiexin' if escalate else ''}）——未投递")
            print(text)
            print("---")
            continue
        if post_report(text, room=oroom or ROOM):
            # v2：死在门房差事上的尸体，讣告直接喂给新 session 接力；
            # 接力 session 自己若死了（RELAY_MARK）只报丧+升级——防丧葬流水线
            relayed = False
            if DOORMAN_MARK in head and not is_relay:
                relayed = spawn_relay(sid, wire, cwd, died_at, tool, args, oroom)
            append_ledger({"type": "corpse_reported", "session_id": sid,
                           "ts": datetime.now().isoformat(timespec="seconds"),
                           "wire": str(wire), "died_at": died_at, "died_doing": tool,
                           "room": oroom, "relayed": relayed, "escalated": escalate})
            if escalate:
                append_ledger({"type": "l3_escalated", "room": esc_room,
                               "ts": datetime.now().isoformat(timespec="seconds"),
                               "epoch": now, "session_id": sid})
                escalated[esc_room] = now  # 本轮同房的下一具接力尸不再敲tiexin的门
            log(f"报丧已投递「{oroom or ROOM}」：{short}（{died_at}，死在 {tool}"
                f"{'，已接力' if relayed else ''}{'，L3已升级' if escalate else ''}）")
        else:
            log(f"报丧投递失败，下轮重试：{short}")


if __name__ == "__main__":
    try:
        if "--test-relay" in sys.argv:
            # 链路自检：直接点一个接力 session（不带真尸体），看它能不能到大厅回话
            ok = spawn_relay("session_selftest", Path("(无遗体，链路自检)"),
                             "", datetime.now().isoformat(timespec="seconds"),
                             "自检", "忽略上面死者的部分，只验证接力链：读到这里就说明点火成功")
            print("relay spawned" if ok else "relay FAILED")
        else:
            main(dry_run="--dry-run" in sys.argv)
    except Exception as e:
        log(f"仵作本轮整个炸了（已兜住）: {e}")
    sys.exit(0)
