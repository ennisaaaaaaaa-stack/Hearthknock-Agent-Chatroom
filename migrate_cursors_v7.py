#!/usr/bin/env python3
"""
v5/v6 → v7 游标台账迁移（太阳哥验收 P0-3：升级迁移断档的整改，2026-08-31）

旧 doorman-state.json 里存着「每房一个」的游标（v5 格式，{room: int}）；
v7 起游标事实源在 chat.db 的 cursors 表（每房每成员一行）。v7 重启只建空表，
随后门房 state.pop('cursors')——不迁就直接全房 recovery_required。

规矩：
  - 先备份（chat.db 和 doorman-state.json 各复制一份带时间戳的 .bak）
  - 单事务原子迁入：任何一步失败整体回滚，不留半成品
  - 旧值越界（< 0 或 > 房内最大消息 id）= 数据不一致：不猜，停机报警
  - 覆盖校验：迁完后每个 open 房的应覆成员（限定房=房内成员，开放房=花名册全员）
    必须人人有行；缺一个就回滚停机报警——宁可上不了线，不静默断档
  - 覆盖校验过了，同事务落 migration_ready 标记——启动闸的门票和台账同生同死，
    不留「迁完了但标记靠 server 自愈」的时间窗
  - 已迁过（cursors 表非空）直接拒绝运行，防重复迁移把新台账覆盖回旧值
  - closed 房不迁（终态，门房不看）；closed 房的旧游标留在 .bak 里可查

用法: python migrate_cursors_v7.py          # 在 chatroom 目录下跑
跑完确认输出「迁移完成」再启动 v7 server / doorman。
"""

import json, shutil, sqlite3, sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "chat.db"
STATE = BASE / "doorman-state.json"
ROSTER = BASE / "roster.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors(
    room TEXT NOT NULL, member TEXT NOT NULL,
    cursor INTEGER NOT NULL, start_cursor INTEGER NOT NULL,
    delivery_id TEXT, updated_ts TEXT,
    PRIMARY KEY(room, member));
CREATE TABLE IF NOT EXISTS cursor_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT, member TEXT, kind TEXT, reason TEXT, actor TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def fail(msg):
    print(f"✗ 迁移中止：{msg}", file=sys.stderr)
    print("  数据库已回滚/未动，备份在旁边。修好原因再跑。", file=sys.stderr)
    sys.exit(1)


def main():
    if not DB.exists():
        fail("chat.db 不存在")
    if not STATE.exists():
        fail("doorman-state.json 不存在——没有旧游标可迁（如果是全新部署，不需要跑这个脚本）")

    old = json.loads(STATE.read_text(encoding="utf-8")).get("cursors") or {}
    if not isinstance(old, dict) or not old:
        fail("doorman-state.json 里没有 cursors（或已迁过被 v7 清空）")
    # type(v) is int 而不是 isinstance——bool 是 int 的子类，True/False 会被
    # isinstance 放过去当成游标 1/0 迁进台账
    if not all(type(v) is int for v in old.values()):
        fail("旧 cursors 不是 {room: int} 的 v5 格式（bool 也不算）——格式不认识，不猜，停机")

    roster = json.loads(ROSTER.read_text(encoding="utf-8")).get("agents", [])
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 备份：动台账前先留底
    shutil.copy(DB, DB.with_name(f"chat.db.bak-{ts}"))
    shutil.copy(STATE, STATE.with_name(f"doorman-state.json.bak-{ts}"))
    print(f"备份落盘：chat.db.bak-{ts} / doorman-state.json.bak-{ts}")

    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    # 「已迁过」的事实源是 meta.migration_ready 标记，不是 cursors 表空不空——
    # v7 server 上岗后新建房/新成员 join 会生出合法的 birth 行，表非空≠已迁过
    if conn.execute("SELECT value FROM meta WHERE key='migration_ready'").fetchone():
        conn.close()
        fail("已迁过（meta.migration_ready 已落账）——拒绝重复迁移（会把新台账覆盖回旧值）")

    rooms = {r[0]: {"members": json.loads(r[1]), "state": r[2]}
             for r in conn.execute("SELECT room,members,state FROM rooms").fetchall()}
    max_id = {r[0]: r[1] for r in conn.execute(
        "SELECT room, COALESCE(MAX(id),0) FROM messages GROUP BY room").fetchall()}

    try:
        with conn:  # 单事务：任何异常整体回滚
            migrated, skipped = [], []
            for room, cur in sorted(old.items()):
                if room not in rooms:
                    fail(f"旧游标的房「{room}」在 chat.db 里不存在——数据不一致，停机")
                if rooms[room]["state"] == "closed":
                    skipped.append(room)
                    continue  # closed 是终态，门房不看，旧值留在 .bak 里
                mx = max_id.get(room, 0)
                if not 0 <= cur <= mx:
                    fail(f"「{room}」旧游标 {cur} 越界（合法范围 0..{mx}，房内最大消息 id {mx}）"
                         "——会跳过或重放真实消息，不猜，停机")
                targets = rooms[room]["members"] or roster  # 限定房=房内成员，开放房=花名册全员
                for m in targets:
                    try:
                        conn.execute("""INSERT INTO cursors(room,member,cursor,start_cursor,updated_ts)
                                        VALUES(?,?,?,?,?)""", (room, m, cur, cur, now))
                    except sqlite3.IntegrityError:
                        # 该行已在（v7 上岗后出生的 birth 行）：旧值不能盖新行，不猜，停机
                        fail(f"「{room}/{m}」已有 cursor 行（v7 上岗后出生的新行）"
                             "——旧游标不能覆盖它，停机人工对账")
                conn.execute("""INSERT INTO cursor_events(room,member,kind,reason,actor,ts)
                                VALUES(?,?,?,?,?,?)""",
                             (room, "*", "migrate",
                              f"旧门房单游标 {cur} 迁入 {len(targets)} 个成员行", "migrate_cursors_v7", now))
                migrated.append((room, cur, len(targets)))

            # 覆盖校验：每个 open 房的应覆成员必须人人有行，缺一个就回滚
            gaps = []
            for room, info in rooms.items():
                if info["state"] != "open":
                    continue
                targets = info["members"] or roster
                have = {r[0] for r in conn.execute(
                    "SELECT member FROM cursors WHERE room=?", (room,)).fetchall()}
                for m in targets:
                    if m not in have:
                        gaps.append(f"{room}/{m}")
            if gaps:
                fail("覆盖校验失败，这些 (房/成员) 迁完后仍缺 cursor 行：\n  " + "\n  ".join(gaps))

            # 覆盖校验过了，同事务落 migration_ready 标记——启动闸的门票和台账
            # 同生同死，不留「迁完了但标记靠 server 自愈」的时间窗
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('migration_ready',?)", (now,))
    except SystemExit:
        raise
    except Exception as e:
        fail(f"事务异常（已回滚）: {e}")
    finally:
        conn.close()

    print(f"迁移完成：{len(migrated)} 房迁入，{sum(n for _, _, n in migrated)} 行落账")
    for room, cur, n in migrated:
        print(f"  「{room}」游标 {cur} → {n} 个成员行")
    if skipped:
        print(f"  跳过 closed 房（终态不巡）: {'、'.join(skipped)}")
    print("可以启动 v7 server / doorman 了。")


if __name__ == "__main__":
    main()
