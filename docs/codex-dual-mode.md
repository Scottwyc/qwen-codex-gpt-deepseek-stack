# Codex 统一 provider 方案：GPT + DeepSeek 走综合代理（共享会话历史）

**推荐架构（2026-08-07 v3 定版，实测验证）**：所有 Codex 会话统一
`model_provider="custom"` → 本地综合代理 `127.0.0.1:11435`，
代理按模型名路由：`deepseek-*` → DeepSeek 官方，`gpt-*` → OpenAI（Platform API key / ChatGPT OAuth）。

```
codex（所有会话，无需 -p profile）
   │
   ▼
本地综合代理 :11435（model_provider="custom"）
   ├─ deepseek-* → api.deepseek.com（Responses 翻译）
   └─ gpt-*      → api.openai.com（Platform API key）
                 └─ chatgpt.com（ChatGPT OAuth，备用）
```

## 为什么这是最终方案

### 1. 会话历史完全统一（关键收益）

Codex 会话文件记录 `model_provider`（openai/deepseek/custom）。若 GPT 直连（openai）
与 DS 直连（deepseek）混用，**恢复对方 provider 创建的会话时请求发往错误端点**
→ `model_not_found`（2026-08-07 实测复现）。

统一为 `custom` 后：所有会话同一 provider 身份 → 恢复任意历史会话请求都到代理 →
代理按模型名路由 → **GPT 会话与 DS 会话可自由 resume，历史完全共享**。

### 2. 一个会话内 /model 双向切换

`/model` 只切换模型名（gpt-5.6-luna ↔ deepseek-v4-flash），不切 provider。
单 provider 架构下天然支持，无需退出重启。

### 3. GPT 双重认证路由（OAuth + API key 都可用）

代理 GPT 路由：**ChatGPT OAuth（订阅）优先 → Platform API key 回退**。
- 有有效 OAuth tokens（`~/.codex/auth.json` / `auth.gpt.json`）→ 走订阅
- 否则用 `openai_api_key`（.env）→ 走 Platform API
- 两条路都已实测走通

## 配置

### `~/.codex/config.toml`（基础配置）

```toml
model_provider = "custom"
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
model_reasoning_summary = "auto"
model_supports_reasoning_summaries = true
model_context_window = 1050000
model_catalog_json = "/home/wuyangcheng/.codex/model-catalogs/unified.json"

[model_providers.custom]
name = "codex-deepseek"
base_url = "http://127.0.0.1:11435"
wire_api = "responses"
requires_openai_auth = false
stream_idle_timeout_ms = 1800000
```

`model_catalog_json`（unified.json）声明 GPT + deepseek 合并模型目录，
`/model` 列表可切换全部 8 个模型。

### 代理 `.env`（综合代理目录）

```
api_key=            # DeepSeek API key（必需）
openai_api_key=     # OpenAI Platform API key（GPT 走 API key 路径必需；
                    #   有有效 OAuth 时可留空）
```

## 使用

```bash
codex                    # 综合代理单 provider：GPT/DS 均可，/model 切换，历史共享
codex -p ds              # 备用：DeepSeek 官方直连（历史与主会话不通用，仅应急）
codex -p gpt             # 备用：GPT 官方直连
```

## 验证记录（2026-08-07）

| 场景 | 结果 |
|------|------|
| curl 代理 gpt-5.6-luna（API key） | ✅ `GPT (OpenAI API key preferred)` 返回正确 |
| curl 代理 deepseek-v4-flash | ✅ 返回正确 |
| codex /model 切 GPT → DS → GPT | ✅ 双向切换对话正常 |
| **退出后 resume 恢复 DS 会话** | ✅ 历史+模型保留，新消息正常（不再 model_not_found） |
| resume 恢复迁移后的旧直连会话 | ✅ 历史完整，走代理回复 |
| GPT OAuth 路径（有效 token 时） | ✅ 已实现，需 OAuth token 有效 |

## 历史方案（供参考）

- **v2（2026-08-07 上午）**：DeepSeek 官方直连 profile（`-p ds` + `[model_providers.deepseek]`）
  —— 发现会话历史不通用，放弃
- **v1（2026-08-01）**：GPT 直连 + DS 走代理双 profile（gpt.config.toml / ds.config.toml）
  —— 双 provider 身份，历史不通用，弃用

## 注意事项

- **`/model` 切换会持久化写回 config.toml 的 `model` 字段**（0.146 行为）：
  切到 deepseek 后 config.toml 默认模型变为 deepseek-v4-flash（provider 仍是 custom，
  代理会正确路由，无碍）；如需 GPT 默认启动手动改回。
- **OAuth 失效陷阱**：代理 OAuth 优先且失效不自动回退 API key。若 `auth.gpt.json`
  含失效 tokens 导致 GPT 401，移走该文件即可强制走 API key 路径（登录 `codex login --device-auth`
  重新生成后自动恢复 OAuth 优先）。
