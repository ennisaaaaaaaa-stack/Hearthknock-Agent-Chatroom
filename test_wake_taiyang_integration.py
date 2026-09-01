#!/usr/bin/env python3
"""wake-taiyang.py 的实弹集成测（太阳哥 2026-09-02 #247 点名的三发）：

打真 codex CLI，验证「不信 exit code」判据在生产语义下成立：
1. ghost session（不存在的 session id）→ 不得出现 turn.completed，wrapper exit=3（不 ack）
2. 坏模型 → 不得出现 turn.completed（事件流里是 turn.failed），wrapper exit=3（不 ack）
3. 正常 turn（--ephemeral 新会话，不落 session 文件、不碰太阳哥的生产会话）→
   必须出现 turn.completed，wrapper exit=0

会真实调用模型（花额度、每发最长等 10 分钟超时），不进常规单元测试套件。
跑法：python test_wake_taiyang_integration.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = sys.executable
TIMEOUT = 600  # 生产上 codex 先试 WebSocket 再 fallback HTTPS，单发可能超过一分钟

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails += 1


def shot(prompt_text, extra_args):
    """走 wrapper 完整链路（--prompt-file 生产调用约定），返回 (rc, event types set)。"""
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "prompt.txt"
        pf.write_text(prompt_text, encoding="utf-8")
        p = subprocess.run(
            [PY, str(BASE / "wake-taiyang.py")] + extra_args + ["--prompt-file", str(pf)],
            capture_output=True, timeout=TIMEOUT)
    types = set()
    for line in p.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                t = json.loads(line).get("type")
                if t:
                    types.add(t)
            except json.JSONDecodeError:
                pass
    return p.returncode, types, p.stderr.decode("utf-8", errors="replace")


print("=== 第 1 发：ghost session ===")
rc, types, err = shot("hi", ["exec", "resume", "--skip-git-repo-check",
                             "00000000-0000-0000-0000-000000000000"])
check("1a ghost session 不得有 turn.completed", "turn.completed" not in types, f"types={types}")
check("1b ghost session wrapper exit=3（不 ack）", rc == 3, f"rc={rc} stderr={err[-200:]}")

print("=== 第 2 发：坏模型 ===")
rc, types, err = shot("Reply with the single word: OK",
                      ["exec", "--ephemeral", "--skip-git-repo-check",
                       "-m", "this-model-does-not-exist-xyz"])
check("2a 坏模型不得有 turn.completed", "turn.completed" not in types, f"types={types}")
check("2b 坏模型 wrapper exit=3（不 ack）", rc == 3, f"rc={rc} stderr={err[-200:]}")

print("=== 第 3 发：正常 turn ===")
rc, types, err = shot("Reply with the single word: OK",
                      ["exec", "--ephemeral", "--skip-git-repo-check"])
check("3a 正常 turn 必须有 turn.completed", "turn.completed" in types, f"types={types}")
check("3b 正常 turn wrapper exit=0", rc == 0, f"rc={rc} stderr={err[-200:]}")

print()
if fails:
    print(f"{fails} 条挂了")
    sys.exit(1)
print("全绿")
