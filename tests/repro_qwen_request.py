#!/usr/bin/env python3
"""从 Qwen Code 实际请求复现：68 tools + medium + 流式"""
import json, time, urllib.request

# 读取代理收到的实际请求体（从日志无法直接获取，构造 68 个工具）
tools = []
for i in range(68):
    tools.append({
        "type": "function",
        "function": {
            "name": f"tool_{i}",
            "description": f"Tool number {i} for testing purposes with a reasonably long description to simulate real tool schemas.",
            "parameters": {"type": "object", "properties": {"arg1": {"type": "string", "description": "First argument"}}, "required": ["arg1"]},
        },
    })

body = {
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [{"role": "user", "content": "Write a detailed 300-word essay about the history of the steam engine with multiple paragraphs. Do not use any tools."}],
    "reasoning_effort": "medium",
    "tools": tools,
    "stream": True,
}
data = json.dumps(body).encode()
print(f"body size: {len(data)/1024:.0f}KB, tools: {len(tools)}")

req = urllib.request.Request(
    "http://127.0.0.1:11435/v1/chat/completions",
    data=data,
    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
    method="POST",
)
t0 = time.time()
resp = urllib.request.urlopen(req, timeout=120)
buf = ""
content_chunks = 0
last_finish = "?"
for raw in resp:
    buf += raw.decode()
    while "\n" in buf:
        line, buf = buf.split("\n", 1)
        line = line.strip()
        if not line.startswith("data: "):
            continue
        js = line[6:]
        if js == "[DONE]":
            break
        try:
            evt = json.loads(js)
            delta = evt.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                content_chunks += 1
            fr = evt.get("choices", [{}])[0].get("finish_reason")
            if fr:
                last_finish = fr
        except Exception:
            pass
dt = time.time() - t0
print(f"time={dt:.1f}s finish={last_finish} content_chunks={content_chunks}")
