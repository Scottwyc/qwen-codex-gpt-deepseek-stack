# Codex 双模方案：GPT 直连 + DeepSeek 走代理

推荐架构（2026-08-01 验证）：codex 用 `-p/--profile` 双配置，
**GPT 原生直连**（ChatGPT OAuth，官方 SSE 完整事件 → 天然无 turn hang），
**DeepSeek 走综合代理**（仍享受协议转换与超时重试）。

```
codex -p gpt            codex -p ds
   │                        │
   ▼                        ▼
官方 ChatGPT OAuth      本地综合代理 :11435
(api.openai.com 原生)      │
                           ├─ DeepSeek API
```

## 为什么

- **GPT 直连**：官方 Responses API 自带 `response.done` 事件序列，
  客户端永远能正常结束 turn —— 从根源避免 turn hang
- **DS 走代理**：DeepSeek 无原生 Responses API，仍需协议转换
- 顺带绕开 `chatgpt.com` usage/rate-limit 端点的 Cloudflare 风控
  （综合代理转发 `/usage` 类请求头不完整时会被 403）

## 配置

### `~/.codex/gpt.config.toml`

```toml
model_provider = "openai"
model = "gpt-5.5"
model_reasoning_effort = "medium"
approval_policy = "never"
sandbox_mode = "danger-full-access"
```

### `~/.codex/ds.config.toml`

```toml
model_provider = "custom"
model = "deepseek-v4-flash"

[model_providers.custom]
name = "codex-deepseek"
base_url = "http://127.0.0.1:11435"
wire_api = "responses"
requires_openai_auth = false
stream_idle_timeout_ms = 1800000
```

基础 `~/.codex/config.toml` 不设 `model_provider`（默认直连）。

## 使用

```bash
codex -p gpt    # GPT 直连
codex -p ds     # DeepSeek 走代理
```

## 验证（2026-08-01）

| 场景 | 结果 |
|------|------|
| `-p gpt` mkdir→write→run 工具链 | ✅ 13.9s，无 hang |
| `-p gpt` edit→run→read→write 多轮 | ✅ 24.4s，无 hang |
| `-p ds` 工具任务 | ✅ 7.9s，deepseek-v4-flash 路由确认 |
| 代理 GPT 透传（fix 16 回归） | ✅ response.completed→response.done→[DONE] |
