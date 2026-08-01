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
│  - OAuth 优先路由（避免 GFW 阻断）    │
└──────┬──────────────────┬───────────┘
       │                  │
       ▼                  ▼
┌────────────────┐  ┌──────────────────┐
│ chatgpt.com    │  │ api.deepseek.com │
│ (ChatGPT 订阅)  │  │ (DeepSeek)       │
└────────────────┘  └──────────────────┘
```

## 组件

| 目录 | 说明 |
|------|------|
| [`proxy/`](proxy/) | 综合代理（基于 codex-deepseek 扩展，见下方引用） |
| [`qwen-code/`](qwen-code/) | Qwen Code 配置模板（脱敏）与集成说明 |
| [`chatgpt-oauth/`](chatgpt-oauth/) | ChatGPT OAuth 设备码登录流程 |
| [`docs/codex-dual-mode.md`](docs/codex-dual-mode.md) | **推荐架构**：Codex GPT 直连 + DS 走代理双 profile |
| [`tests/`](tests/) | 回归测试脚本 |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 19 项已知问题与修复记录 |

## 快速开始

```bash
# 1. 启动综合代理（DS 需要；GPT 直连不需要代理）
cd proxy
cp .env.example .env   # 填入 DeepSeek API key
./start.sh             # 监听 127.0.0.1:11435

# 2. ChatGPT OAuth 登录（GPT 直连必需）
codex login --device-auth   # 生成 ~/.codex/auth.json

# 3. Codex 双模使用（推荐架构，详见 docs/codex-dual-mode.md）
codex -p gpt    # GPT 直连（ChatGPT OAuth，无 turn hang）
codex -p ds     # DeepSeek 走综合代理

# 4. Qwen Code 也可用（可选）
cp qwen-code/settings.example.json ~/.qwen/settings.json
# 编辑 env 部分填入真实 key（ChatGPT OAuth 模式 OPENAI_API_KEY 可留空）
```

## 验证

```bash
# 简单对话
bash tests/test_complex_tool_call.sh

# 复杂多轮工具调用（mkdir → write_file → run_shell → read → edit → run_shell）
# 已在 Qwen Code 端到端验证完整 8 步链路
```

## 引用与来源

本项目中的综合代理是以下开源项目基础上的**生产级扩展**：

1. **[ccswitch-deepseek](https://github.com/liuzhengming/ccswitch-deepseek)** — 原始设计参考：将 Responses API 转换为 Chat Completions API 的协议翻译代理
2. **[codex-deepseek](https://github.com/yangfei4913438/codex-deepseek)** — 直接 fork 演化的 Python 移植版（本仓库 proxy/ 以此为基础）

本仓库在其之上新增的核心能力：
- **GPT 双重路由**：ChatGPT OAuth（订阅）优先，Platform API key 回退
- **Chat Completions ↔ Responses 全格式转换**：消息、tools、tool_choice、reasoning、多模态
- **SSE 流式翻译**：`function_call` 事件 → OpenAI 标准 `tool_calls` 增量格式
- **19 项生产环境 bug 修复**（详见 `docs/troubleshooting.md`，最新：超大上下文断流 EOF 补发 `finish_reason`）

## License

MIT（详见 `proxy/LICENSE`，继承自上游项目）。
