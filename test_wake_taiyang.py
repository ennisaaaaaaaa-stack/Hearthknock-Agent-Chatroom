#!/usr/bin/env python3
"""wake-taiyang.py 的可重复测试（太阳哥 2026-09-01 #227 点名的三条验收）：

1. --prompt-file 文件缺失 → exit=2 且绝不启动 child（fail-closed，
   路径字符串当 prompt 发出去、exit=0 误 ack 的洞）
2. UTF-8 大文件完整进 stdin（假 codex 实收字节逐一比对）
3. 没有 flag 时普通参数恰好以 .txt 结尾也不误读（原样透传）

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
        "pathlib.Path(os.environ['FAKE_OUT']).write_bytes(data)\n",
        encoding="utf-8")
    out = td / "received.bin"
    old_resolve = wt.resolve_codex
    wt.resolve_codex = lambda: Path(sys.executable)  # 假 codex = python 自己
    old_env = os.environ.get("FAKE_OUT")
    os.environ["FAKE_OUT"] = str(out)
    try:
        rc = run_main([str(fake), "exec", "resume", "--skip-git-repo-check", "sid",
                       "--prompt-file", str(pf)])
    finally:
        wt.resolve_codex = old_resolve
        if old_env is None:
            del os.environ["FAKE_OUT"]
        else:
            os.environ["FAKE_OUT"] = old_env
    got = out.read_bytes() if out.exists() else b""
    check("2a 大文件点火 exit=0", rc == 0, f"rc={rc}")
    check("2b UTF-8 大文件完整进 stdin", got == payload.encode("utf-8"),
          f"got={len(got)}B want={len(payload.encode('utf-8'))}B")

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

print()
if fails:
    print(f"{fails} 条挂了")
    sys.exit(1)
print("全绿")
