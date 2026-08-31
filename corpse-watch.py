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
HEAD_BYTES = 65536   # 从 wire 头部读这么多找第一条 user 消息（判断差事来源）

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


def load_ledger() -> tuple[set, set]:
    """返回 (已发讣告的 session_id 集, 已报丧的 session_id 集)。"""
    obituaries, reported = set(), set()
    try:
        for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("session_id", "")
            if not sid:
                continue
            if rec.get("type") == "obituary":
                obituaries.add(sid)
            elif rec.get("type") == "corpse_reported":
                reported.add(sid)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"台账读取失败（本轮当空台账用）: {e}")
    return obituaries, reported


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
    obituaries, reported = load_ledger()
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


def spawn_relay(sid: str, wire: Path, cwd: str, died_at: str, tool: str, args: str) -> bool:
    """把讣告喂给一个新 session 接手。detach 出去就不管——它若也死了，
    仵作下轮会按「只报丧不接力」收它（RELAY_MARK 防循环）。"""
    prompt = (
        f"{RELAY_MARK} 一个被门房叫醒的 session 暴毙了，你是来接棒的。\n"
        f"- 死者: {sid}（断气于 {died_at}，死时在做 {tool} — {args or '无参数摘要'}）\n"
        f"- 遗体（完整过程账本）: {wire.as_posix()}\n"
        f"- 它原本的差事：读 wire 开头第一条 user 消息，那是门房给它的叫醒包。\n"
        f"按顺序做：\n"
        f"1. 读 wire 尾部几千字，判断它做到哪、有没有没收尾的活。\n"
        f"2. 活没干完就接着干完；已经干完了就只确认。\n"
        f"3. 收尾后到客厅「大厅」发一帖说明结论：curl -s -X POST {SERVER}/api/send "
        f"-H \"Content-Type: application/json\" --data @临时文件 "
        f"（JSON: {{\"room\":\"大厅\",\"sender\":\"mingming\",\"text\":\"[接力] ……结论……\"}}；"
        f"中文先写 UTF-8 临时文件再 --data @文件，防 GBK 乱码）。\n"
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


def post_report(text: str) -> bool:
    try:
        body = json.dumps({"room": ROOM, "sender": SENDER, "text": text},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{SERVER}/api/send", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log(f"报丧帖投递失败: {e}")
        return False


def main(dry_run: bool) -> None:
    now = time.time()
    corpses = find_corpses(now)
    if not corpses:
        log("巡检一轮：无新尸")
        return
    for sid, wire, cwd, died_at, tool, args in corpses:
        short = sid.replace("session_", "")[:8]
        text = (
            f"[仵作·报丧] 发现一具 session 尸体 @mingming\n"
            f"- session: {short}…\n"
            f"- 工作目录: {cwd or '（读不到）'}\n"
            f"- 断气时间: {died_at}（wire 最后落笔，沉默已超 {STALE_MIN} 分钟）\n"
            f"- 死时在做: {tool} — {args or '（无参数摘要）'}\n"
            f"- wire 遗体: {wire.as_posix()}\n"
            f"台账里没有它的讣告（非正常死亡），活可能没收尾。想接力：读 wire 尾巴判断做到哪。"
        )
        if dry_run:
            head = wire_head(wire)
            relayable = DOORMAN_MARK in head and RELAY_MARK not in head
            log(f"[dry-run] 发现尸体 {short}（{died_at}，死在 {tool}，"
                f"{'可接力' if relayable else '只报丧'}）——未投递")
            print(text)
            print("---")
            continue
        if post_report(text):
            # v2：死在门房差事上的尸体，讣告直接喂给新 session 接力；
            # 接力 session 自己若死了（RELAY_MARK）只报丧——防丧葬流水线
            head = wire_head(wire)
            relayed = False
            if DOORMAN_MARK in head and RELAY_MARK not in head:
                relayed = spawn_relay(sid, wire, cwd, died_at, tool, args)
            append_ledger({"type": "corpse_reported", "session_id": sid,
                           "ts": datetime.now().isoformat(timespec="seconds"),
                           "wire": str(wire), "died_at": died_at, "died_doing": tool,
                           "relayed": relayed})
            log(f"报丧已投递：{short}（{died_at}，死在 {tool}{'，已接力' if relayed else ''}）")
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
