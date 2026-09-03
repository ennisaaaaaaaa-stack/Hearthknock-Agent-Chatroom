#!/usr/bin/env python3
"""wake-taiyang.py 的可重复测试（太阳哥 2026-09-01 #227 点名的三条验收）：

1. --prompt-file 文件缺失 → exit=2 且绝不启动 child（fail-closed，
   路径字符串当 prompt 发出去、exit=0 误 ack 的洞）
2. UTF-8 大文件完整进 stdin（假 codex 实收字节逐一比对）
3. 没有 flag 时普通参数恰好以 .txt 结尾也不误读（原样透传）
4. turn 终态判据（太阳哥 2026-09-02 #247：不信 exit code——codex 实测
   ghost session/坏模型都是 exit=0 假成功）：exec 模式强制 --json，
   exit=0 且本轮有 turn.completed 才算醒；turn.failed / 无 completed /
   非零 rc 一律 exit=3（不 ack）；非 exec 用法原样透传
5. stdout 流式收集（债①）：~8MB 输出 + 大 stdin 同时跑不互堵、终态照判
6. 杀树 job（债②）：真杀树行为验收——child 挂 job 后生 grandchild，显式
   CloseHandle 触发 KILL_ON_JOB_CLOSE，父子两 PID 都必须死；非 Windows 放行

跑法：python test_wake_taiyang.py
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("wake_taiyang", BASE / "wake-taiyang.py")
wt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wt)

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails += 1


def pid_alive(pid):
    """进程还活着吗。Windows：OpenProcess + GetExitCodeProcess；别处 os.kill(pid, 0)。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    # 私有 WinDLL 实例，和主代码同原则：不污染共享的 windll.kernel32 原型表
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def run_main(argv):
    """以给定 argv 跑 wt.main()，返回 returncode 或 SystemExit 的 code。"""
    old = sys.argv
    sys.argv = ["wake-taiyang.py"] + argv
    try:
        return wt.main()
    except SystemExit as e:
        return e.code
    finally:
        sys.argv = old


# --- 1. 文件缺失：exit=2，child 从未被启动 ---
calls = []
old_run = wt.subprocess.run
wt.subprocess.run = lambda *a, **k: calls.append(a)  # 小孩要是被点火就记账
try:
    rc = run_main(["exec", "resume", "--skip-git-repo-check", "sid",
                   "--prompt-file", "这个文件根本不存在.txt"])
finally:
    wt.subprocess.run = old_run
check("1a 缺失文件 exit=2", rc == 2, f"rc={rc}")
check("1b 缺失文件没启动 child", calls == [], f"calls={calls}")

# --prompt-file 后面没跟路径也一样 fail-closed
calls.clear()
old_run = wt.subprocess.run
wt.subprocess.run = lambda *a, **k: calls.append(a)
try:
    rc = run_main(["exec", "resume", "--prompt-file"])
finally:
    wt.subprocess.run = old_run
check("1c flag 后缺路径 exit=2 且不启动 child", rc == 2 and calls == [], f"rc={rc} calls={calls}")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    # --- 2. UTF-8 大文件完整进 stdin（假 codex 实收比对） ---
    payload = ("门房狠狠审：大积压叫醒包，中文、emoji🔔、符号「」混排。\n" * 8000)  # ~400KB
    pf = td / "prompt.txt"
    pf.write_text(payload, encoding="utf-8")
    fake = td / "fake_codex.py"
    fake.write_text(
        "import sys, os, pathlib\n"
        "data = sys.stdin.buffer.read()\n"
        "pathlib.Path(os.environ['FAKE_OUT']).write_bytes(data)\n"
        "pathlib.Path(os.environ['FAKE_ARGV']).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "print('{\"type\":\"turn.completed\",\"usage\":{}}')\n",
        encoding="utf-8")
    out = td / "received.bin"
    argvf = td / "argv.txt"
    old_resolve = wt.resolve_codex
    wt.resolve_codex = lambda: Path(sys.executable)  # 假 codex = python 自己
    old_env = {k: os.environ.get(k) for k in ("FAKE_OUT", "FAKE_ARGV")}
    os.environ["FAKE_OUT"] = str(out)
    os.environ["FAKE_ARGV"] = str(argvf)
    try:
        rc = run_main([str(fake), "exec", "resume", "--skip-git-repo-check", "sid",
                       "--prompt-file", str(pf)])
    finally:
        wt.resolve_codex = old_resolve
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    got = out.read_bytes() if out.exists() else b""
    check("2a 大文件点火 exit=0", rc == 0, f"rc={rc}")
    check("2b UTF-8 大文件完整进 stdin", got == payload.encode("utf-8"),
          f"got={len(got)}B want={len(payload.encode('utf-8'))}B")
    check("2c exec 模式强制带 --json", "--json" in argvf.read_text(encoding="utf-8").splitlines(),
          argvf.read_text(encoding="utf-8"))

    # --- 3. 没有 flag：普通参数恰好以 .txt 结尾（文件还真存在）也不误读 ---
    txt = td / "普通.txt"
    txt.write_text("我不是 prompt 文件", encoding="utf-8")
    args = ["exec", "resume", "sid", str(txt)]
    got_args, got_prompt = wt.split_prompt_file(list(args))
    check("3a 无 flag 原样透传", got_args == args and got_prompt is None,
          f"args={got_args} prompt={got_prompt!r}")

    # 有 flag 的正常路径：flag 位置换 "-"，flag 本身从 argv 里拿掉
    got_args, got_prompt = wt.split_prompt_file(
        ["exec", "resume", "sid", "--prompt-file", str(pf)])
    check("3b 有 flag 换成 '-' 读文件",
          got_args == ["exec", "resume", "sid", "-"] and got_prompt == payload,
          f"args={got_args}")

    # --- 4. turn 终态判据（不信 exit code） ---
    def fake_with(body, name):
        f = td / name
        f.write_text(body, encoding="utf-8")
        return f

    def run_fake(fake, argv_extra, rc_env=None):
        old_resolve = wt.resolve_codex
        wt.resolve_codex = lambda: Path(sys.executable)
        old_rc = os.environ.get("FAKE_RC")
        if rc_env is not None:
            os.environ["FAKE_RC"] = str(rc_env)
        try:
            return run_main([str(fake)] + argv_extra)
        finally:
            wt.resolve_codex = old_resolve
            if rc_env is not None:
                if old_rc is None:
                    os.environ.pop("FAKE_RC", None)
                else:
                    os.environ["FAKE_RC"] = old_rc

    # 4a: exit=0 但事件流是 turn.failed（坏模型的真实长相）→ 3
    f_failed = fake_with(
        "import os, sys\n"
        "print('{\"type\":\"thread.started\",\"thread_id\":\"t1\"}')\n"
        "print('{\"type\":\"turn.failed\",\"error\":{\"message\":\"bad model\"}}')\n"
        "sys.exit(int(os.environ.get('FAKE_RC', '0')))\n",
        "fake_failed.py")
    rc = run_fake(f_failed, ["exec", "--skip-git-repo-check", "hi"])
    check("4a exit0+turn.failed → 3", rc == 3, f"rc={rc}")

    # 4b: exit=0 且一个事件都没有（ghost session 的真实长相）→ 3
    f_silent = fake_with("import sys\nsys.exit(0)\n", "fake_silent.py")
    rc = run_fake(f_silent, ["exec", "resume", "--skip-git-repo-check", "ghost-sid", "-"])
    check("4b exit0+无事件 → 3", rc == 3, f"rc={rc}")

    # 4c: 有 turn.completed 但 child 非零 → 3（原始 rc 记 stderr，对外一律 3）
    rc = run_fake(f_failed, ["exec", "hi"], rc_env=1)
    check("4c 非零 rc → 3", rc == 3, f"rc={rc}")

    # 4d: exit=0 + turn.completed + 混着 item error 噪音（WebSocket fallback 的真实长相）→ 0
    f_ok = fake_with(
        "print('{\"type\":\"item.completed\",\"item\":{\"type\":\"error\",\"message\":\"fallback noise\"}}')\n"
        "print('not json at all')\n"
        "print('{\"type\":\"turn.completed\",\"usage\":{}}')\n",
        "fake_ok.py")
    rc = run_fake(f_ok, ["exec", "hi"])
    check("4d exit0+completed(混噪音) → 0", rc == 0, f"rc={rc}")

    # 4e: 非 exec 用法（--version 等）原样透传，不强加 --json 也不判事件
    rc = run_fake(f_failed, ["--version"], rc_env=7)
    check("4e 非 exec 原样透传 rc", rc == 7, f"rc={rc}")

    # 4f: 事件解析单元——thread_id 提取、坏行跳过
    types, tid = wt.parse_turn_events(
        b'garbage\n{"type":"thread.started","thread_id":"abc"}\n{"type":"turn.completed"}\n{"broken\n')
    check("4f parse_turn_events", types == {"thread.started", "turn.completed"} and tid == "abc",
          f"types={types} tid={tid}")

    # --- 5. 流式收集（债①）：超大 stdout 不整包缓冲、不互堵、终态照判 ---
    f_big = fake_with(
        "import sys\n"
        "line = '{\"type\":\"item.completed\",\"item\":{\"text\":\"" + "x" * 3800 + "\"}}'\n"
        "for _ in range(2000):\n"  # ~8MB，远超管道缓冲和旧整包缓冲的胃口
        "    print(line)\n"
        "sys.stdout.flush()\n"
        "data = sys.stdin.buffer.read()\n"  # 同时验 stdin 喂饭线程不和 stdout 互堵
        "assert len(data) > 100000\n"
        "print('{\"type\":\"turn.completed\",\"usage\":{}}')\n",
        "fake_big.py")
    rc = run_fake(f_big, ["exec", "--skip-git-repo-check", "hi",
                          "--prompt-file", str(pf)])
    check("5a 8MB 流式+大 stdin 不互堵 exit=0", rc == 0, f"rc={rc}")

    # --- 6. 杀树 job（债②）：真杀树行为验收（太阳哥 2026-09-03 37610a8 轮审②）---
    # child 挂进 job 后才放生 grandchild（发令枪文件，消除 assign 前的抢跑窗口，
    # 保证孙辈 deterministic 地落在 job 里），然后显式 CloseHandle(job) 模拟
    # wrapper 暴毙时句柄随尸体关闭——父子两个 PID 都必须死。「杀整棵树」验的是
    # 行为，不是 assign 的返回值。
    import subprocess as _sp
    import time as _time
    if os.name == "nt":
        go_file = td / "go"
        gc_pid_file = td / "gc.pid"
        child = _sp.Popen([sys.executable, "-c",
            "import subprocess, sys, time, pathlib\n"
            f"go = pathlib.Path(r'{go_file}')\n"
            "while not go.exists():\n"
            "    time.sleep(0.05)\n"
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"pathlib.Path(r'{gc_pid_file}').write_text(str(gc.pid))\n"
            "time.sleep(60)\n"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        try:
            job = wt.assign_kill_on_close_job(child)
            check("6a Windows 挂 job 成功", bool(job), f"job={job}")
            if job:
                go_file.touch()  # 发令：child 此刻才生 grandchild，孙辈落在 job 里
                deadline = _time.time() + 10
                while not gc_pid_file.exists() and _time.time() < deadline:
                    _time.sleep(0.1)
                gc_pid = int(gc_pid_file.read_text())
                import ctypes as _ct
                from ctypes import wintypes as _wty
                _k32 = _ct.WinDLL("kernel32")  # 私有实例，同 pid_alive 的原则
                _k32.CloseHandle.restype = _wty.BOOL
                _k32.CloseHandle.argtypes = [_wty.HANDLE]
                check("6c 显式 CloseHandle(job)", bool(_k32.CloseHandle(_wty.HANDLE(job))))
                try:
                    child.wait(timeout=10)
                    child_dead = True
                except _sp.TimeoutExpired:
                    child_dead = False
                check("6d child 被 KILL_ON_JOB_CLOSE 杀掉", child_dead,
                      f"pid={child.pid}")
                deadline = _time.time() + 10
                while pid_alive(gc_pid) and _time.time() < deadline:
                    _time.sleep(0.2)
                check("6e grandchild 陪葬（整棵树都死）", not pid_alive(gc_pid),
                      f"gc_pid={gc_pid}")
        finally:
            child.kill()  # 断言失败也别留 60s 的活口
            child.wait()
    else:
        proc = _sp.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                         stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        try:
            check("6a 非 Windows 放行 None", wt.assign_kill_on_close_job(proc) is None)
        finally:
            proc.kill()
            proc.wait()
    # 逐行解析单元
    t, tid = wt.parse_event_line(b'{"type":"thread.started","thread_id":"z9"}\n')
    t2, _ = wt.parse_event_line(b'not json')
    check("6b parse_event_line", t == "thread.started" and tid == "z9" and t2 is None,
          f"t={t} tid={tid} t2={t2}")

print()
if fails:
    print(f"{fails} 条挂了")
    sys.exit(1)
print("全绿")
