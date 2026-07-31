#!/bin/bash
# 测试复杂工具多轮调用：需要工具调用 + 工具结果 + 继续对话

echo "=============================================="
echo "Test 1: 简单对话（无工具）"
echo "=============================================="
RESP1=$(curl -s -w "\n%{http_code}" --max-time 30 \
  http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test" \
  -d '{
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [
      {"role": "user", "content": "Say hello in exactly 3 words."}
    ],
    "stream": false
  }')
HTTP_CODE=$(echo "$RESP1" | tail -1)
BODY=$(echo "$RESP1" | sed '$d')
echo "HTTP: $HTTP_CODE"
echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d['choices'][0]['message']['content']; print('Content:', c[:200]); print('Finish:', d['choices'][0].get('finish_reason','?'))" 2>&1
echo ""

echo "=============================================="
echo "Test 2: 工具调用请求（需要调用工具）"
echo "=============================================="
RESP2=$(curl -s -w "\n%{http_code}" --max-time 60 \
  http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test" \
  -d '{
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [
      {"role": "user", "content": "What is 2+2? And can you also tell me the current weather in Beijing using the get_weather tool?"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string", "description": "City name"}
            },
            "required": ["city"]
          }
        }
      },
      {
        "type": "function",
        "function": {
          "name": "calculate",
          "description": "Evaluate a mathematical expression",
          "parameters": {
            "type": "object",
            "properties": {
              "expression": {"type": "string", "description": "Math expression to evaluate"}
            },
            "required": ["expression"]
          }
        }
      }
    ],
    "stream": false
  }')
HTTP_CODE2=$(echo "$RESP2" | tail -1)
BODY2=$(echo "$RESP2" | sed '$d')
echo "HTTP: $HTTP_CODE2"
python3 -c "
import sys, json
d = json.loads(sys.stdin.read().strip())
msg = d['choices'][0]['message']
print('Content:', str(msg.get('content',''))[:200])
tcs = msg.get('tool_calls', [])
if tcs:
    print(f'Tool calls: {len(tcs)}')
    for tc in tcs:
        fn = tc.get('function', {})
        print(f'  - {fn.get(\"name\",\"?\")}: {fn.get(\"arguments\",\"{}\")[:200]}')
print('Finish:', d['choices'][0].get('finish_reason','?'))
" <<< "$BODY2" 2>&1
echo ""

echo "=============================================="
echo "Test 3: 工具结果 + 继续对话（多轮工具调用）"
echo "=============================================="
RESP3=$(curl -s -w "\n%{http_code}" --max-time 60 \
  http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test" \
  -d '{
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [
      {"role": "user", "content": "What is the weather in Beijing and Shanghai? Also calculate 15*7."},
      {"role": "assistant", "content": null, "tool_calls": [
        {"id": "call_001", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Beijing\"}"}},
        {"id": "call_002", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Shanghai\"}"}},
        {"id": "call_003", "type": "function", "function": {"name": "calculate", "arguments": "{\"expression\": \"15*7\"}"}}
      ]},
      {"role": "tool", "tool_call_id": "call_001", "content": "Beijing: Sunny, 32°C"},
      {"role": "tool", "tool_call_id": "call_002", "content": "Shanghai: Cloudy, 28°C"},
      {"role": "tool", "tool_call_id": "call_003", "content": "105"}
    ],
    "stream": false
  }')
HTTP_CODE3=$(echo "$RESP3" | tail -1)
BODY3=$(echo "$RESP3" | sed '$d')
echo "HTTP: $HTTP_CODE3"
python3 -c "
import sys, json
d = json.loads(sys.stdin.read().strip())
msg = d['choices'][0]['message']
content = msg.get('content','')
print('Content:', str(content)[:500])
print('Finish:', d['choices'][0].get('finish_reason','?'))
" <<< "$BODY3" 2>&1
echo ""

echo "=============================================="
echo "Test 4: 流式工具调用（SSE）"
echo "=============================================="
RESP4=$(curl -s -N --max-time 60 \
  http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-test" \
  -d '{
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [
      {"role": "user", "content": "Use the calculate tool to compute 123*456."}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "calculate",
          "parameters": {
            "type": "object",
            "properties": {
              "expression": {"type": "string"}
            },
            "required": ["expression"]
          }
        }
      }
    ],
    "stream": true
  }')
echo "=== STREAM RESPONSE ==="
echo "$RESP4" | head -50
echo "..."
echo "$RESP4" | tail -20
echo ""
echo "=== DONE ==="
