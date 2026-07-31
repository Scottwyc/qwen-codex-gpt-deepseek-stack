# Troubleshooting：15 项已知问题与修复记录

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

## ChatGPT 后端限制（不可绕过）

- `max_output_tokens` / `max_tokens`：均返回 400 `Unsupported parameter`
- 无法通过代理控制输出长度；通过 `reasoning_effort` 间接影响输出预算
