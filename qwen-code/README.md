# Qwen Code + GPT 集成

通过本地综合代理，Qwen Code 可以使用：
- **ChatGPT 订阅（OAuth）**：`-chatgpt` 后缀模型（推荐，无需 OpenAI Platform API key）
- **OpenAI Platform API**：非 `-chatgpt` 模型（需要 `OPENAI_API_KEY`）

> **已验证（2026-08-07）**：Qwen Code 走综合代理同时打通 GPT 的 **API key** 与 **OAuth** 两条路：
> - 代理 `.env` 配 `openai_api_key` → 非 `-chatgpt` 模型走 Platform API（当前生效路径）
> - 代理检测到有效 ChatGPT OAuth tokens → `-chatgpt` 模型走订阅
> - 两条路都经 `http://127.0.0.1:11435/v1` 一个端点，代理按模型后缀/认证自动选择

## 配置步骤

### 1. 安装综合代理

见 [`../proxy/README.md`](../proxy/README.md)。代理默认监听 `127.0.0.1:11435`。

### 2. 配置 Qwen Code settings

```bash
# 复制脱敏模板（不含任何真实密钥）
cp settings.example.json ~/.qwen/settings.json
# 编辑：替换 env 中的占位符
vim ~/.qwen/settings.json
```

### 3. 关键配置说明

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `modelProviders.openai[].baseUrl` | `http://127.0.0.1:11435/v1` | GPT 模型走本地代理 |
| `env.OPENAI_API_KEY` | Platform key 或空 | ChatGPT OAuth 模式下可为空 |
| `gpt-5.6*.extra_body.reasoning_effort` | `"medium"` | **必须**。`none` 时 ChatGPT 后端输出预算仅 ~400 tokens |
| `samplingParams.max_completion_tokens` | 不要设置 | ChatGPT 后端返回 400 `Unsupported parameter` |

### 4. 选择模型

在 Qwen Code 中 `/model`，选择 `gpt-5.6-sol [ChatGPT]` 等 `[ChatGPT]` 变体。

## 模型命名约定

| Qwen Code id | 路由 |
|--------------|------|
| `gpt-5.6-sol-chatgpt` | ChatGPT OAuth → `gpt-5.6-sol` |
| `gpt-5.6-sol` | Platform API → `gpt-5.6-sol` |
| `gpt-5.6-luna-chatgpt` | ChatGPT OAuth → `gpt-5.6-luna` |

`-chatgpt` 后缀仅在 Qwen Code 中用于区分路由，代理层自动剥离后转发给后端。

## 已知限制

- **ChatGPT 后端不支持 `max_output_tokens` / `max_tokens`**：任何输出长度控制参数都会返回 400
- **`reasoning.effort=none` 输出预算低**：约 400 tokens，复杂工具任务会被截断。使用 `medium`（约 3000+ tokens）
- **多模态图片**：Qwen Code 发送 `image_url`，代理自动转换为后端支持的 `input_image`
- **超大上下文断流**：上下文累积到 ~3MB（1000+ 消息）时 ChatGPT 后端可能中途断流。代理会补发合成
  `finish_reason`，客户端不再报 `Model stream ended without a finish reason`，但断流点后的内容会丢失。
  建议长任务及时压缩上下文或开新会话（详见 [`../docs/troubleshooting.md`](../docs/troubleshooting.md) 第 19 项）

## Platform API 直连（可选，不经过代理）

有 OpenAI Platform API key 时，可让 `gpt-5.5` 直连 `api.openai.com`：

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

⚠️ 注意：
- **id 必须是官方模型名**（不能带自定义后缀）
- **官方限制**：gpt-5.x 在 `/v1/chat/completions` 中带 tools 时 `reasoning_effort` 必须为 `none`（Qwen Code 是 agent 模式总带 tools）→ 直连**无推理**
- 需要 `samplingParams`（否则 Qwen Code 发 nested `reasoning` → 400）和 `max_completion_tokens`（替代 `max_tokens`）
- 需要推理能力时推荐走代理（`-chatgpt`，medium 动态降级）
