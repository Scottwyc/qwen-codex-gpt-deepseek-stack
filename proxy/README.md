# 综合代理（codex-deepseek 扩展版）

协议翻译代理：将 OpenAI Responses API / Chat Completions API 统一转发到
**ChatGPT 订阅后端**（OAuth）或 **DeepSeek API**，支持 Codex 与 Qwen Code 双客户端。

## 来源

- **原始参考**：[ccswitch-deepseek](https://github.com/liuzhengming/ccswitch-deepseek)
- **fork 演化**：[codex-deepseek](https://github.com/yangfei4913438/codex-deepseek)

本目录为生产级扩展版本，新增 GPT OAuth 路由、Chat Completions 支持、SSE 工具调用翻译等。

## 启动

```bash
cp .env.example .env    # 填入 DeepSeek API key（GPT 走 OAuth 无需 key）
./start.sh              # 监听 127.0.0.1:11435
```

生产环境建议持久运行：

```bash
tmux new-session -d -s "proxy_daemon" -c "$PWD" \
  "python3 -u -m src.main 2>&1 | tee /tmp/proxy.log"
```

## 环境变量（.env）

| 变量 | 说明 |
|------|------|
| `api_key` | DeepSeek API key（必需） |
| `base_url` | DeepSeek API base（默认 `https://api.deepseek.com`） |
| `port` | 代理监听端口（默认 `11435`） |
| `openai_api_key` | 可选，Platform API key 回退路径 |
| `gpt_model` | 默认 GPT 模型（默认 `gpt-5.6-sol`） |
| `gpt_chatgpt_backend_base_url` | ChatGPT 后端（默认 `https://chatgpt.com/backend-api/codex`） |
| `gpt_enable_app_server_fallback` | 是否启用 app-server 回退（默认 false） |

完整变量见 `.env.example`。

## 路由规则

| 请求路径 | 客户端 | 后端 |
|----------|--------|------|
| `/responses` | Codex CLI | GPT OAuth / DeepSeek |
| `/v1/chat/completions` | Qwen Code | GPT OAuth / Platform API |
| `/v1/responses` | 其他 OpenAI SDK | 同上 |

GPT 路由优先级：**ChatGPT OAuth → Platform API key → 错误**。

## 测试

```bash
cd ..
bash tests/test_complex_tool_call.sh   # 4 项 curl 回归测试
python3 -m pytest tests/ -q             # 单元测试（translate 等）
```
