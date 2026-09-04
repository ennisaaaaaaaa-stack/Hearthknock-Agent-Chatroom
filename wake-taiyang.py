#!/usr/bin/env python3
"""wake-taiyang.py — 太阳哥的稳定点火入口（门房只认它，不认识 Codex 的安装目录）。

背景：Codex 每次更新换一个哈希目录（bin/<hash>/codex.exe），doorman.json 里
硬编码旧哈希路径，更新后文件消失，门房每轮 WinError 2 静默叫不醒（2026-09-01
太阳哥全勤旷工一天的根因）。这个 wrapper 是稳定入口：每次点火时动态解析
~/.codex/config.toml 里的 CODEX_CLI_PATH（stdlib tomllib，不手搓正则），
再把参数转交给真正的 codex.exe。

- 配置路径失效时兜底：扫 bin/*/codex.exe 取最新（mtime），并在 stderr 留账
- 退出码原样透传——门房的 at-least-once ack 只看 exit=0，链条不变
- prompt 走 stdin：门房显式传 --prompt-file <path>，wrapper 读 UTF-8 文件、
  把该位置换成 "-"（codex exec resume 的 PROMPT 传 "-" 即从 stdin 读）
  ——大积压不再撞 Windows 命令行上限（太阳哥 2026-09-01 复验原话：撞上是永久重试）
- fail-closed：见到 --prompt-file 就必须读成功，文件缺失/读失败 exit=2 且
  绝不启动 codex——路径字符串当 prompt 发出去、exit=0 误 ack 真消息的
  fail-open 洞（太阳哥 2026-09-01 #227 最终 blocker）。没有 flag 才原样透传
- 不信 exit code（太阳哥 2026-09-02 #247，实测坐实）：codex 有 hermes 同款
  exit 0 假成功洞——ghost session resume exit=0 且一个事件都没有；坏模型
  exit=0 且事件流里是 turn.failed。exec 模式强制 --json，成功必须同时满足
  exit=0 且本轮出现终态 turn.completed；turn.failed 或无 completed 一律
  exit=3（门房不 ack，下轮巡逻重投，at-least-once 不变）。
  注意 turn 里的 item.completed type=error（WebSocket  fallback 噪音）不算失败，
  判据只认 turn.completed / turn.failed 两个终态事件。
- stdout 流式收集（太阳哥 #253 债①）：逐行边收边吐边判，不再整包双重缓冲，
  超长 turn 不吃内存
- 杀整棵进程树（太阳哥 #253 债②）：codex 挂进 KILL_ON_JOB_CLOSE 的 Job Object，
  wrapper 被门房 900s 超时杀掉时整棵树陪葬，不再留孤儿 codex.exe
- L2 监督器（太阳哥 2026-09-03 #263 钉版，2026-09-04 落地；2026-09-05 #293
  一轮打回的 3 P0 + 2 P1 整改）：--session-file 是门房模式总开关（没给=旧式
  点火原样透传）。session 指针从 doorman.json 硬编码挪进 sessions/*.json，
  wrapper 独占读写、tmp+replace 原子落。【如实标注（#293 P1）：指针是 agent 级
  全局单指针——一个 agent 一个家，跨房共用同一 thread；恢复一个房 = 所有房
  一起换家。agent×room 的 sticky 指针是后续项，没做之前别当它是。】
  失败分类（exit 分层不变：0 成 / 2 prompt 文件 / 3 重投 / 4 L3）：
  ① 有 thread.started 但 turn 失败 = thread 活着 → exit=3 重试同 session；
     但 recovered=true 的指针同款失败 = 试住期间又失败 → exit=4
  ② ghost 收窄（#293 P0-3）：只有「resume 且 rc=0 且零事件」才判 resume_lost
     （三发实弹验过的签名）；rc≠0 零事件（认证/配置/启动错误同款长相）或
     有事件无 thread.started → retry_same exit=3，不换身份
  ③ resume_lost → 降级全新 exec（输入=工作区+原叫醒包，--room 告诉 wrapper
     拉哪房），开 recovery episode 并持久化进指针文件：一次 episode 最多签
     一个 fresh generation（fresh 失败但 thread 已出生也落指针，下轮 resume
     它试住，不是再生一个），fresh 最多试 MAX_FRESH_ATTEMPTS 次；generation
     失败或试住后再失败或次数用尽 → exit=4
  ④ recovered 指针 ghost（恢复的新家从没活过一轮就没了）→ exit=4
  ⑤ 指针文件已存在但损坏 → fail-closed exit=4（#293 P0-1：virgin=失忆老坑
     在指针层的重演——缺文件可初始化，坏文件绝不擅自 fresh 换身份，留着验尸）
  recovered 在新家成功跑过一轮后洗回 false——能正常生活的算正常人家。每轮
  原子落 sessions/*.result.json：outcome/failure_class/thread_id/turn_terminal/rc，
  给人和审计看；doorman 只认 exit code，不读 stderr、不解析 result。
用法: python wake-taiyang.py exec resume --skip-git-repo-check <session-id> --prompt-file <prompt-file>
门房 L2 用法: python wake-taiyang.py exec --skip-git-repo-check --session-file <json> --room <房名> --prompt-file <prompt-file>
"""

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

CODEX_HOME = Path.home() / ".codex"
CONFIG = CODEX_HOME / "config.toml"
BIN_DIR = Path.home() / "AppData/Local/OpenAI/Codex/bin"


def find_key(data, key):
    """递归找嵌套表里的 key——Codex 把 CODEX_CLI_PATH 放在哪个 section
    是发行版自己的事（2026-09-01 实测在 [mcp_servers.node_repl.env] 下）。"""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            hit = find_key(v, key)
            if hit is not None:
                return hit
    return None


def resolve_codex():
    """从 CODEX_CLI_PATH 解析当前 codex.exe；失效则兜底扫 bin 目录。找不到返回 None。"""
    try:
        data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        raw = find_key(data, "CODEX_CLI_PATH")
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
            print(f"[wake-taiyang] config 里的 CODEX_CLI_PATH 已失效: {p}", file=sys.stderr)
        else:
            print(f"[wake-taiyang] {CONFIG} 里没有 CODEX_CLI_PATH", file=sys.stderr)
    except Exception as e:
        print(f"[wake-taiyang] 读 {CONFIG} 失败: {e}", file=sys.stderr)
    # 兜底：版本哈希目录里挑最新的 codex.exe（配置还没跟上更新时先救命）
    try:
        cands = sorted(BIN_DIR.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except Exception as e:
        print(f"[wake-taiyang] 扫 {BIN_DIR} 失败: {e}", file=sys.stderr)
        return None
    if cands:
        print(f"[wake-taiyang] 兜底启用 {cands[0]}", file=sys.stderr)
        return cands[0]
    return None


def split_prompt_file(args):
    """门房模式显式传 --prompt-file <path>：见到 flag 就必须读成功——
    文件缺失/读失败直接 exit(2)，绝不启动 codex（fail-closed；
    「路径字符串当 prompt 发给 codex、exit=0 误 ack」的 fail-open 洞，
    太阳哥 2026-09-01 #227）。没有 flag 才原样透传——普通文本恰好
    以 .txt 结尾也不许被误读成文件。"""
    if "--prompt-file" not in args:
        return args, None
    i = args.index("--prompt-file")
    if i + 1 >= len(args):
        print("[wake-taiyang] --prompt-file 后面没跟路径", file=sys.stderr)
        sys.exit(2)
    pf = Path(args[i + 1])
    try:
        text = pf.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[wake-taiyang] 读 prompt 文件失败 {pf}: {e}", file=sys.stderr)
        sys.exit(2)
    return args[:i] + ["-"] + args[i + 2:], text


def parse_event_line(line):
    """单行（bytes/str）→ (事件 type or None, thread_id or None)。
    非 JSON 行（警告等）跳过。流式收集的逐行入口。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line.startswith("{"):
        return None, None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    t = ev.get("type")
    tid = ev.get("thread_id") if t == "thread.started" else None
    return t, tid


def parse_turn_events(raw):
    """从整段 JSONL bytes 里收事件 type。返回 (types set, thread_id or None)。
    （逐行版 parse_event_line 的批量壳，单测和离线分析用）"""
    types = set()
    thread_id = None
    for line in raw.splitlines():
        t, tid = parse_event_line(line)
        if t:
            types.add(t)
        if tid:
            thread_id = tid
    return types, thread_id


def assign_kill_on_close_job(proc):
    """把 child 挂进 Windows Job Object（KILL_ON_JOB_CLOSE）：wrapper 一旦被
    门房 900s 超时杀掉，job 句柄随 wrapper 尸体关闭，OS 自动杀整棵进程树
    （太阳哥 2026-09-02 #253 债②——以前只杀 wrapper，codex.exe 孤儿漏网）。
    非 Windows / 任何一步失败都留账放行：杀树是善后增强，不许挡点火主链。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        # 钉 WinAPI 原型（太阳哥 2026-09-03 37610a8 轮审①）：ctypes 默认
        # restype=c_int，64 位下 HANDLE 返回值会被截成 32 位——句柄高位一丢，
        # 后面每次调用都在操作一个假句柄。restype/argtypes 必须显式钉死。
        # 用私有 WinDLL 实例，不污染共享的 windll.kernel32 原型表。
        k32 = ctypes.WinDLL("kernel32")
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            print("[wake-taiyang] CreateJobObject 失败，杀树兜底没挂上", file=sys.stderr)
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            print("[wake-taiyang] SetInformationJobObject 失败，杀树兜底没挂上", file=sys.stderr)
            k32.CloseHandle(job)
            return None
        if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(proc._handle)):
            print("[wake-taiyang] AssignProcessToJobObject 失败（可能已在别的 job 里），"
                  "杀树兜底没挂上", file=sys.stderr)
            k32.CloseHandle(job)
            return None
        return job  # 句柄刻意不关：跟着 wrapper 的命走，wrapper 死=树死
    except Exception as e:
        print(f"[wake-taiyang] 挂杀树 job 失败: {e}（放行，不挡点火）", file=sys.stderr)
        return None


# ── L2 监督器（太阳哥 2026-09-03 #263 钉版：不要失败即 fork，按失败分类；
#    2026-09-05 #293 整改：ghost 签名收窄 + recovery episode 持久化 + 坏指针 fail-closed）──
# ① 有 thread.started 但 turn 失败 = thread 活着 → exit=3，指针不动，下轮重试同 session
#    （recovered=true 的除外：试住期间又失败 = 兜底链断到头 → exit=4）
# ② ghost 签名收窄：resume 且 rc=0 且零事件才判 resume_lost → 降级全新 exec；
#    rc≠0 零事件 / 有事件无 thread.started → retry_same exit=3，不换身份
# ③ recovery episode 持久化在指针文件里：一次 episode 最多签一个 fresh generation、
#    fresh 最多试 MAX_FRESH_ATTEMPTS 次；generation 失败 / 试住后再失败 / 次数用尽
#    → exit=4 交 L3 不再生崽。recovered 在新家成功跑过一轮后洗回 false
# ④ 指针文件损坏 = fail-closed exit=4（缺文件才可当初次上岗）

SERVER = "http://localhost:7788"  # 拉工作区用；太阳哥和门房同机

MAX_FRESH_ATTEMPTS = 2  # 一次 recovery episode 里 fresh 的最大尝试次数（太阳哥 #293 P0-2：
                        # 不持久化尝试数，fresh 失败下轮失忆重来，能无限生新 thread）


def split_wrapper_flags(args):
    """摘掉 wrapper 自己的 flag（--session-file/--room），剩下的透传给 codex。
    --session-file 是 L2 门房模式的总开关：没给就是旧式点火，参数原样透传。"""
    session_file = room = None
    out = []
    i = 0
    while i < len(args):
        if args[i] == "--session-file" and i + 1 < len(args):
            session_file = args[i + 1]
            i += 2
        elif args[i] == "--room" and i + 1 < len(args):
            room = args[i + 1]
            i += 2
        else:
            out.append(args[i])
            i += 1
    return out, session_file, room


def atomic_write_json(path, obj):
    """tmp+replace 原子落：任何时刻文件要么是旧的整份要么是新的整份。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_session(path):
    """读 agent 级全局指针（如实标注：不是 agent×room，见文件头 L2 段）。
    返回 (thread_id, recovered, recovery, corrupt)。
    文件不存在 = 初次上岗（可初始化）；已存在但损坏 = fail-closed（corrupt=True）
    ——绝不当初次上岗擅自 fresh 换身份（太阳哥 #293 P0-1：virgin=失忆老坑在
    指针层的重演。坏文件一行不写，留着验尸）。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False, None, False
    except Exception as e:
        print(f"[wake-taiyang] session 文件损坏 {path}: {e}——fail-closed 交 L3，"
              f"不擅自换身份（坏文件留着验尸）", file=sys.stderr)
        return None, False, None, True
    if not isinstance(d, dict):
        print(f"[wake-taiyang] session 文件不是 JSON 对象 {path}——fail-closed 交 L3",
              file=sys.stderr)
        return None, False, None, True
    return d.get("thread_id"), bool(d.get("recovered")), d.get("recovery"), False


def write_result(session_file, **kw):
    """每轮原子落一份 result JSON 在指针旁边：outcome/failure_class/thread_id/
    turn_terminal/rc——给人和审计看的。doorman 只认 exit code，不解析这个文件。"""
    if not session_file:
        return
    kw.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        atomic_write_json(str(session_file) + ".result.json", kw)
    except Exception as e:
        print(f"[wake-taiyang] result JSON 落盘失败: {e}（不挡主链）", file=sys.stderr)


def fetch_workspace(room):
    """全新 exec 的上下文补救：拉房间工作区（决定/待办/进度）。拉不到不挡点火。"""
    try:
        q = urllib.parse.urlencode({"room": room})
        with urllib.request.urlopen(f"{SERVER}/api/workspace?{q}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("content") or ""
    except Exception as e:
        print(f"[wake-taiyang] 拉工作区失败（{room}）: {e}——不挡点火", file=sys.stderr)
        return ""


def build_resume_args(args, tid):
    """exec --flags [-] → exec resume --flags <tid> [-]：session_id 必须插在 prompt
    位置参数（"-"）之前——clap 按位置顺序认 [SESSION_ID] [PROMPT]，颠倒会张冠李戴。
    不能假设 "-" 在末尾：--json 是 split_prompt_file 之后才追加进 args 的。"""
    i = args.index("exec")
    head, rest = args[:i + 1], args[i + 1:]
    if "-" in rest:
        j = rest.index("-")
        return head + ["resume"] + rest[:j] + [tid] + rest[j:]
    return head + ["resume"] + rest + [tid]


def build_fresh_prompt(room, prompt, old_tid):
    """全新 exec 的输入：恢复说明 + 工作区 + 原叫醒包（叫醒包自带最近 N 条）。"""
    if prompt is None:
        return None
    parts = ["[门房·L2 恢复] 你的旧 session 没能续上（resume 找不到 rollout），"
             "这是一次全新点火。旧 thread_id=%s。上下文补救如下，读完接着干活。\n"
             % (old_tid or "无")]
    if room:
        ws = fetch_workspace(room)
        if ws:
            parts.append("## 房间「%s」工作区（决定/待办/进度）\n%s\n" % (room, ws))
    parts.append("## 原叫醒包\n" + prompt)
    return "\n".join(parts)


def turn_terminal(types):
    return "failed" if "turn.failed" in types else (
        "completed" if "turn.completed" in types else "none")


def turn_ok(rc, types):
    """不信 exit code：exit=0 且本轮出现 turn.completed 且无 turn.failed 才算醒。"""
    return rc == 0 and "turn.completed" in types and "turn.failed" not in types


def run_exec(codex, args, prompt):
    """点一次 codex exec：流式收 stdout 逐行边吐边判终态，挂杀树 job。
    返回 (rc, types set, thread_id or None)。"""
    p = subprocess.Popen([str(codex)] + args,
                         stdin=subprocess.PIPE if prompt is not None else None,
                         stdout=subprocess.PIPE)
    if prompt is not None:
        # stdin 太大时同步写会和孩子吐 stdout 互堵，喂饭走旁路线程
        data = prompt.encode("utf-8")
        def _feed():
            try:
                p.stdin.write(data)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # 孩子早夭，饭倒了，rc 会告诉我们
        threading.Thread(target=_feed, daemon=True).start()
    # TODO(非阻断，太阳哥 2026-09-03 37610a8 轮审)：Popen 后再 assign 有极短
    # 抢跑窗口——child 若在 assign 前抢先 fork 出子进程，早产孙辈漏在 job 外。
    assign_kill_on_close_job(p)
    types = set()
    thread_id = None
    for line in p.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        t, tid = parse_event_line(line)
        if t:
            types.add(t)
        if tid:
            thread_id = tid
    return p.wait(), types, thread_id


def main():
    args, prompt = split_prompt_file(sys.argv[1:])
    args, session_file, room = split_wrapper_flags(args)
    codex = resolve_codex()
    if not codex:
        print("[wake-taiyang] 找不到任何 codex.exe", file=sys.stderr)
        return 127
    # exec 才有一轮 turn 可验；--version 等别的用法原样透传
    if "exec" not in args:
        p = subprocess.run([str(codex)] + args,
                           input=prompt.encode("utf-8") if prompt is not None else None)
        return p.returncode
    if "--json" not in args:
        args = args + ["--json"]
    if not session_file:
        # 旧式点火（resume <id> 由调用方自己拼进 args）：原样透传
        rc, types, thread_id = run_exec(codex, args, prompt)
        if thread_id:
            print(f"[wake-taiyang] thread_id={thread_id}", file=sys.stderr)
        if turn_ok(rc, types):
            return 0
        # 不信 exit code：turn.failed 或无 completed 一律 exit=3（不 ack，等重投）。
        # 原始非零 rc 记进 stderr 留账，对外只认 3——门房侧「非零即失败」语义不变
        print(f"[wake-taiyang] 本轮没有 turn.completed（rc={rc}, "
              f"events={sorted(types) or '无'}），按失败 exit=3", file=sys.stderr)
        return 3
    # ── L2 门房模式：session 指针 wrapper 自己持有，按失败分类处置 ──
    tid, recovered, recovery, corrupt = load_session(session_file)
    if corrupt:
        # 坏指针 fail-closed：不点火、不写指针文件，只落 result 交 L3（#293 P0-1）
        write_result(session_file, outcome="l3", failure_class="session_corrupt",
                     thread_id=None, turn_terminal="none", rc=None)
        return 4
    # recovery episode 进行中且指针还指着死掉的旧家（或没签到家）→ 跳过 resume
    # 直接续 fresh；指针已是新一代（tid != old_tid）才走 resume（试住验收）
    if tid and not (recovery and tid == recovery.get("old_tid")):
        rc, types, _ = run_exec(codex, build_resume_args(args, tid), prompt)
        if "thread.started" in types:
            # thread 活着，成败只看 turn 终态
            if turn_ok(rc, types):
                if recovered or recovery:
                    # 恢复的新家成功生活了一轮 → 洗回正常人家，episode 销账
                    atomic_write_json(session_file, {"thread_id": tid, "recovered": False})
                write_result(session_file, outcome="ok", failure_class="none",
                             thread_id=tid, turn_terminal="completed", rc=rc)
                return 0
            if recovered:
                # 试住期间 turn 又失败 → 兜底链断到头（#293 P0-2：旧版这里
                # exit=3 重试同 session，recovered 指针能永远重试下去）
                write_result(session_file, outcome="l3",
                             failure_class="recovered_turn_failed",
                             thread_id=tid, turn_terminal=turn_terminal(types), rc=rc,
                             events=sorted(types))
                print(f"[wake-taiyang] 恢复出的 thread {tid} 试住期间 turn 又失败"
                      f"（rc={rc}, events={sorted(types) or '无'}）——"
                      f"兜底链断到头，exit=4 交 L3", file=sys.stderr)
                return 4
            write_result(session_file, outcome="retry_same", failure_class="turn_failed",
                         thread_id=tid, turn_terminal=turn_terminal(types), rc=rc,
                         events=sorted(types))
            print(f"[wake-taiyang] thread 活着但本轮没有 turn.completed（rc={rc}, "
                  f"events={sorted(types) or '无'}）——指针不动，exit=3 重试同 session",
                  file=sys.stderr)
            return 3
        # 无 thread.started：先按签名分类，不是 ghost 就不换身份（#293 P0-3 收窄：
        # 只有「resume 且 rc=0 且零事件」是三发实弹验过的 ghost；rc≠0 零事件是
        # 认证/配置/启动错误的同款长相，有事件无 thread.started 也不是没接上）
        if not (rc == 0 and not types):
            write_result(session_file, outcome="retry_same", failure_class="resume_error",
                         thread_id=tid, turn_terminal=turn_terminal(types), rc=rc,
                         events=sorted(types))
            print(f"[wake-taiyang] resume 异常但不是 ghost 签名（rc={rc}, "
                  f"events={sorted(types) or '无'}）——指针不动，exit=3 重试同 session",
                  file=sys.stderr)
            return 3
        # ghost = resume_lost
        if recovered:
            # 恢复的新家从没活过一轮就又没了 → 交 L3，不再生崽
            write_result(session_file, outcome="l3", failure_class="recovered_lost",
                         thread_id=tid, turn_terminal="none", rc=rc, events=[])
            print(f"[wake-taiyang] 恢复出的 thread {tid} resume 又失败——"
                  f"兜底链断到头，exit=4 交 L3，不再自动生崽", file=sys.stderr)
            return 4
        print(f"[wake-taiyang] resume 没接上（thread {tid} 找不到 rollout，ghost 签名）"
              f"——降级全新 exec，开 recovery episode", file=sys.stderr)
        recovery = {"old_tid": tid, "attempts": 0,
                    "started_ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    # ── fresh 路径（初次上岗 / ghost 降级 / episode 续试）──
    old_tid = recovery.get("old_tid") if recovery else None
    attempts = int(recovery.get("attempts", 0)) if recovery else 0
    if attempts >= MAX_FRESH_ATTEMPTS:
        write_result(session_file, outcome="l3", failure_class="fresh_exhausted",
                     thread_id=tid or old_tid, turn_terminal="none", rc=None)
        print(f"[wake-taiyang] recovery episode 已试 {attempts} 次 fresh 全失败——"
              f"兜底链断到头，exit=4 交 L3，不再自动生崽", file=sys.stderr)
        return 4
    rc, types, new_tid = run_exec(codex, args, build_fresh_prompt(room, prompt, old_tid or tid))
    attempts += 1
    if turn_ok(rc, types) and new_tid:
        # 原子写指针，episode 销账；从旧家搬来的打 recovered=true（试住期）
        atomic_write_json(session_file,
                          {"thread_id": new_tid, "recovered": recovery is not None})
        write_result(session_file, outcome="fresh_recovered" if recovery else "ok",
                     failure_class="resume_lost" if recovery else "none",
                     thread_id=new_tid, turn_terminal="completed", rc=rc)
        print(f"[wake-taiyang] 全新 exec 归位，新指针 {new_tid}"
              f"（recovered={recovery is not None}）", file=sys.stderr)
        return 0
    # fresh 失败：episode 和 attempts 必须落盘，否则下轮失忆重数（无限生崽的洞，
    # #293 P0-2）。thread 已出生（new_tid）也一样落指针——一次 episode 只签一个
    # generation，下轮 resume 它试住，不是再生一个
    keep = new_tid or old_tid or tid
    atomic_write_json(session_file, {
        "thread_id": keep, "recovered": True,
        "recovery": {"old_tid": old_tid or tid, "attempts": attempts,
                     "started_ts": (recovery or {}).get("started_ts")
                                   or time.strftime("%Y-%m-%d %H:%M:%S")}})
    if attempts >= MAX_FRESH_ATTEMPTS:
        write_result(session_file, outcome="l3", failure_class="fresh_exhausted",
                     thread_id=keep, turn_terminal=turn_terminal(types), rc=rc,
                     events=sorted(types))
        print(f"[wake-taiyang] 第 {attempts} 次 fresh 也没拿到 turn.completed——"
              f"episode 尝试用尽，exit=4 交 L3", file=sys.stderr)
        return 4
    write_result(session_file, outcome="retry_fresh", failure_class="fresh_failed",
                 thread_id=keep, turn_terminal=turn_terminal(types), rc=rc,
                 events=sorted(types))
    print(f"[wake-taiyang] 第 {attempts} 次 fresh 没拿到 turn.completed（rc={rc}, "
          f"events={sorted(types) or '无'}）——episode 已落盘（attempts={attempts}），"
          f"exit=3 等重投", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
