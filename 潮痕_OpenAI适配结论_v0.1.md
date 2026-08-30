# 潮痕（Tideline）× OpenAI 适配结论 v0.1

> 调研：太阳哥  
> 日期：2026-08-30  
> 对象：ChatGPT Chat、ChatGPT Work、Codex

## 结论先行

1. **Chat 端不是没有工具能力。** Chat 与 Work 都支持插件；插件可携带 MCP 工具，同一个插件可在两个模式中使用。
2. **潮痕当前的 stdio MCP 可以接 Chat/Work，第一步不必改成 HTTP。** OpenAI Secure MCP Tunnel 能在本机以 stdio 命令启动私有 MCP，再通过出站 HTTPS 让 ChatGPT 调用；潮痕数据和服务无需暴露到公网。
3. **Chat/Work 没有文档化的通用逐轮生命周期 hook。** 因此可以稳定获得“主动调用的记忆工具”，但不能只靠现有 stdio MCP 保证每轮自动注入、每轮自动落盘。
4. **Codex 可以做到自动注入/落盘。** Codex 官方 hooks 有 `SessionStart`、`UserPromptSubmit`、`PreCompact`、`Stop`、`SessionEnd` 等事件，可调用脚本或已连接的 MCP 工具。
5. **Chat 与 Work 可以共享上下文。** 同一 ChatGPT Project 中，Chat 和 Work 共享项目文件、指令与连接源；它们也使用账号/工作区可用的 ChatGPT memory。**但本地 Codex memory 是另一套存储，不与 ChatGPT memory 原生合并。** 若三端统一，以潮痕同一数据库作为外部事实源最稳。

## 潮痕现状

- [`server.py`](https://github.com/ennisaaaaaaaa-stack/tideline-memory/blob/main/server.py) 是标准 MCP server，但入口固定为 `stdio_server()`。
- [`tideline_provider.py`](https://github.com/ennisaaaaaaaa-stack/tideline-memory/blob/main/plugins/tideline_provider.py) 为 Hermes provider，实现：
  - `system_prompt_block`：T0/T2/T2b/T3；
  - `prefetch`：T1/T4；
  - `sync_turn`：原始回合写入；
  - `on_pre_compress`：压缩前兜底。
- MCP server 已提供 `memory_*`、`context_*` 工具，但 provider 的自动注入能力没有暴露成一个通用 MCP 工具。

## 建议增加的两个通用接口

保留现有细粒度工具，再加两个跨 runtime 的组合接口：

```yaml
memory_context:
  input:
    agent_id: string
    session_id: string
    query: string | null
    token_budget: integer
    layers: [T0, T1, T2, T2b, T3, T4]
  output:
    context_block: string
    memory_ids: [integer]
    truncated: boolean
    diagnostics: object

context_record_turn:
  input:
    event_id: string       # 幂等键
    agent_id: string
    session_id: string
    surface: chat | work | codex
    user_text: string
    assistant_text: string
    created_at: ISO-8601
  output:
    recorded: boolean
    context_ids: [integer]
```

原因：

- 避免每个 runtime 各自复制一遍 T0–T4 拼接逻辑；
- 让 Chat/Work 一次工具调用即可拿到有预算上限的上下文；
- 用 `event_id` 防止 hook 重试或多端同步造成重复记忆；
- `surface + agent_id + session_id` 保留来源，未来可决定同人格共享或不同人格隔离。

## 三端接法

### ChatGPT Chat / Work

1. 用 Secure MCP Tunnel 直接运行现有 `python server.py`（stdio）。
2. 创建私有 developer-mode 插件，Chat 与 Work 安装同一个插件，指向同一潮痕数据库。
3. 插件内带一个记忆 skill：新对话先调用 `memory_context`；重要事件和对话收束时调用 `context_record_turn`。
4. 把 Chat 与 Work 放进同一个 ChatGPT Project，共享项目指令与 Sources。

限制：skill/模型主动调用能提高命中率，但不等于生命周期级强保证。若必须“每轮必注入、必落盘”，需要自建 Responses API 编排层，或等待 Chat/Work 提供等价 hooks。

Secure MCP Tunnel 需要 Platform tunnel 权限、运行时 API key 和 ChatGPT developer-mode 权限；它适合私有使用，不能直接用于公开插件分发。若账号/工作区暂时没有 Tunnel 能力，备选方案才是给潮痕增加受认证的 Streamable HTTP MCP transport。

### Codex

- `SessionStart(startup|resume|compact)`：调用 `memory_context`，把返回值作为 `additionalContext`。
- `UserPromptSubmit`：以用户 prompt 为 query 调 `memory_context`，做逐轮 T1/T4 召回。
- `Stop`：异步调用 `context_record_turn`；只写原文，embedding 可异步补齐。
- `PreCompact`：触发 scan/solidify checkpoint，避免压缩前上下文漏存。
- `SessionEnd`：只做轻量兜底；官方默认时间很短，不放重 embedding 工作。

注意：`SessionStart` 时 MCP 连接可能尚未 ready，`SessionEnd` 也不支持 MCP-tool hook。稳妥实现是 command hook 调一个潮痕 CLI/共享库；MCP 继续负责交互工具。

## 对架构的短评

潮痕把“原始上下文、结构化叙事、身份状态、主动召回、夜间巩固”拆开是对的，且 WAL + 确定性脚本层让它容易维护。当前最大的可移植性缺口不是存储或检索，而是 provider 逻辑只落在 Hermes 抽象上。把 provider 核心抽成纯函数/共享库，再由 Hermes provider、Codex hooks、MCP `memory_context` 三个薄适配器调用，会比给每个 runtime 重写一份注入逻辑稳健。

另一个风险是默认 T0+T2+T2b+T3 约 9k–11k tokens。跨端接入时应把 `token_budget`、分层开关、返回 memory IDs 与截断诊断作为正式接口，避免“记得更多”反而挤掉当前任务。

## 安全与隔离

- 当前 schema 没有 `agent_id/workspace_id` 列，`AGENT_NAME` 只是显示标签，不是权限边界。太阳哥单人格单库可以直接用；多个 agent 共服务前，要么每人格分库，要么给所有表和查询补强制 namespace，不能只相信模型传入的 `agent_id`。
- SQLite 文件只由潮痕服务所在主机访问，不通过网络文件系统共享；多端都走同一服务接口。
- 读、候选写入、确认写入建议分 scope；保留审计、幂等键、速率限制与删除/导出能力。
- 被召回的记忆按“不可信数据”处理，只能提供事实上下文，不能覆盖 system/developer 指令。
- 若 embedding 指向远程 API，对话文本会离开本机。家庭私密内容优先本地 bge-m3，或先做脱敏与保留策略。

## 官方依据

- [Skills & Plugins：ChatGPT 与 Codex 可共享插件目录，插件可含 MCP connectors](https://learn.chatgpt.com/docs/skills-and-plugins)
- [Plugins：插件可在 Chat 和 Work 使用，并提供 MCP 工具](https://learn.chatgpt.com/docs/plugins)
- [Projects and chats：同一 Project 中 Chat 与 Work 共享项目上下文](https://learn.chatgpt.com/docs/projects)
- [Memories：ChatGPT memory 与本地 Codex memory 是分开的](https://learn.chatgpt.com/docs/customization/memories)
- [Codex Hooks：SessionStart/UserPromptSubmit/PreCompact/Stop/SessionEnd](https://learn.chatgpt.com/docs/hooks)
- [Secure MCP Tunnel：私有 stdio/HTTP MCP 可通过出站隧道接 ChatGPT/Codex](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
