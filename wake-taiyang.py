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
用法: python wake-taiyang.py exec resume --skip-git-repo-check <session-id> --prompt-file <prompt-file>
"""

import json
import os
import subprocess
import sys
import threading
import tomllib
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


def main():
    args, prompt = split_prompt_file(sys.argv[1:])
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
    # 流式收集（太阳哥 2026-09-02 #253 债①）：以前 subprocess.run 整包双重缓冲，
    # 超长 turn 把全部 JSONL 压在内存里；现在逐行读，边收边原样吐回（门房日志
    # 链路不变），事件终态逐行判。stderr 让 child 直接继承。
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
    # 抢跑窗口——child 若在 assign 前抢先 fork 出子进程，早产孙辈漏在 job 外，
    # 杀树时漏网。codex 实测真抢跑了再升级 CREATE_SUSPENDED → assign →
    # ResumeThread（创建即挂起，挂进 job 后再放行）。
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
    rc = p.wait()
    if thread_id:
        print(f"[wake-taiyang] thread_id={thread_id}", file=sys.stderr)
    if rc == 0 and "turn.completed" in types and "turn.failed" not in types:
        return 0
    # 不信 exit code：turn.failed 或无 completed 一律 exit=3（不 ack，等重投）。
    # 原始非零 rc 记进 stderr 留账，对外只认 3——门房侧「非零即失败」语义不变
    print(f"[wake-taiyang] 本轮没有 turn.completed（rc={rc}, "
          f"events={sorted(types) or '无'}），按失败 exit=3", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
