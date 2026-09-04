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
7. L2 状态机（太阳哥 2026-09-05 #293 打回后点名的测试清单）：坏指针不 fresh、
   fresh 失败两轮只生一个且进 4、recovered turn_failed 能终止、rc1 无事件
   不换指针、ghost 真签名保留、episode attempts 持久化与用尽、试住成功洗回
8. doorman l3_alert（#293 P1）：POST 失败不记 6h 账不落盘、可立即重试；
   成功才记账并原子落盘；6h 幂等不刷屏

跑法：python test_wake_taiyang.py
"""

import importlib.util
import json as _json
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

# --- 7. L2 状态机（太阳哥 2026-09-05 #293 打回后点名的测试清单） ---
# run_exec 全部用剧本替身：按调用顺序弹 (rc, types, tid)，记录是 resume 还是 fresh。
def run_l2(td, script, session_content="__missing__"):
    """session_content: dict 写 JSON / str 原样写（造坏文件）/ "__missing__" 不写。
    返回 (rc, calls, session 读出值, 原始文件内容)。"""
    sf = td / "sess.json"
    if session_content != "__missing__":
        if isinstance(session_content, dict):
            sf.write_text(_json.dumps(session_content, ensure_ascii=False), encoding="utf-8")
        else:
            sf.write_text(session_content, encoding="utf-8")
    pf = td / "prompt.txt"
    pf.write_text("叫醒包", encoding="utf-8")
    calls = []
    scripted = list(script)
    old_run_exec, old_resolve, old_ws = wt.run_exec, wt.resolve_codex, wt.fetch_workspace
    wt.resolve_codex = lambda: Path(sys.executable)
    wt.fetch_workspace = lambda room: ""

    def fake_run_exec(codex, args, prompt):
        calls.append("resume" if "resume" in args else "fresh")
        return scripted.pop(0)

    wt.run_exec = fake_run_exec
    try:
        rc = run_main(["exec", "--skip-git-repo-check",
                       "--session-file", str(sf), "--room", "测试房",
                       "--prompt-file", str(pf)])
    finally:
        wt.run_exec, wt.resolve_codex, wt.fetch_workspace = old_run_exec, old_resolve, old_ws
    raw = sf.read_text(encoding="utf-8") if sf.exists() else None
    try:
        sess = _json.loads(raw) if raw else None
    except Exception:
        sess = raw
    return rc, calls, sess, raw


with tempfile.TemporaryDirectory() as td7:
    td7 = Path(td7)

    # 7a 坏指针不 fresh（P0-1）：fail-closed exit=4，child 一次没点，坏文件原样留着
    rc, calls, sess, raw = run_l2(td7, [], session_content="{ 这不是 json")
    res = _json.loads((td7 / "sess.json.result.json").read_text(encoding="utf-8"))
    check("7a 坏指针 exit=4 不点火不换身份",
          rc == 4 and calls == [] and raw == "{ 这不是 json"
          and res.get("failure_class") == "session_corrupt",
          f"rc={rc} calls={calls} raw={raw!r} res={res}")

    # 7b rc=1 无事件不换指针（P0-3 收窄）：retry_same exit=3，不 fresh，指针原样
    rc, calls, sess, _ = run_l2(td7, [(1, set(), None)],
                                session_content={"thread_id": "old", "recovered": False})
    check("7b rc1无事件 → 3 不换指针",
          rc == 3 and calls == ["resume"] and sess.get("thread_id") == "old"
          and not sess.get("recovered") and "recovery" not in sess,
          f"rc={rc} calls={calls} sess={sess}")

    # 7c 真 ghost（rc=0 零事件）通路保留：降级 fresh，成功 → 新指针 recovered=true
    rc, calls, sess, _ = run_l2(td7, [(0, set(), None),
                                      (0, {"thread.started", "turn.completed"}, "new1")],
                                session_content={"thread_id": "old", "recovered": False})
    check("7c ghost → fresh 成功 recovered=true",
          rc == 0 and calls == ["resume", "fresh"]
          and sess == {"thread_id": "new1", "recovered": True},
          f"rc={rc} calls={calls} sess={sess}")

    # 7d fresh 失败两轮只生一个且进 4（P0-2）：fresh 失败但 thread 已出生 → 落指针；
    # 下轮 resume 它（试住）turn 又失败 → exit=4；全程只 fresh 过一次
    rc1, calls1, sess1, _ = run_l2(td7, [(0, set(), None),
                                         (1, {"thread.started", "turn.failed"}, "new1")],
                                   session_content={"thread_id": "old", "recovered": False})
    ok1 = (rc1 == 3 and calls1 == ["resume", "fresh"]
           and sess1["thread_id"] == "new1" and sess1["recovered"] is True
           and sess1["recovery"]["attempts"] == 1
           and sess1["recovery"]["old_tid"] == "old")
    rc2, calls2, sess2, _ = run_l2(td7, [(1, {"thread.started", "turn.failed"}, "new1")],
                                   session_content=sess1)
    check("7d fresh失败两轮只生一个且进4",
          ok1 and rc2 == 4 and calls2 == ["resume"] and sess2["thread_id"] == "new1",
          f"rc1={rc1} calls1={calls1} sess1={sess1} | rc2={rc2} calls2={calls2}")

    # 7e fresh 失败（thread 没出生）两轮 → 第二次进 4（P0-2 attempts 持久化；
    # 第二轮 episode 进行中且指针还是死掉的旧家 → 跳过 resume 直接 fresh）
    rc1, calls1, sess1, _ = run_l2(td7, [(0, set(), None), (1, set(), None)],
                                   session_content={"thread_id": "old", "recovered": False})
    ok1 = (rc1 == 3 and sess1["thread_id"] == "old"
           and sess1["recovery"]["attempts"] == 1)
    rc2, calls2, sess2, _ = run_l2(td7, [(1, set(), None)], session_content=sess1)
    check("7e fresh 两次没出生 → 第二轮进4",
          ok1 and rc2 == 4 and calls2 == ["fresh"]
          and sess2["recovery"]["attempts"] == 2,
          f"rc1={rc1} sess1={sess1} | rc2={rc2} calls2={calls2} sess2={sess2}")

    # 7f recovered 指针 thread.started+turn.failed → 终止进 4（P0-2：旧版永远 exit=3）
    rc, calls, sess, _ = run_l2(td7, [(0, {"thread.started", "turn.failed"}, "t")],
                                session_content={"thread_id": "t", "recovered": True})
    check("7f recovered turn_failed → 4",
          rc == 4 and calls == ["resume"], f"rc={rc} calls={calls}")

    # 7g recovered 指针 ghost → 4（原有 case③ 回归）
    rc, calls, _, _ = run_l2(td7, [(0, set(), None)],
                             session_content={"thread_id": "t", "recovered": True})
    check("7g recovered ghost → 4", rc == 4 and calls == ["resume"], f"rc={rc}")

    # 7h 试住成功一轮 → 洗回正常人家，episode 销账
    rc, calls, sess, _ = run_l2(
        td7, [(0, {"thread.started", "turn.completed"}, "t")],
        session_content={"thread_id": "t", "recovered": True,
                         "recovery": {"old_tid": "old", "attempts": 1}})
    check("7h 试住成功洗回 false",
          rc == 0 and sess == {"thread_id": "t", "recovered": False},
          f"rc={rc} sess={sess}")

    # 7i episode attempts 已用尽 → 不点火直接 4
    rc, calls, _, _ = run_l2(td7, [], session_content={
        "thread_id": "old", "recovered": True,
        "recovery": {"old_tid": "old", "attempts": 2}})
    check("7i attempts 用尽直接 4 不点火", rc == 4 and calls == [], f"rc={rc} calls={calls}")

# --- 8. doorman l3_alert（太阳哥 #293 P1：POST 成功才记 6h 账 + 原子落盘） ---
spec_dm = importlib.util.spec_from_file_location("doorman", BASE / "doorman.py")
dm = importlib.util.module_from_spec(spec_dm)
spec_dm.loader.exec_module(dm)

with tempfile.TemporaryDirectory() as td8:
    td8 = Path(td8)
    dm.STATE_FILE = td8 / "state.json"
    dm.LOG = td8 / "test.log"
    cfg8 = {"server": "http://x"}
    state8 = {"l3_alerted": {}}
    calls8 = []

    def post_fail(server, path, payload):
        calls8.append(path)
        raise OSError("网断了")

    def post_ok(server, path, payload):
        calls8.append(path)
        return {"id": 1}

    # 8a POST 失败：不记 6h 账、不落盘
    dm.post_json = post_fail
    dm.l3_alert(cfg8, state8, "太阳哥", "测试房")
    check("8a POST失败不记账不落盘",
          state8["l3_alerted"] == {} and not dm.STATE_FILE.exists()
          and calls8 == ["/api/send"],
          f"state={state8} calls={calls8}")

    # 8b 失败后立即可重试（不静默六小时）；成功才记账 + 原子落盘
    dm.post_json = post_ok
    dm.l3_alert(cfg8, state8, "太阳哥", "测试房")
    on_disk = _json.loads(dm.STATE_FILE.read_text(encoding="utf-8")) \
        if dm.STATE_FILE.exists() else {}
    check("8b 成功才记账且落盘",
          len(calls8) == 2 and "太阳哥|测试房" in state8["l3_alerted"]
          and "太阳哥|测试房" in on_disk.get("l3_alerted", {}),
          f"calls={calls8} state={state8['l3_alerted']} disk={on_disk}")

    # 8c 6h 幂等：成功记账后不再 POST
    dm.l3_alert(cfg8, state8, "太阳哥", "测试房")
    check("8c 6h 内不重复 POST", len(calls8) == 2, f"calls={calls8}")

print()
if fails:
    print(f"{fails} 条挂了")
    sys.exit(1)
print("全绿")
