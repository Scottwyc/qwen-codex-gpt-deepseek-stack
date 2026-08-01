# Troubleshooting：19 项已知问题与修复记录

生产环境（Qwen Code + Codex + GPT 双后端）中逐步修复的问题汇总。
按时间顺序排列，编号即修复顺序。

| # | 问题 | 根因 | 修复 |
|---|------|------|------|
| 1 | Qwen Code `/model` 报 API key 错误 | `[ChatGPT]` 变体缺 `envKey` 字段 | settings.json 补充 `envKey: OPENAI_API_KEY` |
| 2 | `gpt-5.4-chatgpt` 返回 404 | 代理透传 `-chatgpt` 后缀 | 转发前剥离后缀 |
| 3 | `sk-` header 请求 30s hang | API key 直连 `api.openai.com` 被 GFW 阻断 | 两路由均 OAuth 优先 |
| 4 | Qwen Code turn hang（"Defragmenting memories"） | SSE `Connection: keep-alive` 不关闭 | Chat Completions → `close` + `shutdown(SHUT_WR)` |
| 5 | 400 `output_text` / `tools[0].name` 错误 | 消息 content type 写死 `input_text` + tools 嵌套 | 逐 role 转换 + tools 格式翻译 |
| 6 | 400 `Invalid value: 'function_call_output'` | function_call 嵌套在 content 数组内 | 改为顶层 input item |
| 7 | 非流式请求永久 hang | `[DONE]` 检测 bug（inner loop 消耗 buf） | done flag 替代 break+check |
| 8 | 大量 thinking token | `reasoning_effort` 未从 Chat Completions 映射 | 映射到 `responses_body["reasoning"]` |
| 9 | 工具调用 output 为空，模型只输出一句话 | `function_call` SSE 事件未翻译 | `_responses_to_chat_completion_chunk` 处理 3 种事件 |
| 10 | 代理 crash `KeyError: 'role'` | function_call item 无 role key，直接 `item["role"]` | 改用 `item.get("role")` |
| 11 | 非流式 tool_calls 丢失 | 非流式处理器仅监听 `output_text.delta` | 添加完整 function_call 事件解析 + `_flush_tc()` |
| 12 | content=None 时 tool_calls 丢失 | `str(None)`→`"None"`，tool_calls 在 str 分支内部 | tool_calls 处理移出 content 分支 |
| 13 | **tool call 循环调用（空参数）** | `function_call_arguments.delta` 重复带 id + 发送累计值，客户端按 id 去重跳过 | 按 OpenAI 标准：delta 不带 id、发送增量 |
| 14 | 400 `Invalid value: 'image_url'` | 图片消息未转换 | `image_url` → `input_image` |
| 15 | 输出截断（~400 tokens） | `reasoning_effort=none` 压低 ChatGPT 后端输出预算 | 改为 `medium`（~3000+ tokens） |
| 16 | **Codex turn hang（GPT OAuth）** | 透传流在 `response.completed` 后终止，keep-alive 连接不关闭，Codex 等待 `response.done` | 补发标准 `response.done` + `[DONE]` |
| 17 | **Platform API 直连流式 hang** | direct 透传 keep-alive 不关 + 无 `[DONE]` 检测 | `Connection: close` + `[DONE]` 检测 + `shutdown(SHUT_WR)` |
| 18 | **Platform API 400 reasoning/max_tokens** | Qwen Code 发 Responses 风格字段（nested `reasoning`、`max_tokens`），官方 Chat Completions 不接受 | body 清理：`reasoning` dict → `reasoning_effort`，`max_tokens` → `max_completion_tokens` |
| 18b | **Platform API 400 tools+reasoning** | 官方限制 gpt-5.x 在 /v1/chat/completions 中 tools 时 reasoning 必须 none | 有 tools 时动态降级 `reasoning_effort=none` |
| 19 | **Qwen Code "Model stream ended without a finish reason"** | 上游 ChatGPT 后端对超大上下文（~3.2MB / 1000+ 消息）请求中途断流（EOF），代理只发 `[DONE]`，从未发过带 finish_reason 的 chunk | Chat Completions 路径 EOF 时补发合成 `finish_reason:"stop"` chunk；passthrough 路径 EOF 时补发 `response.failed` + `[DONE]` |

## 关键修复细节

### 13：tool call 循环调用（核心）

**症状**：Qwen Code 反复失败 `Shell {}` / `WriteFile {}`（空参数），RETRY LOOP DETECTED。

**根因**：代理每个 `function_call_arguments.delta` chunk 都重复发送相同 `id` 和**累计值**。
客户端对相同 id 的 chunk 去重，只保留第一个（`arguments=""`），后续增量全部跳过。

**修复后的 SSE 格式**（OpenAI 标准）：

```json
// 第一个 chunk：带 id
{"delta": {"tool_calls": [{"index": 0, "id": "fc_...", "type": "function",
                           "function": {"name": "run_shell_command", "arguments": ""}}]}}
// 后续 delta：不带 id，只发增量
{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"co"}}]}}
// done：空 arguments + finish_reason
{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ""}}]},
 "finish_reason": "tool_calls"}
```

### 15：reasoning_effort 与输出预算

| reasoning_effort | ChatGPT 后端输出预算 | 结论 |
|------------------|---------------------|------|
| `none` | ~400 tokens | 复杂工具任务会被截断 |
| `medium` | ~3000+ tokens | 推荐 |

`reasoning_effort=none` 是为规避**官方 OpenAI API** 的 `400 Function tools with reasoning_effort`
错误而设置，但 ChatGPT 后端（`chatgpt.com/backend-api/codex/responses`）**支持** reasoning + tools。

### 16：Codex turn hang（ChatGPT OAuth 路径）

**症状**：Codex 调用 GPT（ChatGPT OAuth）时，代理 2 秒内完成响应（日志见
`terminal event observed`），但 Codex 客户端永久等待，进程不退出（turn hang）。

**根因**：`/responses` 路径是**字节级透传**上游 ChatGPT 后端的 SSE 流。透传循环检测到
`response.completed`（`_responses_sse_terminal_seen`）后立即 `return` 结束生成器。
但 `_sse_response` 使用 `Connection: keep-alive`（为兼容 Codex 不主动关闭），
连接保持打开却无后续数据。Codex 客户端在 `response.completed` 之后还期待
**`response.done`**（官方 Responses API 流的最终事件）来结束 turn → 永久 `epoll_wait`。

curl 测试不暴露此问题（curl 读完响应后自行关闭连接或超时退出），只有真实 Codex
客户端会 hang。

**修复**：透传检测到 terminal 事件后：
1. 继续读完上游剩余数据（不丢字节）
2. 解析上游 `response.completed` 中的真实 `response.id`
3. 补发标准事件：
```python
event: response.done
data: {"type": "response.done", "response": {"id": "<上游id>", ...}}

event: done
data: [DONE]
```

**验证**：`codex exec` 之前 40+ 秒不退出；修复后正常完成。
复杂工具任务（mkdir → write_file → read 回读）端到端成功，产物验证存在。

### 19：Model stream ended without a finish reason（超大上下文断流）

**症状**：Qwen Code（走代理 `-chatgpt`）在长任务运行到上下文极大时报
`✕[API Error: Model stream ended without a finish reason.]`，Ctrl+Y 重试仍反复报错。

**根因**：长任务上下文累积到 ~3.2MB（1000+ 条消息）后，上游 ChatGPT 后端
（`chatgpt.com/backend-api/codex/responses`）对这些超大请求**中途断流**（代理日志特征：
只有 `TTFB connect`，无 `first_token`、无 `[USAGE]`、无 `[FC] done`，`TIME 5-9s`）。
此时 `_handle_gpt_chat_to_stream` 读到上游 EOF 后只发 `data: [DONE]`，
**从未发过带 `finish_reason` 的 chunk** → Qwen Code 判定流异常终止。

**修复**：
1. Chat Completions 路径（Qwen Code）：循环内跟踪 `sent_finish`；EOF 时若从未发过
   finish_reason，补发合成 `finish_reason: "stop"` 空 delta chunk 再 `[DONE]`，并记 error 日志
2. passthrough 路径（Codex）：EOF 且未观察到 terminal 事件时，补发 `response.failed` + `[DONE]`，
   避免 Codex 客户端等待 `response.done` → turn hang

**验证**（2026-08-01，代理 v55）：
- 正常流式请求：内容 delta → `finish_reason:"stop"` → `[DONE]`，无回归
- 上游断流场景：不再报 finish reason 错误，流正常结束

**注意**：补发 stop 只保证客户端不报错/不 hang；断流点之后的输出内容确实丢失。
超大上下文（>3MB）仍会触发上游断流，建议长任务及时压缩上下文或开新会话。

## turn hang 最终定论（2026-08-01）

**turn hang 根因完全在综合代理的协议转换层，codex / Qwen Code 客户端本身无 bug。**

| 证据 | 说明 |
|------|------|
| fix 4 / fix 16 | 代理 SSE 终止行为错误（keep-alive 不关 / 缺 `response.done`） |
| codex 直连官方（`-p gpt`） | 复杂任务 39.5s 完整执行，无 hang |
| Qwen Code 直连官方（gpt-5.5） | 复杂流水线完整执行，无 hang |

codex 按官方协议等待 `response.done` 是正确行为；代理违反协议导致客户端无限等待。

## Platform API 直连指南（Qwen Code + api.openai.com）

**可行，但官方限制：gpt-5.x 在 `/v1/chat/completions` 中 tools 时 `reasoning_effort` 必须为 `none`**。
Qwen Code 是 agent 模式（任何请求都带 tools），因此直连时**无推理**。

### 配置（settings.json）

```jsonc
{
  "id": "gpt-5.5",
  "name": "gpt-5.5",
  "envKey": "OPENAI_API_KEY",
  "baseUrl": "https://api.openai.com/v1",
  "generationConfig": {
    "timeout": 120000,
    "contextWindowSize": 1000000,
    "samplingParams": {
      "reasoning_effort": "none",
      "max_completion_tokens": 8192
    }
  }
}
```

要点：
- **id 必须是官方模型名**（不能带自定义后缀，会 404）
- 必须设置 `samplingParams`（否则 Qwen Code 注入 nested `reasoning` dict → 400）
- `max_completion_tokens` 替代 `max_tokens`（gpt-5.x 不支持后者）

### 验证结果（2026-08-01 独立 Qwen Code 窗口实测）

- 工具调用 ✅（`Shell echo` 输出正确）
- 复杂流水线 ✅（写脚本→运行→统计→读→报告→验证，5 产物全生成，60s 内完成）
- turn hang ✅ 无（官方原生 SSE 自带 `[DONE]`）

### 直连 vs 走代理

| 维度 | 直连官方 | 走代理（-chatgpt） |
|------|---------|-------------------|
| reasoning | ❌ 必须 none | ✅ medium（动态降级） |
| 输出预算 | 固定 max_completion_tokens | 长输出支持 |
| 推荐场景 | 轻量查询 | 复杂编码/推理任务 |

## ChatGPT 后端限制（不可绕过）

- `max_output_tokens` / `max_tokens`：均返回 400 `Unsupported parameter`
- 无法通过代理控制输出长度；通过 `reasoning_effort` 间接影响输出预算
