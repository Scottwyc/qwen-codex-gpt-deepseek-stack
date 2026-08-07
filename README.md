# qwen-codex-gpt-deepseek-stack

**Qwen Code + Codex CLI + GPT + DeepSeek 四合一综合代理方案**

通过一个本地综合代理，让 [Qwen Code](https://github.com/QwenLM/Qwen-Code) 和 [Codex CLI](https://github.com/openai/codex) 同时使用：
- **GPT 模型**（gpt-5.4 / 5.5 / 5.6 系列）— 走 **ChatGPT 订阅 OAuth** 或 **OpenAI Platform API**
- **DeepSeek 模型**（deepseek-v4-pro / flash）

## 架构

```
┌─────────────┐     ┌─────────────┐
│  Qwen Code  │     │  Codex CLI  │
│ (Chat Comp.)│     │(Responses)  │
└──────┬──────┘     └──────┬──────┘
       │                   │
       ▼                   ▼
┌─────────────────────────────────────┐
│       本地综合代理 (127.0.0.1:11435)  │
│  - Chat Completions ↔ Responses 转换 │
│  - SSE 流式翻译（tool_calls 等）      │
│  - 按模型名路由：deepseek-* → DS      │
│  - GPT 双认证：OAuth 优先 + API key   │
└──────┬──────────────────┬───────────┘
       │                  │
       ▼                  ▼
┌────────────────┐  ┌──────────────────┐
│ OpenAI         │  │ api.deepseek.com │
│ (API key/OAuth)│  │ (DeepSeek)       │
└────────────────┘  └──────────────────┘
```

## 组件

| 目录 | 说明 |
|------|------|
| [`proxy/`](proxy/) | 综合代理（基于 codex-deepseek 扩展，见下方引用） |
| [`qwen-code/`](qwen-code/) | Qwen Code 配置模板（脱敏）与集成说明 |
| [`chatgpt-oauth/`](chatgpt-oauth/) | ChatGPT OAuth 设备码登录流程 |
| [`docs/codex-dual-mode.md`](docs/codex-dual-mode.md) | **推荐架构**：Codex 统一走代理单 provider（GPT/DS 切换 + 历史共享） |
| [`tests/`](tests/) | 回归测试脚本 |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 19 项已知问题与修复记录 |

## 快速开始

```bash
# 1. 启动综合代理（Codex 全模型 + Qwen Code GPT 都需要）
cd proxy
cp .env.example .env   # 填入 DeepSeek API key（api_key）；建议同时填 openai_api_key（GPT API key 兜底）
./start.sh             # 监听 127.0.0.1:11435

# 2. Codex 统一走代理（推荐，历史共享）
#    基础 config.toml: model_provider="custom" → 127.0.0.1:11435
codex                  # /model 可切换 GPT/DS 全部模型，会话历史共享

# 3. Qwen Code 也可用（可选）
cp qwen-code/settings.example.json ~/.qwen/settings.json
# 编辑 env 部分填入真实 key（ChatGPT OAuth 模式 OPENAI_API_KEY 可留空）
```

## 验证

```bash
# 简单对话
bash tests/test_complex_tool_call.sh

# 复杂多轮工具调用（mkdir → write_file → run_shell → read → edit → run_shell）
# 已在 Qwen Code 端到端验证完整 8 步链路

# Codex 历史共享验证（2026-08-07）
# 1) /model 切 gpt-5.6-luna → 对话
# 2) /model 切 deepseek-v4-flash → 对话
# 3) 退出后 codex resume → 恢复任一会话，历史+模型保留，正常对话
```

## 引用与来源

本项目中的综合代理是以下开源项目基础上的**生产级扩展**：

1. **[ccswitch-deepseek](https://github.com/liuzhengming/ccswitch-deepseek)** — 原始设计参考：将 Responses API 转换为 Chat Completions API 的协议翻译代理
2. **[codex-deepseek](https://github.com/yangfei4913438/codex-deepseek)** — 直接 fork 演化的 Python 移植版（本仓库 proxy/ 以此为基础）

本仓库在其之上新增的核心能力：
- **GPT 双重路由**：ChatGPT OAuth（订阅）优先，Platform API key 回退（2026-08-07 修复 API key 路径：`openai_api_key` 配好后 GPT 全走通）
- **按模型名路由**：同一端口服务 GPT + DeepSeek（`deepseek-*` → DS，`gpt-*` → OpenAI）
- **Chat Completions ↔ Responses 全格式转换**：消息、tools、tool_choice、reasoning、多模态
- **SSE 流式翻译**：`function_call` 事件 → OpenAI 标准 `tool_calls` 增量格式
- **Codex 会话历史共享**：所有会话统一 custom provider → GPT/DS 历史可互相 resume（2026-08-07 实测）
- **19 项生产环境 bug 修复**（详见 `docs/troubleshooting.md`，最新：超大上下文断流 EOF 补发 `finish_reason`）

## License

MIT（详见 `proxy/LICENSE`，继承自上游项目）。
