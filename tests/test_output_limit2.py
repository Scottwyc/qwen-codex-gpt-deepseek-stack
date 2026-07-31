#!/usr/bin/env python3
"""对比两个路径的输出 token 限制（流式逐行读取）"""
import json, time, urllib.request

BASE = "http://127.0.0.1:11435"

def send_stream(path, body, timeout=120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
        method="POST",
    )
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=timeout)
    lines = []
    for raw in resp:
        lines.append(raw.decode())
    dt = time.time() - t0
    return resp.status, lines, dt

# 1. Responses 路径（Codex 用）
print("=" * 60)
print("Test A: /responses (Codex path) - long text request")
print("=" * 60)
resp_body = {
    "model": "gpt-5.6-sol",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Write a detailed 500-word essay about the history of the steam engine. Include multiple paragraphs."}]}],
    "stream": True,
    "store": False,
}
status, lines, dt = send_stream("/responses", resp_body)
usage_info = ""
text_chars = 0
for line in lines:
    line = line.strip()
    if line.startswith("data: "):
        try:
            evt = json.loads(line[6:])
            if evt.get("type") == "response.completed":
                usage = evt.get("response", {}).get("usage") or {}
                out = usage.get("output_tokens", "?")
                det = usage.get("output_tokens_details") or {}
                usage_info = f"output_tokens={out} reasoning={det.get('reasoning_tokens', 0)}"
            if evt.get("type") == "response.output_text.delta":
                text_chars += len(evt.get("delta", ""))
        except Exception:
            pass
print(f"status={status} time={dt:.1f}s {usage_info}")
print(f"total text chars: {text_chars}")
print()

# 2. Chat Completions 路径（Qwen Code 用）非流式
print("=" * 60)
print("Test B: /v1/chat/completions (Qwen Code path) - long text request")
print("=" * 60)
chat_body = {
    "model": "gpt-5.6-sol-chatgpt",
    "messages": [{"role": "user", "content": "Write a detailed 500-word essay about the history of the steam engine. Include multiple paragraphs."}],
    "stream": False,
}
data = json.dumps(chat_body).encode()
req = urllib.request.Request(
    BASE + "/v1/chat/completions",
    data=data,
    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
    method="POST",
)
t0 = time.time()
try:
    resp = urllib.request.urlopen(req, timeout=120)
    body = resp.read().decode()
    dt = time.time() - t0
    d = json.loads(body)
    content = d["choices"][0]["message"]["content"] or ""
    finish = d["choices"][0]["finish_reason"]
    print(f"status={resp.status} time={dt:.1f}s finish={finish} text_chars={len(content)}")
    print(f"content head: {content[:120]}")
    print(f"content tail: {content[-120:]}")
except Exception as e:
    dt = time.time() - t0
    print(f"err: {e} time={dt:.1f}s")
