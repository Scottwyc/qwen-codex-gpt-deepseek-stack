# ChatGPT OAuth（设备码登录）

综合代理支持通过 **ChatGPT 订阅账号**（而非 OpenAI Platform API）调用 GPT 模型。
使用 `chatgpt.com/backend-api/codex/responses` 后端，需要 ChatGPT Plus/Pro 订阅。

## 原理

```
Qwen Code / Codex
    │  Chat Completions / Responses API
    ▼
本地综合代理 (127.0.0.1:11435)
    │  OAuth Bearer token
    ▼
chatgpt.com/backend-api/codex/responses  (ChatGPT 订阅)
```

OAuth token 保存在 `~/.codex/auth.json`，代理自动读取。

## 登录流程（设备码）

```bash
# 1. 安装 codex CLI（用于生成 OAuth token）
# https://github.com/openai/codex

# 2. 首次登录（会显示设备码，浏览器打开并确认）
codex login

# 3. 验证 token 生成
ls ~/.codex/auth.json
```

`auth.json` 包含 `access_token` / `refresh_token`，代理会：
1. 优先使用 OAuth token 访问 ChatGPT 后端
2. token 过期时自动刷新
3. 无 token 时回退到 Platform API key

## 注意事项

- `auth.json` 是**敏感文件**，不要提交到 git 仓库
- 网络环境需能访问 `chatgpt.com`（中国大陆需代理）
- OAuth 路径优先于 Platform API key（避免 GFW 阻断 `api.openai.com`）

## 验证登录

```bash
# 通过代理发送一个 GPT 请求（ChatGPT OAuth 路由）
curl -s http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test" \
  -d '{"model": "gpt-5.6-sol-chatgpt", "messages": [{"role": "user", "content": "hi"}]}'
```

如果返回正常内容，说明 OAuth 路由工作正常。
