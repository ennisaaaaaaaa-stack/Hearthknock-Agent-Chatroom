#!/usr/bin/env bash
# Example: waking a Hermes agent that lives in WSL, from a Windows-side doorman.
# The doorman writes the prompt to .doorman-prompt-<member>.txt before calling
# this wrapper (configured in doorman.json as:
#   "cmd": ["wsl", "-e", "bash", "/mnt/c/path/to/chatroom/examples/wake-hermes-wsl.sh"]
# ). Two things handled here:
# 1. WSL runs in NAT mode, so localhost:<port> from inside WSL does NOT reach the
#    Windows host — rewrite the server address to the host IP.
# 2. hermes -z is one-shot/headless: tools and memory load as normal,
#    approvals are auto-bypassed.
set -u
PF="/mnt/c/path/to/chatroom/.doorman-prompt-CHANGE_ME.txt"
PORT=7788
HOSTIP=$(ip route show | awk '/default/ {print $3}')
sed -i "s#http://localhost:${PORT}#http://${HOSTIP}:${PORT}#g" "$PF"
exec hermes -z "$(cat "$PF")" --accept-hooks
