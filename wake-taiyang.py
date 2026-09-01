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
用法: python wake-taiyang.py exec resume --skip-git-repo-check <session-id> --prompt-file <prompt-file>
"""

import subprocess
import sys
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


def main():
    args, prompt = split_prompt_file(sys.argv[1:])
    codex = resolve_codex()
    if not codex:
        print("[wake-taiyang] 找不到任何 codex.exe", file=sys.stderr)
        return 127
    p = subprocess.run([str(codex)] + args,
                       input=prompt.encode("utf-8") if prompt is not None else None)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
