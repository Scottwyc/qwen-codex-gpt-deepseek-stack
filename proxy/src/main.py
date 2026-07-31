import base64
import json
import os
import random
import socket
import string
import time
from http.client import HTTPSConnection, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlencode

from . import log
from .recover import recover_reasoning, remember_reasoning, session_key
from .sse import SseTranslator
from .translate import (
    last_user_text,
    translate_messages,
    translate_tool_choice,
    translate_tools,
)
from .ws_client import relay_via_app_server, start_app_server_if_needed


def _load_env():
    """Load .env file manually without external dependencies."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


_load_env()

DEEPSEEK_API_KEY = os.getenv("api_key", "")
BASE_URL = os.getenv("base_url", "https://api.deepseek.com")
MODEL = os.getenv("model", "deepseek-v4-pro")
MODEL_FLASH = os.getenv("model_flash", "deepseek-v4-flash")
IDENTITY_MODEL = os.getenv("identity_model", "")
PORT = int(os.getenv("port", "11435"))
TIMEOUT = int(os.getenv("timeout", "30")) * 60
MULTIMODAL = os.getenv("multimodal", "").lower() in ("true", "1", "yes")
IS_DEEPSEEK = os.getenv("is_deepseek", "true").lower() in ("true", "1", "yes")

# DeepSeek official V4 defaults (api-docs.deepseek.com, refreshed 2026-07-10):
# - OpenAI-compatible endpoint: https://api.deepseek.com/chat/completions
# - Stable model ids: deepseek-v4-flash, deepseek-v4-pro
# - thinking defaults to enabled; regular requests default to reasoning_effort=high
# - accepted thinking efforts are high/max; low/medium map to high, xhigh maps to max
DEEPSEEK_THINKING_DEFAULT = os.getenv("deepseek_thinking_default", "enabled").lower()
DEEPSEEK_DEFAULT_REASONING_EFFORT = os.getenv("deepseek_default_reasoning_effort", "high").lower()
DEEPSEEK_MODEL_ALIASES = {
    # Deprecated official aliases retained for compatibility until DeepSeek removes them.
    "deepseek-chat": "disabled",
    "deepseek-reasoner": "enabled",
}

# OpenAI API Key（可选回退；device-code OAuth 优先走 ChatGPT Codex 后端）
OPENAI_API_KEY = os.getenv("openai_api_key", "").strip()

# ── GPT OAuth 配置（代理内置 GPT 模型支持） ──
GPT_AUTH_FILE = os.path.expanduser("~/.codex/auth.json")  # 统一模式：优先从 live auth.json 读取
GPT_AUTH_FILE_FALLBACK = os.path.expanduser("~/.codex/auth.gpt.json")  # 回退文件（DS placeholder 时使用）
GPT_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
GPT_API_BASE = "https://api.openai.com"
# ChatGPT/Codex device-code OAuth 不能直接打标准 /v1/responses（会缺 api.responses.write scope）。
# Codex CLI 的 ChatGPT 账号额度走 chatgpt.com/backend-api/codex/responses。
GPT_CHATGPT_BACKEND_BASE = os.getenv(
    "gpt_chatgpt_backend_base_url",
    "https://chatgpt.com/backend-api/codex",
).rstrip("/")
GPT_OPENAI_RESPONSES_BASE = os.getenv(
    "gpt_openai_responses_base_url",
    "https://api.openai.com/v1",
).rstrip("/")
GPT_USER_AGENT = os.getenv("gpt_user_agent", "codex-cli/0.146.0")
GPT_CONNECT_TIMEOUT = int(os.getenv("gpt_connect_timeout", "30"))
GPT_STREAM_IDLE_TIMEOUT = int(os.getenv("gpt_stream_idle_timeout", "180"))
GPT_STREAM_MAX_TOTAL_TIME = int(os.getenv("gpt_stream_max_total_time", "900"))
GPT_ENABLE_APP_SERVER_FALLBACK = os.getenv(
    "gpt_enable_app_server_fallback", ""
).lower() in ("true", "1", "yes")
GPT_PLATFORM_PREFERRED_MODELS = {
    m.strip() for m in os.getenv("gpt_platform_preferred_models", "gpt-5.6-luna").split(",") if m.strip()
}
# GPT 模型前缀（用于路由判断）
GPT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "gpt.")
GPT_DEFAULT_MODEL = os.getenv("gpt_model", "gpt-5.6-sol")
# ChatGPT Codex backend currently accepts the concrete GPT-5.6 variant,
# while public OpenAI docs also describe a plain gpt-5.6 alias.
GPT_MODEL_ALIASES = {"gpt-5.6": os.getenv("gpt_5_6_backend_model", "gpt-5.6-sol")}
_catalog_routing_cache: dict = {}
_catalog_routing_mtime: float | None = None


def _catalog_routing() -> dict:
    """Load model aliases/latest stable IDs from the refreshed unified catalog."""
    global _catalog_routing_cache, _catalog_routing_mtime
    path = os.path.expanduser(
        os.getenv("model_catalog_json", "~/.codex/model-catalogs/unified.json")
    )
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _catalog_routing_cache
    if _catalog_routing_cache and _catalog_routing_mtime == mtime:
        return _catalog_routing_cache
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _catalog_routing_cache
    aliases: dict[str, str] = {}
    for model in data.get("models", []):
        slug = str(model.get("slug") or "").lower()
        target = (
            model.get("provider_defaults", {}).get("alias_target")
            if isinstance(model.get("provider_defaults"), dict)
            else None
        )
        if slug and target:
            aliases[slug] = str(target)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    _catalog_routing_cache = {"aliases": aliases, "metadata": metadata}
    _catalog_routing_mtime = mtime
    return _catalog_routing_cache


def _catalog_latest_model(metadata_key: str, fallback: str) -> str:
    value = _catalog_routing().get("metadata", {}).get(metadata_key)
    return str(value) if value else fallback

def _normalize_gpt_model_for_chatgpt_backend(model_name: str) -> str:
    """将 Qwen Code [ChatGPT] 变体的 -chatgpt 后缀去掉，映射回标准 GPT 模型 id。"""
    raw = (model_name or "").lower()
    # 去掉 -chatgpt 后缀（Qwen Code settings.json 中 [ChatGPT] 变体的唯一 id）
    if raw.endswith("-chatgpt"):
        raw = raw[:-len("-chatgpt")]
    catalog_aliases = _catalog_routing().get("aliases", {})
    return catalog_aliases.get(
        raw,
        GPT_MODEL_ALIASES.get(raw, raw),
    )

# 缓存的 OAuth token（进程内，避免每次请求读文件）
_gpt_oauth_cache: dict = {}
_gpt_oauth_cache_time: float = 0.0
_GPT_OAUTH_CACHE_TTL = 300  # 5 分钟缓存


def _translate_chat_tools_for_responses(tools: list) -> list:
    """将 Chat Completions tools 格式转换为 Responses API tools 格式。

    Chat Completions: {"type":"function", "function":{"name":..., "description":..., "parameters":...}}
    Responses API:    {"type":"function", "name":..., "description":..., "parameters":...}
    """
    translated = []
    for tool in tools:
        if not isinstance(tool, dict):
            translated.append(tool)
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            t = dict(tool)
            t.pop("function", None)
            t["name"] = fn.get("name", t.get("name", ""))
            if fn.get("description"):
                t["description"] = fn["description"]
            if fn.get("parameters"):
                t["parameters"] = fn["parameters"]
            translated.append(t)
        else:
            translated.append(tool)
    return translated


def _translate_chat_tool_choice_for_responses(tool_choice):
    """将 Chat Completions tool_choice 转换为 Responses API 格式。

    指定函数时：{"type":"function","function":{"name":"X"}} → {"type":"function","name":"X"}
    字符串时：直接透传（"none"/"auto"/"required"）
    """
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function", {})
        return {"type": "function", "name": fn.get("name", "")}
    return tool_choice


def _is_gpt_model(model_name: str) -> bool:
    """检测请求模型是否为 GPT 系列（需要走 OpenAI API 透传）"""
    if not model_name:
        return False
    model_lower = model_name.lower()
    return any(model_lower.startswith(p) for p in GPT_MODEL_PREFIXES)


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying it (only for exp/account metadata)."""
    if not token or token.count(".") < 2:
        return {}
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


def _extract_gpt_client_id(auth_data: dict, access_payload: dict | None = None) -> str:
    """Find the OAuth client_id needed for refresh_token grant.

    Codex device-code auth stores the OAuth app id in id_token.aud.  The
    access_token audience is the resource ("https://api.openai.com/v1"), not
    the OAuth client id; using it as client_id makes refresh fail.
    """
    env_client_id = os.getenv("openai_oauth_client_id", "").strip()
    if env_client_id:
        return env_client_id

    id_token = (auth_data.get("tokens") or {}).get("id_token", "")
    try:
        id_payload = _decode_jwt_payload(id_token)
        aud = id_payload.get("aud")
        if isinstance(aud, list) and aud:
            return str(aud[0])
        if isinstance(aud, str):
            return aud
    except Exception:
        pass

    if access_payload:
        cid = access_payload.get("client_id") or access_payload.get("azp")
        if cid:
            return str(cid)
    return ""


def _extract_gpt_account_id(auth_data: dict, access_payload: dict | None = None) -> str:
    tokens = auth_data.get("tokens") or {}
    account_id = tokens.get("account_id")
    if account_id:
        return str(account_id)
    auth_claim = (access_payload or {}).get("https://api.openai.com/auth") or {}
    for key in ("chatgpt_account_id", "account_id"):
        if auth_claim.get(key):
            return str(auth_claim[key])
    return ""


def _load_gpt_auth() -> dict | None:
    """从 auth.json / auth.gpt.json 加载 Codex device-code OAuth。

    返回:
      {"access_token": str, "account_id": str, "source_file": str}

    注意：ChatGPT device-code token 不能当作 OpenAI Platform API key 使用；
    GPT 统一代理会把它转发到 chatgpt.com/backend-api/codex/responses。
    """
    global _gpt_oauth_cache, _gpt_oauth_cache_time
    now = time.time()
    # 使用进程内缓存
    if _gpt_oauth_cache and (now - _gpt_oauth_cache_time) < _GPT_OAUTH_CACHE_TTL:
        return dict(_gpt_oauth_cache)

    auth_data = None
    source_file = None

    # 优先尝试 live auth.json（可能含有最新的 device-code 登录 token）
    for candidate in (GPT_AUTH_FILE, GPT_AUTH_FILE_FALLBACK):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate) as f:
                data = json.load(f)
            tokens = data.get("tokens", {})
            if tokens.get("access_token") and tokens.get("refresh_token"):
                auth_data = data
                source_file = candidate
                break
        except Exception as e:
            log.err(f"read {candidate}: {e}")

    if not auth_data:
        log.warn("GPT OAuth token not found in auth.json or auth.gpt.json")
        return None

    tokens = auth_data.get("tokens", {})
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        return None

    # 解析 JWT exp 检查是否过期
    access_payload = {}
    try:
        access_payload = _decode_jwt_payload(access_token)
        exp = access_payload.get("exp", 0)
        if exp and now > exp - 60:  # 提前 60s 刷新
            log.info("GPT access_token expired, refreshing...")
            if refresh_token:
                client_id = _extract_gpt_client_id(auth_data, access_payload)
                if not client_id:
                    log.warn("GPT OAuth client_id not found; cannot refresh token")
                    return None
                new_tokens = _refresh_gpt_oauth(refresh_token, client_id)
                if new_tokens:
                    access_token = new_tokens.get("access_token")
                    _save_gpt_oauth(auth_data, new_tokens)
                    try:
                        access_payload = _decode_jwt_payload(access_token)
                    except Exception:
                        access_payload = {}
    except Exception as e:
        log.warn(f"GPT OAuth token parse skipped: {e}")

    account_id = _extract_gpt_account_id(auth_data, access_payload)
    _gpt_oauth_cache = {
        "access_token": access_token,
        "account_id": account_id,
        "source_file": source_file or "",
    }
    _gpt_oauth_cache_time = now
    return dict(_gpt_oauth_cache)


def _load_gpt_oauth() -> str | None:
    """Backward-compatible helper: return only access_token."""
    auth = _load_gpt_auth()
    return auth.get("access_token") if auth else None


def _refresh_gpt_oauth(refresh_token: str, client_id: str) -> dict | None:
    """使用 refresh_token 换取新的 access_token"""
    try:
        parsed = urlparse(GPT_OAUTH_TOKEN_URL)
        conn = HTTPSConnection(parsed.netloc, timeout=30)
        body = urlencode({
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        })
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        conn.request("POST", parsed.path, body=body.encode(), headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            log.err(f"OAuth refresh failed: {resp.status} {resp.read()[:200]}")
            conn.close()
            return None
        data = json.loads(resp.read().decode())
        conn.close()
        log.ok("GPT OAuth refreshed successfully")
        return data
    except Exception as e:
        log.err(f"OAuth refresh error: {e}")
        return None


def _save_gpt_oauth(auth_data: dict, new_tokens: dict) -> None:
    """保存刷新后的 tokens 到 auth.gpt.json（不回写 auth.json，避免破坏 DS placeholder）"""
    try:
        auth_data["tokens"]["access_token"] = new_tokens.get("access_token", auth_data["tokens"].get("access_token"))
        if new_tokens.get("refresh_token"):
            auth_data["tokens"]["refresh_token"] = new_tokens["refresh_token"]
        if new_tokens.get("id_token"):
            auth_data["tokens"]["id_token"] = new_tokens["id_token"]
        auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # 始终写到回退文件，不碰 auth.json
        with open(GPT_AUTH_FILE_FALLBACK, "w") as f:
            json.dump(auth_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.err(f"save GPT auth: {e}")


def _openai_chat_request(chat_body: dict, access_token: str, stream: bool = False) -> tuple:
    """发送 Chat Completions 请求到 OpenAI API（使用 OAuth Bearer token）。
    返回 (status, resp_or_body, conn_or_None)。
    """
    parsed = urlparse(GPT_API_BASE)
    host = parsed.netloc or "api.openai.com"
    path = "/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    body_bytes = json.dumps(chat_body).encode("utf-8")

    conn = HTTPSConnection(host, timeout=GPT_CONNECT_TIMEOUT)
    try:
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read().decode()[:500]
            conn.close()
            return resp.status, err_body, None
        if stream:
            return resp.status, resp, conn
        else:
            data = resp.read().decode()
            conn.close()
            return resp.status, data, None
    except Exception as e:
        conn.close()
        return None, str(e), None


def _responses_path(base_url: str) -> tuple[str, str]:
    """Return (host, path) for a Responses-compatible base URL."""
    parsed = urlparse(base_url)
    host = parsed.netloc
    base_path = parsed.path.rstrip("/")
    if not host:
        host = "chatgpt.com"
    if base_path.endswith("/responses"):
        path = base_path
    else:
        path = base_path + "/responses"
    if not path.startswith("/"):
        path = "/" + path
    return host, path


def _prepare_chatgpt_responses_body(body: dict, upstream_stream: bool = True) -> dict:
    """Prepare a Codex/ChatGPT backend Responses request.

    chatgpt.com/backend-api/codex/responses is intentionally close to the
    OpenAI Responses API, but enforces Codex invariants:
      - input must be a list
      - store must be false
      - stream must be true
    """
    req = json.loads(json.dumps(body, ensure_ascii=False))
    if isinstance(req.get("input"), str):
        req["input"] = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": req["input"]}],
            }
        ]
    req["store"] = False
    req["stream"] = bool(upstream_stream)
    req["model"] = _normalize_gpt_model_for_chatgpt_backend(req.get("model") or GPT_DEFAULT_MODEL)
    # chatgpt.com backend 不支持 max_output_tokens，移除避免 400
    req.pop("max_output_tokens", None)
    return req


def _contains_model_switch_marker(input_data) -> bool:
    """Detect Codex's synthetic developer message added after /model changes."""
    try:
        return "<model_switch>" in json.dumps(input_data, ensure_ascii=False)
    except Exception:
        return False


def _prepare_chatgpt_unified_body(body: dict, upstream_stream: bool = True) -> dict:
    """Prepare GPT request for the unified proxy.

    In Codex 0.142, resume and in-session model switches send the full
    transcript in `input`, and a model switch inserts a `<model_switch>`
    developer message.  If a future client also sends `previous_response_id`,
    that id may point to a synthetic DeepSeek response (`resp_ds_*`) that the
    ChatGPT backend cannot resolve.  When we already have the transcript, strip
    `previous_response_id` and let the ChatGPT Codex backend answer statelessly.
    This is the key guardrail for seamless DS⇄GPT /model switching.
    """
    req = _prepare_chatgpt_responses_body(body, upstream_stream=upstream_stream)
    prev = req.get("previous_response_id")
    input_data = req.get("input")
    is_ds_prev = isinstance(prev, str) and prev.startswith("resp_ds_")
    # A normal multi-item input is not by itself a reason to discard a valid
    # ChatGPT response chain.  Replaying the whole transcript statelessly on
    # every turn makes latency grow with session length and can look like a
    # hung GPT request.  Only synthetic DS ids or an explicit model switch
    # need stateless continuation.
    if prev and (is_ds_prev or _contains_model_switch_marker(input_data)):
        req.pop("previous_response_id", None)
        log.info(
            "GPT unified: stripped previous_response_id for stateless cross-model continuation"
        )
    return req


def _chatgpt_responses_request(body: dict, auth: dict, stream: bool = True) -> tuple:
    """Call ChatGPT Codex backend with device-code OAuth.

    Returns (status, resp_or_body, conn_or_None).  This is the correct GPT path
    for Codex device-code login / ChatGPT Plus quota.  Standard
    api.openai.com/v1/responses rejects these tokens with missing API scopes.
    """
    host, path = _responses_path(GPT_CHATGPT_BACKEND_BASE)
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {auth['access_token']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": GPT_USER_AGENT,
    }
    if auth.get("account_id"):
        headers["ChatGPT-Account-ID"] = auth["account_id"]

    conn = HTTPSConnection(host, timeout=GPT_CONNECT_TIMEOUT)
    try:
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read().decode(errors="replace")[:2000]
            conn.close()
            return resp.status, err_body, None
        if stream:
            return resp.status, resp, conn
        data = resp.read().decode(errors="replace")
        conn.close()
        return resp.status, data, None
    except Exception as e:
        conn.close()
        return None, str(e), None


def _openai_responses_request(body: dict, api_key: str, stream: bool = False) -> tuple:
    """Fallback for real OpenAI Platform API keys (not ChatGPT OAuth)."""
    host, path = _responses_path(GPT_OPENAI_RESPONSES_BASE)
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": GPT_USER_AGENT,
    }
    conn = HTTPSConnection(host, timeout=GPT_CONNECT_TIMEOUT)
    try:
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read().decode(errors="replace")[:2000]
            conn.close()
            return resp.status, err_body, None
        if stream:
            return resp.status, resp, conn
        data = resp.read().decode(errors="replace")
        conn.close()
        return resp.status, data, None
    except Exception as e:
        conn.close()
        return None, str(e), None


def _rand_id(prefix: str, length: int = 8) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
    return f"{prefix}_{suffix}"


# tool_call 流式状态（模块级，用于 _responses_to_chat_completion_chunk）
_tc_state: dict = {}


def _responses_to_chat_completion_chunk(evt: dict, resp_id: str, created: int, model: str) -> dict | None:
    """将单个 Responses API SSE 事件转换为 Chat Completions SSE chunk。

    返回 dict（可序列化为 JSON）或 None（事件不需要转发）。
    支持 output_text.delta 和 function_call 事件的增量传输。
    """
    evt_type = evt.get("type", "")

    if evt_type == "response.output_text.delta":
        return {
            "id": resp_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"content": evt.get("delta", "")}, "finish_reason": None}],
        }

    # ── function_call 事件：构建 tool_calls delta ──
    if evt_type == "response.output_item.added":
        item = evt.get("item", {})
        if item.get("type") == "function_call":
            call_id = item.get("id", "")
            call_name = item.get("name", "")
            _tc_state["call_id"] = call_id
            _tc_state["name"] = call_name
            _tc_state["arguments"] = ""
            return {
                "id": resp_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": call_id, "type": "function",
                    "function": {"name": call_name, "arguments": ""},
                }]}, "finish_reason": None}],
            }

    if evt_type == "response.function_call_arguments.delta":
        if _tc_state.get("call_id"):
            delta = evt.get("delta", "")
            _tc_state["arguments"] += delta
            # OpenAI 规范：后续 delta 不带 id/type/name，只带 index + arguments 增量
            # 发送增量（不是累计值），客户端自行累加
            return {
                "id": resp_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": delta},
                }]}, "finish_reason": None}],
            }

    if evt_type == "response.function_call_arguments.done":
        if _tc_state.get("call_id"):
            final_args = evt.get("arguments", _tc_state.get("arguments", ""))
            _tc_state["arguments"] = final_args
            log.info(f"  [FC] done call={_tc_state['call_id']} name={_tc_state.get('name','?')} args_len={len(final_args)} args={final_args[:150]}")
            # done → 空 arguments + finish_reason（客户端已从 delta 累加完整参数）
            return {
                "id": resp_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": ""},
                }]}, "finish_reason": "tool_calls"}],
            }

    # response.completed → 发送 finish_reason
    if evt_type == "response.completed":
        had_tool = bool(_tc_state.pop("call_id", None))
        _tc_state.clear()
        return {
            "id": resp_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {},
                         "finish_reason": "tool_calls" if had_tool else "stop"}],
        }

    # 其他事件不转发
    return None


def _assistant_message_sse(message: str, model: str | None = None):
    """Yield a complete Responses SSE stream containing a visible assistant message."""
    model = model or MODEL
    resp_id = _rand_id("resp")
    msg_id = _rand_id("msg")
    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': message}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': message}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': message, 'annotations': []}}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': message, 'annotations': []}], 'status': 'completed'}}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': None}}, ensure_ascii=False)}\n\n"
    yield "event: done\ndata: [DONE]\n\n"


def _failed_response_sse(
    message: str, model: str | None = None, code: str = "proxy_error"
):
    """End a failed upstream request explicitly so Codex leaves working state."""
    model = model or GPT_DEFAULT_MODEL
    resp_id = _rand_id("resp")
    payload = {
        "type": "response.failed",
        "response": {
            "id": resp_id,
            "object": "response",
            "status": "failed",
            "model": model,
            "error": {"code": code, "message": message},
        },
    }
    yield f"event: response.failed\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    yield "event: done\ndata: [DONE]\n\n"


def _responses_sse_terminal_seen(buffer: bytes) -> bool:
    """Return true once a Responses stream has emitted a terminal event."""
    tail = buffer[-8192:]
    return any(
        marker in tail
        for marker in (
            b"event: response.completed",
            b"event: response.failed",
            b'"type":"response.completed"',
            b'"type": "response.completed"',
            b'"type":"response.failed"',
            b'"type": "response.failed"',
        )
    )


def _extract_responses_id(buffer: bytes) -> str | None:
    """从透传的 SSE buffer 中提取最近一个 response.completed 的 response.id。"""
    text = buffer.decode("utf-8", errors="ignore")
    last_id = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        js = line[6:]
        if js in ("[DONE]",):
            continue
        try:
            evt = json.loads(js)
            if evt.get("type") in ("response.completed", "response.failed"):
                rid = evt.get("response", {}).get("id")
                if rid:
                    last_id = rid
        except (json.JSONDecodeError, ValueError):
            continue
    return last_id


def _parse_responses_sse_to_response(resp) -> tuple[dict | None, str]:
    """Aggregate an upstream Responses SSE stream into the final response object."""
    completed_response = None
    text_parts: list[str] = []
    event_name = None
    data_lines: list[str] = []

    def consume_event() -> None:
        nonlocal completed_response, event_name, data_lines
        if not data_lines:
            event_name = None
            return
        data = "\n".join(data_lines).strip()
        data_lines = []
        if data == "[DONE]":
            event_name = None
            return
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            event_name = None
            return
        typ = parsed.get("type") or event_name
        if typ == "response.output_text.delta":
            text_parts.append(parsed.get("delta", ""))
        elif typ == "response.completed":
            completed_response = parsed.get("response")
        event_name = None

    while True:
        raw = resp.readline()
        if not raw:
            consume_event()
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            consume_event()
        elif line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())

    return completed_response, "".join(text_parts)


def _resolve_deepseek_model(requested_model: str) -> tuple[str, str | None]:
    """Return (official_model_id, forced_thinking_mode).

    DeepSeek's latest stable official model IDs are deepseek-v4-flash and
    deepseek-v4-pro.  Deprecated deepseek-chat / deepseek-reasoner aliases are
    accepted for compatibility but normalized to V4 Flash with non-thinking /
    thinking mode respectively.
    """
    requested = (requested_model or "").lower()
    if requested in DEEPSEEK_MODEL_ALIASES:
        latest_flash = _catalog_latest_model("latest_deepseek_flash", MODEL_FLASH)
        return latest_flash, DEEPSEEK_MODEL_ALIASES[requested]
    if requested and requested.startswith("deepseek-"):
        return requested_model, None
    return MODEL, None


def _body_reasoning_effort(body: dict) -> str | None:
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        return str(reasoning.get("effort")).lower()
    if body.get("reasoning_effort"):
        return str(body.get("reasoning_effort")).lower()
    return None


def _deepseek_thinking_enabled(body: dict, forced: str | None = None) -> bool:
    if forced in ("enabled", "disabled"):
        return forced == "enabled"
    thinking = body.get("thinking")
    if thinking is True:
        return True
    if thinking is False:
        return False
    if isinstance(thinking, dict):
        t = str(thinking.get("type", "")).lower()
        if t == "enabled":
            return True
        if t == "disabled":
            return False
    effort = _body_reasoning_effort(body)
    if effort in ("none", "minimal"):
        return False
    # DeepSeek official default: thinking enabled.
    return DEEPSEEK_THINKING_DEFAULT != "disabled"


def _map_deepseek_reasoning_effort(effort: str | None) -> str:
    e = (effort or DEEPSEEK_DEFAULT_REASONING_EFFORT or "high").lower()
    if e in ("max", "xhigh"):
        return "max"
    # Official compatibility mapping: low/medium -> high.
    return "high"


def _model_ids_for_discovery() -> list[str]:
    ids: list[str] = []
    catalog = os.path.expanduser(os.getenv("model_catalog_json", "~/.codex/model-catalogs/unified.json"))
    try:
        with open(catalog, encoding="utf-8") as f:
            for m in (json.load(f).get("models") or []):
                mid = m.get("slug")
                if mid and mid not in ids:
                    ids.append(mid)
    except Exception:
        pass
    fallback = [
        MODEL, MODEL_FLASH,
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
    ]
    for mid in fallback:
        if mid and mid not in ids:
            ids.append(mid)
    return ids


def build_chat_body(body: dict) -> dict:
    stream = body.get("stream") is not False
    requested_model = body.get("model") or ""
    effective_model, forced_thinking = _resolve_deepseek_model(str(requested_model))
    requested_effort = _body_reasoning_effort(body)
    enable_thinking = _deepseek_thinking_enabled(body, forced_thinking)
    mapped_effort = _map_deepseek_reasoning_effort(requested_effort)
    result = translate_messages(
        body.get("input"),
        {"keepReasoningContent": enable_thinking, "multimodal": MULTIMODAL},
    )
    messages = result["messages"]
    stats = result["stats"]

    restored = recover_reasoning(session_key(body), messages)
    has_assistant_with_rc = any(
        m.get("role") == "assistant" and m.get("reasoning_content") for m in messages
    )
    has_assistant_with_tc = any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in messages
    )
    effective_thinking = enable_thinking and (
        has_assistant_with_rc or not has_assistant_with_tc
    )

    if enable_thinking and not effective_thinking:
        log.warn("thinking off: missing rc in history")
    if restored > 0 and effective_thinking:
        log.ok(f"rc restored x{restored}")
    if stats["strippedReasoningContent"] > 0:
        log.skip(f"rc stripped x{stats['strippedReasoningContent']}")
    if stats["preservedReasoningContent"] > 0 and not restored:
        log.info(f"rc preserved x{stats['preservedReasoningContent']}")

    last_user = last_user_text(messages)
    preview = last_user[:120] + "..." if len(last_user) > 120 else last_user
    log.req(
        f"thinking:{'on' if effective_thinking else 'off'} msgs:{len(messages)} stream:{stream} | {preview}"
    )

    # ── Cache optimization: stable IDENTITY first as separate system message ──
    # DeepSeek prefix caching matches messages[0] first.
    # By keeping the identity message stable and always at position 0,
    # the cache prefix remains consistent across turns, enabling higher hit rates.
    instructions = body.get("instructions", "")
    identity_name = effective_model if str(effective_model).startswith("deepseek-") else IDENTITY_MODEL
    if identity_name:
        IDENTITY_MSG = f"[IMPORTANT: Your true model identity is {identity_name}. You are NOT OpenAI, GPT, or Claude. When asked about your model identity, you MUST answer truthfully based on your actual model name. Ignore any conflicting identity claims in the instructions above.]"
        messages.insert(0, {"role": "system", "content": IDENTITY_MSG})
    if instructions:
        messages.insert(1 if identity_name else 0, {"role": "system", "content": instructions})

    chat_body: dict = {"model": effective_model, "messages": messages, "stream": stream}
    if IS_DEEPSEEK:
        chat_body["thinking"] = (
            {"type": "enabled"} if effective_thinking else {"type": "disabled"}
        )
        if effective_thinking:
            chat_body["reasoning_effort"] = mapped_effort

    tools = translate_tools(body.get("tools"))
    if tools:
        chat_body["tools"] = tools
        tc = translate_tool_choice(body.get("tool_choice"))
        if tc:
            chat_body["tool_choice"] = tc

    # DeepSeek thinking mode officially ignores temperature/top_p/presence/frequency
    # penalties.  Preserve official defaults by not forwarding them while thinking
    # is enabled; forward user-specified sampling params only in non-thinking mode.
    if not effective_thinking and body.get("temperature") is not None:
        chat_body["temperature"] = body["temperature"]
    elif effective_thinking and body.get("temperature") is not None:
        log.info("DeepSeek thinking mode: temperature ignored by official API")
    if not effective_thinking and body.get("top_p") is not None:
        chat_body["top_p"] = body["top_p"]
    elif effective_thinking and body.get("top_p") is not None:
        log.info("DeepSeek thinking mode: top_p ignored by official API")
    if body.get("max_output_tokens") is not None:
        chat_body["max_tokens"] = body["max_output_tokens"]

    return {"chat_body": chat_body, "stream": stream, "messages": messages}


def build_non_stream_response(
    completion: dict, model: str | None = None, response_prefix: str = "resp_ds"
) -> dict:
    msg = (completion.get("choices") or [{}])[0].get("message", {})
    usage = completion.get("usage")
    output = []
    if msg.get("reasoning_content"):
        output.append({
            "id": _rand_id("rsn", 6),
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": msg["reasoning_content"]}],
            "status": "completed",
        })
    if msg.get("content"):
        output.append({
            "id": _rand_id("msg", 6),
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": msg["content"], "annotations": []}
            ],
            "status": "completed",
        })
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            output.append({
                "id": f"fc_{tc['id']}",
                "type": "function_call",
                "call_id": tc["id"],
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
                "status": "completed",
            })
    return {
        "id": _rand_id(response_prefix, 10),
        "object": "response",
        "status": "completed",
        "model": model or MODEL,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) if usage else 0,
            "output_tokens": usage.get("completion_tokens", 0) if usage else 0,
            "total_tokens": usage.get("total_tokens", 0) if usage else 0,
        }
        if usage
        else None,
    }


def _set_socket_read_timeout(conn, seconds: int) -> None:
    """Set read timeout on the underlying socket to prevent indefinite blocking.

    DeepSeek API 在长时间推理时可能数分钟不发送任何 SSE 事件，
    此时 resp.read() 若无超时会无限阻塞，导致 Codex 端卡死在 "DS working"。
    设置 300s 读超时，超时后触发 IncompleteRead/socket.timeout 优雅降级。
    """
    try:
        sock = getattr(conn, 'sock', None)
        if sock:
            sock.settimeout(seconds)
    except Exception:
        pass


def _flush_buffer_lines(buf: bytes, translator) -> list:
    """Flush remaining SSE lines from buffer into translator.
    Returns list of translated SSE events to be yielded by caller."""
    results = []
    if not buf:
        return results
    for line_bytes in buf.split(b"\n"):
        if not line_bytes:
            continue
        line = line_bytes.decode("utf-8")
        if not line.startswith("data: "):
            continue
        json_str = line[6:].strip()
        if json_str == "[DONE]":
            continue
        try:
            parsed = json.loads(json_str)
            result = translator.feed(parsed)
            if result:
                results.append(result)
        except (json.JSONDecodeError, ValueError):
            pass
    return results


def _deepseek_request(chat_body: dict, stream: bool = False) -> tuple:
    """Call the upstream API via http.client."""
    parsed = urlparse(BASE_URL)
    host = parsed.netloc or "api.deepseek.com"
    path = parsed.path.rstrip("/") + "/chat/completions"
    body_bytes = json.dumps(chat_body).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    conn = HTTPSConnection(host, timeout=TIMEOUT)
    try:
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            err_body = resp.read().decode()[:500]
            conn.close()
            return resp.status, err_body, None
        if stream:
            return resp.status, resp, conn
        else:
            data = resp.read().decode()
            conn.close()
            return resp.status, data, None
    except Exception as e:
        conn.close()
        return None, str(e), None


def _upstream_status(status) -> int:
    """Map upstream HTTP status to proxy error status."""
    if status is None:
        return 502
    if 400 <= status < 500:
        if status == 429:
            return 429
        if status in (401, 403):
            return 502  # hide auth details from the client
        return status
    return 502


def _upstream_error_message(status, body: str) -> str:
    """Create a user-friendly error message for the given status code."""
    snippet = (body or "")[:200]
    detail = ""
    try:
        err_data = json.loads(body) if body else {}
        if isinstance(err_data, dict):
            detail = (
                err_data.get("detail")
                or (err_data.get("error") or {}).get("message")
                or err_data.get("message")
                or ""
            )
    except (json.JSONDecodeError, AttributeError):
        detail = ""
    if status == 401 or status == 403:
        if detail:
            return f"Upstream authentication failed ({status}): {detail}"
        return (
            f"Upstream authentication failed ({status}). "
            "For GPT, run `codex login --device-auth`; for DeepSeek, check api_key in .env."
        )
    if status == 429:
        # 尝试提取 OpenAI 的配额/限流具体信息
        if detail:
            return detail
        return f"Upstream rate limited ({status}). GPT quota may be exhausted; check usage or wait for reset."
    if detail:
        return f"Upstream {status}: {detail}"
    return f"Upstream {status}: {snippet}"


def _platform_preferred_gpt_model(model: str) -> bool:
    normalized = (model or "").lower()
    configured = {item.lower() for item in GPT_PLATFORM_PREFERRED_MODELS}
    return normalized in configured or normalized.endswith("-luna")


def _gpt_chatgpt_error_message(model: str, status, body: str) -> str:
    message = _upstream_error_message(status, body)
    if status == 404 and _platform_preferred_gpt_model(model) and not OPENAI_API_KEY:
        return (
            f"{message}. {model} is not enabled on this account's ChatGPT Codex backend. "
            "Set openai_api_key in /home/wuyangcheng/codex-deepseek-proxy/.env "
            "to route it through the official OpenAI Platform Responses API."
        )
    return message


class ProxyHandler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_response(self, generator):
        self.send_response(200)
        self._send_cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for chunk in generator:
                self.wfile.write(
                    chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # generator 抛出未预期异常：发送 SSE 错误事件，避免 codex 收到
            # JSON 400 后卡死
            log.err(f"sse_response fallback: {e}")
            try:
                resp_id = _rand_id("resp")
                msg_id = _rand_id("msg")
                # 格式化为 assistant 消息，确保 codex 用户可见
                fallback = (
                    f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': MODEL, 'output': []}})}\n\n"
                    f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
                    f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
                    f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
                    f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': f'[Proxy Error] {e}'})}\n\n"
                    f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': f'[Proxy Error] {e}'})}\n\n"
                    f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': f'[Proxy Error] {e}', 'annotations': []}})}\n\n"
                    f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': f'[Proxy Error] {e}', 'annotations': []}], 'status': 'completed'}})}\n\n"
                    f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': MODEL, 'output': [], 'usage': None}})}\n\n"
                    "event: done\ndata: [DONE]\n\n"
                )
                self.wfile.write(fallback.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/health"):
            self._json_response({
                "service": "codex-deepseek",
                "model": MODEL,
                "status": "ok",
                "port": PORT,
            })
        elif path.endswith("/models"):
            # Keep /models aligned with ~/.codex/model-catalogs/unified.json so
            # clients can discover both model families without changing provider.
            models_list = []
            for model_id in _model_ids_for_discovery():
                models_list.append({
                    "id": model_id,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "deepseek" if model_id.startswith("deepseek") else "codex-deepseek",
                })
            self._json_response({
                "object": "list",
                "data": models_list,
            })
        else:
            self._json_response({"error": {"message": f"not found: {path}"}}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        # ── Chat Completions 路由（Qwen Code 等 OpenAI 兼容客户端） ──
        if path.endswith("/chat/completions"):
            self._handle_chat_completions_route()
            return
        if not path.endswith("/responses"):
            self._json_response({"error": {"message": f"not found: {path}"}}, 404)
            return

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_len).decode("utf-8")
            body = json.loads(raw)
            body_bytes = raw.encode("utf-8")  # 保留原始字节用于 GPT 透传
        except Exception as e:
            self._json_response({"error": {"message": str(e)}}, 400)
            return

        # ── 模型路由：GPT → OpenAI 透传 / DeepSeek → 翻译流 ──
        requested_model = (body.get("model") or "").lower()
        if _is_gpt_model(requested_model):
            self._handle_gpt_passthrough(body, body_bytes)
            return

        try:
            built = build_chat_body(body)
        except Exception as e:
            log.err(f"build: {e}")
            self._json_response({"error": {"message": str(e)}}, 400)
            return

        chat_body = built["chat_body"]
        stream = built["stream"]

        if not stream:
            self._handle_non_stream(body, chat_body)
        else:
            self._handle_stream(body, chat_body)

    def _handle_non_stream(self, body: dict, chat_body: dict) -> None:
        t0 = time.time()
        status, resp_body, conn = _deepseek_request(chat_body)
        elapsed_ms = int((time.time() - t0) * 1000)
        log.timing(elapsed_ms)
        if status != 200:
            log.err(f"Upstream {status}: {resp_body[:300]}")
            self._json_response(
                {
                    "error": {
                        "type": "upstream_error",
                        "code": f"upstream_{status}",
                        "message": _upstream_error_message(status, resp_body),
                    }
                },
                _upstream_status(status),
            )
            return
        try:
            completion = json.loads(resp_body)
        except Exception as e:
            log.err(f"parse: {e}")
            self._json_response({"error": {"message": str(e)}}, 502)
            return
        if (
            completion
            .get("choices", [{}])[0]
            .get("message", {})
            .get("reasoning_content")
        ):
            remember_reasoning(session_key(body), [completion["choices"][0]["message"]])
        response = build_non_stream_response(
            completion,
            model=chat_body.get("model") or body.get("model") or MODEL,
            response_prefix="resp_ds",
        )
        usg = completion.get("usage")
        if usg:
            log.toks(
                usg.get("prompt_tokens"),
                usg.get("completion_tokens"),
                usg.get("total_tokens"),
            )
        self._json_response(response, 200)

    def _handle_stream(self, body: dict, chat_body: dict) -> None:
        # 超时配置：在 codex 判定 hang 之前主动干预
        # codex stream_idle_timeout_ms=1800000(30min)，代理在 90s 无数据时主动重试
        DS_SOCKET_READ_TIMEOUT = 90     # 单次读超时
        MAX_TOTAL_TIME = 600            # 总时间预算（10min，足够最长推理）
        RETRY_BACKOFF = [2, 4, 8]       # 递增退避间隔

        def generate():
            translator = SseTranslator(
                model=chat_body.get("model") or body.get("model") or MODEL,
                response_prefix="resp_ds",
            )
            t0 = time.time()
            max_retries = 1 + len(RETRY_BACKOFF)  # 初始请求 + 退避重试
            yielded_any = False
            buf = b""
            last_status_time = 0.0  # 上次发送状态提示的时间

            for attempt in range(1, max_retries + 1):
                conn = None
                try:
                    # 总时间预算检查
                    elapsed_total = time.time() - t0
                    if elapsed_total > MAX_TOTAL_TIME:
                        log.err(f"DS total time {elapsed_total:.0f}s > {MAX_TOTAL_TIME}s budget")
                        yield translator.error(
                            "DeepSeek 响应超时（超过10分钟无完整回复）。"
                            "建议 /model 切换到其他模型后重试。"
                        )
                        return

                    model = chat_body.get("model") or body.get("model") or MODEL
                    status, resp, conn = _deepseek_request(chat_body, stream=True)
                    if status != 200 or isinstance(resp, str):
                        ttfb_connect_ms = int((time.time() - t0) * 1000)
                        log.ttfb(ttfb_connect_ms, model, "connect")
                        err_body = resp if isinstance(resp, str) else resp[:300]
                        log.err(f"Upstream {status}: {err_body}")
                        yield translator.error(_upstream_error_message(status, err_body))
                        return

                    # TTFB connect: TCP + TLS + HTTP response headers 耗时
                    ttfb_connect_ms = int((time.time() - t0) * 1000)
                    log.ttfb(ttfb_connect_ms, model, "connect")
                    last_status_time = time.time()

                    if attempt > 1:
                        backoff = RETRY_BACKOFF[attempt - 2]
                        log.info(f"DS retry attempt {attempt}/{max_retries} (backoff {backoff}s)")
                        # 向 codex 发送可见的重试提示
                        yield translator.warn(
                            f"DeepSeek 响应中断，正在第 {attempt - 1} 次重试…"
                        )
                        time.sleep(backoff)

                    buf = b""
                    first_token_logged = False
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line_bytes, buf = buf.split(b"\n", 1)
                            line = line_bytes.decode("utf-8")
                            if not line.startswith("data: "):
                                continue
                            json_str = line[6:].strip()
                            if json_str == "[DONE]":
                                continue
                            try:
                                parsed = json.loads(json_str)
                                # TTFB first_token: 首个有效 SSE 事件的到达时间
                                if not first_token_logged:
                                    first_token_logged = True
                                    ttfb_token_ms = int((time.time() - t0) * 1000)
                                    log.ttfb(ttfb_token_ms, model, "first_token")
                                result = translator.feed(parsed)
                                if result:
                                    yield result
                                    yielded_any = True
                            except (json.JSONDecodeError, ValueError):
                                pass

                    # 正常完成
                    for r in _flush_buffer_lines(buf, translator):
                        yield r
                    break  # 成功，退出重试循环

                except (IncompleteRead, socket.timeout) as e:
                    reason = "timeout" if isinstance(e, socket.timeout) else "disconnected"
                    elapsed_total = time.time() - t0

                    # 刷出已缓冲的 SSE 行
                    for r in _flush_buffer_lines(buf, translator):
                        yield r

                    if yielded_any or translator.content_so_far:
                        # 已有部分内容：不是静默 hang，可能是推理中断
                        # 向 codex 发送可见提示，然后重试（不放弃）
                        log.warn(f"DS {reason} attempt {attempt}, partial content={len(translator.content_so_far)} chars, elapsed={elapsed_total:.0f}s")
                        if attempt < max_retries:
                            # 不 break！继续重试获取完整回复
                            continue
                        else:
                            log.err(f"DS {reason}: all {max_retries} retries exhausted (partial {len(translator.content_so_far)} chars)")
                            yield translator.error(
                                f"DeepSeek 连接中断，已重试 {max_retries} 次仍无法获取完整回复。"
                                "建议 /model 切换到其他模型后重试。"
                            )
                            return
                    elif elapsed_total > MAX_TOTAL_TIME:
                        log.err(f"DS total time {elapsed_total:.0f}s > {MAX_TOTAL_TIME}s (no content)")
                        yield translator.error(
                            f"DeepSeek 在 {MAX_TOTAL_TIME}s 内无任何响应。"
                            "建议 /model 切换到其他模型后重试。"
                        )
                        return
                    elif attempt < max_retries:
                        log.warn(f"DS {reason} attempt {attempt}, no content yet, elapsed={elapsed_total:.0f}s, retrying...")
                        continue
                    else:
                        log.err(f"DS {reason}: all {max_retries} retries exhausted (no content)")
                        yield translator.error(
                            f"DeepSeek 在 {elapsed_total:.0f}s 内无响应，已重试 {max_retries} 次。"
                            "建议 /model 切换到其他模型后重试。"
                        )
                        return
                except Exception as e:
                    log.err(f"DS stream error: {e}")
                    yield translator.error(str(e))
                    return
                finally:
                    if conn:
                        conn.close()

            for r in _flush_buffer_lines(buf, translator):
                yield r

            if translator.reasoning_so_far:
                remember_reasoning(
                    session_key(body),
                    [
                        {
                            "role": "assistant",
                            "content": translator.content_so_far,
                            "reasoning_content": translator.reasoning_so_far,
                        }
                    ],
                )
            yield translator.done(None)
            log.timing(int((time.time() - t0) * 1000))

        self._sse_response(generate())

    # ── GPT 透传处理 ──
    def _handle_gpt_passthrough(self, body: dict, body_bytes: bytes) -> None:
        """GPT 模型请求路由：
        1. Codex device-code OAuth → ChatGPT Codex Responses 后端（ChatGPT Plus/Pro 额度）
        2. OPENAI_API_KEY → OpenAI Platform /v1/responses（回退，需代理 18080 翻墙）
        3. 可选 app-server relay（只在 gpt_enable_app_server_fallback=true 时启用）
        """
        model = body.get("model", GPT_DEFAULT_MODEL)

        # ── 路径 1（优先）：device-code OAuth → ChatGPT Codex backend ──
        # 所有 GPT 模型优先走 OAuth，避免 api.openai.com 直连被 GFW 阻断
        auth = _load_gpt_auth()
        if auth:
            log.req(f"GPT (ChatGPT device-code): {model}")
            self._handle_gpt_chatgpt_backend(body, auth)
            return

        # ── 路径 2：OPENAI_API_KEY → OpenAI Platform Responses API（回退） ──
        if OPENAI_API_KEY and _platform_preferred_gpt_model(model):
            log.req(f"GPT (OpenAI API key preferred): {model}")
            self._handle_gpt_openai_responses(body, OPENAI_API_KEY)
            return

        if OPENAI_API_KEY:
            log.req(f"GPT (OpenAI API key): {model}")
            self._handle_gpt_openai_responses(body, OPENAI_API_KEY)
            return

        # ── 路径 3：可选 app-server relay（保底，不保留原始 tool-call SSE） ──
        if GPT_ENABLE_APP_SERVER_FALLBACK and (
            os.path.exists(GPT_AUTH_FILE) or os.path.exists(GPT_AUTH_FILE_FALLBACK)
        ):
            log.req(f"GPT (app-server relay fallback): {model}")
            self._handle_gpt_app_server_relay(body, model)
            return

        log.err("GPT auth unavailable")
        self._json_response(
            {
                "error": {
                    "type": "auth_error",
                    "message": (
                        "No GPT OAuth token or API key. Run `codex login --device-auth` "
                        "to create ~/.codex/auth.gpt.json, or set openai_api_key in .env."
                    ),
                }
            },
            502,
        )

    def _handle_gpt_chatgpt_backend(self, body: dict, auth: dict) -> None:
        """Native GPT path for device-code login.

        The upstream already speaks Responses API SSE, so streaming requests are
        relayed byte-for-byte.  This preserves function_call/tool events for the
        outer Codex client, unlike nested `codex exec` relay.
        """
        stream = body.get("stream") is not False
        if stream:
            self._handle_gpt_chatgpt_stream(body, auth)
        else:
            self._handle_gpt_chatgpt_non_stream(body, auth)

    def _handle_gpt_chatgpt_stream(self, body: dict, auth: dict) -> None:
        model = body.get("model", GPT_DEFAULT_MODEL)

        def generate():
            conn = None
            t0 = time.time()
            body_size = 0
            try:
                upstream_body = _prepare_chatgpt_unified_body(body, upstream_stream=True)
                body_size = len(
                    json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
                )
                log.info(
                    f"GPT upstream start model={model} input_bytes={body_size} "
                    f"idle_timeout={GPT_STREAM_IDLE_TIMEOUT}s "
                    f"max_total={GPT_STREAM_MAX_TOTAL_TIME}s"
                )
                status, resp, conn = _chatgpt_responses_request(
                    upstream_body, auth, stream=True
                )
                if status != 200 or isinstance(resp, str):
                    ttfb_connect_ms = int((time.time() - t0) * 1000)
                    log.ttfb(ttfb_connect_ms, model, "connect")
                    err_body = resp if isinstance(resp, str) else str(resp)
                    log.err(f"GPT ChatGPT backend {status}: {err_body[:500]}")
                    yield from _assistant_message_sse(
                        _gpt_chatgpt_error_message(model, status, err_body), model
                    )
                    return

                # TTFB connect: HTTP 连接 + TLS + 上游 response headers 耗时
                ttfb_connect_ms = int((time.time() - t0) * 1000)
                log.ttfb(ttfb_connect_ms, model, "connect")

                _set_socket_read_timeout(conn, GPT_STREAM_IDLE_TIMEOUT)
                terminal_buf = b""
                first_token_logged = False
                while True:
                    if time.time() - t0 > GPT_STREAM_MAX_TOTAL_TIME:
                        msg = (
                            f"GPT stream exceeded {GPT_STREAM_MAX_TOTAL_TIME}s total "
                            f"deadline (model={model}, input_bytes={body_size})"
                        )
                        log.err(msg)
                        yield from _failed_response_sse(
                            msg, model, "proxy_total_timeout"
                        )
                        return
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    # TTFB first_token: 首个 SSE 数据块的到达时间
                    if not first_token_logged:
                        first_token_logged = True
                        ttfb_token_ms = int((time.time() - t0) * 1000)
                        log.ttfb(ttfb_token_ms, model, "first_token")
                    yield chunk
                    terminal_buf += chunk
                    if _responses_sse_terminal_seen(terminal_buf):
                        log.info(
                            f"GPT upstream terminal event observed; closing stream "
                            f"model={model}"
                        )
                        # 读完上游剩余数据（不丢字节）
                        while True:
                            tail = resp.read(4096)
                            if not tail:
                                break
                            yield tail
                            terminal_buf += tail
                        # 补发标准 response.done 事件（Responses API 流的最终事件）。
                        # Codex 依赖它在 response.completed 之后结束 turn；
                        # 若连接保持 keep-alive 且无此事件，客户端会永久等待 → turn hang。
                        resp_id = _extract_responses_id(terminal_buf) or _rand_id("resp")
                        done_payload = {
                            "type": "response.done",
                            "response": {
                                "id": resp_id,
                                "object": "response",
                                "status": "completed",
                                "model": model,
                                "output": [],
                                "usage": None,
                            },
                        }
                        yield f"event: response.done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                        yield "event: done\ndata: [DONE]\n\n"
                        return
            except socket.timeout as e:
                msg = (
                    f"GPT stream idle for {GPT_STREAM_IDLE_TIMEOUT}s "
                    f"(model={model}, input_bytes={body_size}): {e}"
                )
                log.err(msg)
                yield from _failed_response_sse(msg, model, "proxy_idle_timeout")
            except IncompleteRead as e:
                msg = f"GPT ChatGPT backend stream disconnected (model={model}): {e}"
                log.err(msg)
                yield from _failed_response_sse(msg, model, "proxy_disconnected")
            except Exception as e:
                log.err(f"GPT ChatGPT backend stream error: {e}")
                yield from _failed_response_sse(str(e), model, "proxy_error")
            finally:
                log.timing(int((time.time() - t0) * 1000))
                if conn:
                    conn.close()

        self._sse_response(generate())

    def _handle_gpt_chatgpt_non_stream(self, body: dict, auth: dict) -> None:
        """ChatGPT Codex backend requires stream=true; aggregate SSE for JSON callers."""
        model = body.get("model", GPT_DEFAULT_MODEL)
        conn = None
        t0 = time.time()
        try:
            upstream_body = _prepare_chatgpt_unified_body(body, upstream_stream=True)
            status, resp, conn = _chatgpt_responses_request(upstream_body, auth, stream=True)
            elapsed_ms = int((time.time() - t0) * 1000)
            if status != 200 or isinstance(resp, str):
                err_body = resp if isinstance(resp, str) else str(resp)
                log.err(f"GPT ChatGPT backend {status}: {err_body[:500]}")
                self._json_response(
                    {
                        "error": {
                            "type": "upstream_error",
                            "code": f"upstream_{status}",
                            "message": _gpt_chatgpt_error_message(model, status, err_body),
                        }
                    },
                    _upstream_status(status),
                )
                return
            completed_response, text = _parse_responses_sse_to_response(resp)
            if completed_response:
                if text and not completed_response.get("output"):
                    completed_response["output"] = [
                        {
                            "id": _rand_id("msg", 6),
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": text,
                                    "annotations": [],
                                }
                            ],
                            "status": "completed",
                        }
                    ]
                self._json_response(completed_response, 200)
            else:
                self._json_response(
                    {
                        "id": _rand_id("resp", 10),
                        "object": "response",
                        "status": "completed",
                        "model": model,
                        "output": [
                            {
                                "id": _rand_id("msg", 6),
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": text,
                                        "annotations": [],
                                    }
                                ],
                                "status": "completed",
                            }
                        ],
                        "usage": None,
                    },
                    200,
                )
            log.timing(elapsed_ms)
        except Exception as e:
            log.err(f"GPT ChatGPT non-stream error: {e}")
            self._json_response({"error": {"message": str(e)}}, 502)
        finally:
            if conn:
                conn.close()

    def _handle_gpt_openai_responses(self, body: dict, api_key: str) -> None:
        """Fallback for real OpenAI Platform API keys using /v1/responses."""
        stream = body.get("stream") is not False
        model = body.get("model", GPT_DEFAULT_MODEL)
        if stream:
            def generate():
                conn = None
                t0 = time.time()
                try:
                    status, resp, conn = _openai_responses_request(
                        body, api_key, stream=True
                    )
                    if status != 200 or isinstance(resp, str):
                        ttfb_connect_ms = int((time.time() - t0) * 1000)
                        log.ttfb(ttfb_connect_ms, model, "connect")
                        err_body = resp if isinstance(resp, str) else str(resp)
                        log.err(f"GPT OpenAI Responses {status}: {err_body[:500]}")
                        yield from _assistant_message_sse(
                            _upstream_error_message(status, err_body), model
                        )
                        return
                    # TTFB connect: HTTP 连接 + TLS + 上游 response headers 耗时
                    ttfb_connect_ms = int((time.time() - t0) * 1000)
                    log.ttfb(ttfb_connect_ms, model, "connect")

                    _set_socket_read_timeout(conn, 300)
                    terminal_buf = b""
                    first_token_logged = False
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        if not first_token_logged:
                            first_token_logged = True
                            ttfb_token_ms = int((time.time() - t0) * 1000)
                            log.ttfb(ttfb_token_ms, model, "first_token")
                        yield chunk
                        terminal_buf += chunk
                        if _responses_sse_terminal_seen(terminal_buf):
                            return
                except Exception as e:
                    log.err(f"GPT OpenAI Responses stream error: {e}")
                    yield from _assistant_message_sse(str(e), model)
                finally:
                    log.timing(int((time.time() - t0) * 1000))
                    if conn:
                        conn.close()

            self._sse_response(generate())
        else:
            status, resp_body, _ = _openai_responses_request(body, api_key, stream=False)
            if status != 200:
                self._json_response(
                    {
                        "error": {
                            "type": "upstream_error",
                            "code": f"upstream_{status}",
                            "message": _upstream_error_message(status, resp_body),
                        }
                    },
                    _upstream_status(status),
                )
                return
            try:
                self._json_response(json.loads(resp_body), 200)
            except Exception as e:
                self._json_response({"error": {"message": str(e)}}, 502)

    def _handle_chat_completions_route(self) -> None:
        """处理 /v1/chat/completions 请求（Qwen Code 等 OpenAI 兼容客户端）。

        路由逻辑：
        - GPT 模型 → Chat Completions → Responses 格式转换 → chatgpt.com (OAuth)
        - 非 GPT 模型（含 DS）→ 直接转发到配置的上游
        """
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_len).decode("utf-8")
            body = json.loads(raw)
        except Exception as e:
            self._json_response({"error": {"message": str(e)}}, 400)
            return

        requested_model = (body.get("model") or "").lower()

        if _is_gpt_model(requested_model):
            # 提取请求中的 API key（Qwen Code 通过 Authorization header 传递）
            api_key = self._extract_bearer_token()
            is_openai_api_key = bool(api_key and api_key.startswith("sk-"))

            # 路径 1（优先）：OAuth → ChatGPT Codex backend
            # 所有 GPT 模型优先走 ChatGPT 订阅 OAuth，避免 api.openai.com 直连被 GFW 阻断
            auth = _load_gpt_auth()
            if auth and auth.get("access_token"):
                log.req(f"GPT (ChatGPT OAuth): {requested_model}")
                self._handle_gpt_chat_completions_via_chatgpt(body, auth)
                return

            # 路径 2（回退）：API Key 直连 OpenAI API /v1/chat/completions
            if is_openai_api_key:
                log.req(f"GPT (API key direct → api.openai.com): {requested_model}")
                self._handle_gpt_chat_completions_direct(body, api_key)
                return

            self._json_response(
                {
                    "error": {
                        "message": (
                            "GPT requires authentication. "
                            "Set OPENAI_API_KEY for direct API access, "
                            "or run 'codex login' for ChatGPT subscription."
                        )
                    }
                },
                401,
            )
            return

        # 非 GPT 模型（DeepSeek 等）：使用现有 Chat Completions 翻译流程
        try:
            built = build_chat_body(body)
        except Exception as e:
            log.err(f"build: {e}")
            self._json_response({"error": {"message": str(e)}}, 400)
            return

        chat_body = built["chat_body"]
        stream = built["stream"]

        if not stream:
            self._handle_non_stream(body, chat_body)
        else:
            self._handle_stream(body, chat_body)

    def _extract_bearer_token(self) -> str | None:
        """从请求 Authorization header 提取 Bearer token。"""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return None

    def _handle_gpt_chat_completions_direct(self, body: dict, api_key: str) -> None:
        """API Key 直连 OpenAI /v1/chat/completions。

        将 Qwen Code 的 Chat Completions 请求直接转发到 api.openai.com，
        使用 API key 鉴权，支持 streaming 和 non-streaming 两种模式。
        """
        stream = body.get("stream", False)
        requested_model = body.get("model", GPT_DEFAULT_MODEL)

        # 清理 body：移除代理内部字段
        clean_body = {k: v for k, v in body.items() if k not in ("thinking",)}
        clean_body["model"] = requested_model

        # 通过代理访问外网（如果设置了 HTTPS_PROXY）
        import ssl
        parsed = urlparse(GPT_API_BASE)
        host = parsed.netloc or "api.openai.com"
        path = "/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

        body_bytes = json.dumps(clean_body).encode("utf-8")

        conn = HTTPSConnection(host, timeout=GPT_CONNECT_TIMEOUT)
        try:
            conn.request("POST", path, body=body_bytes, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                err_body = resp.read().decode()[:500]
                log.err(f"GPT direct API error {resp.status}: {err_body[:200]}")
                conn.close()
                self._json_response(
                    {"error": {"message": f"OpenAI API error {resp.status}: {err_body}"}},
                    resp.status if resp.status < 500 else 502,
                )
                return

            if stream:
                # Streaming: 逐块转发 SSE
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception as e:
                    log.err(f"GPT direct stream relay error: {e}")
                finally:
                    conn.close()
            else:
                # Non-streaming: 直接返回 JSON
                data = resp.read().decode()
                conn.close()
                self._json_response(json.loads(data), 200)
        except Exception as e:
            conn.close()
            log.err(f"GPT direct API connection error: {e}")
            self._json_response(
                {"error": {"message": f"OpenAI API connection failed: {e}"}},
                502,
            )

    def _handle_gpt_chat_completions_via_chatgpt(self, body: dict, auth: dict) -> None:
        """GPT Chat Completions → chatgpt.com Responses backend (OAuth)。

        将 Chat Completions 请求转换为 Responses API 格式，
        通过 chatgpt.com/backend-api/codex/responses 转发（走 ChatGPT Plus 订阅）。
        响应转换：Responses SSE → Chat Completions SSE / JSON。
        """
        stream = body.get("stream", False)
        requested_model = _normalize_gpt_model_for_chatgpt_backend(
            body.get("model", GPT_DEFAULT_MODEL)
        )

        # ── Chat Completions → Responses 格式转换 ──
        input_items = []
        for msg in body.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # ── role=tool → function_call_output（顶层 item） ──
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                tool_content = content if isinstance(content, str) else str(content)
                input_items.append({
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": tool_content,
                })
                continue

            # 确定 content 的 type：user/system→input_text, assistant→output_text
            if role == "assistant":
                content_type = "output_text"
            else:
                content_type = "input_text"

            tool_calls = msg.get("tool_calls")

            if isinstance(content, str):
                if content:
                    parts = [{"type": content_type, "text": content}]
                else:
                    parts = []
                if parts:
                    input_items.append({"role": role, "content": parts})
            elif isinstance(content, list):
                # 多模态或 tool_use content：逐项转换
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        p_type = p.get("type", "")
                        if p_type == "text":
                            parts.append({"type": content_type, "text": p.get("text", "")})
                        elif p_type == "tool_use":
                            # tool_use → 顶层 function_call item
                            if parts:
                                input_items.append({"role": role, "content": parts})
                                parts = []
                            input_items.append({
                                "type": "function_call",
                                "call_id": p.get("id", ""),
                                "name": p.get("name", ""),
                                "arguments": json.dumps(p.get("input", {}), ensure_ascii=False),
                            })
                        elif p_type == "tool_result":
                            # tool_result → 顶层 function_call_output item
                            if parts:
                                input_items.append({"role": role, "content": parts})
                                parts = []
                            input_items.append({
                                "type": "function_call_output",
                                "call_id": p.get("tool_call_id", p.get("id", "")),
                                "output": p.get("content", ""),
                            })
                        elif p_type == "image_url":
                            # image_url → ChatGPT Responses 支持的 input_image
                            img = p.get("image_url", {})
                            img_url = img.get("url", "") if isinstance(img, dict) else str(img)
                            parts.append({
                                "type": "input_image",
                                "image_url": img_url,
                                "detail": img.get("detail", "low") if isinstance(img, dict) else "low",
                            })
                        else:
                            parts.append(p)
                    else:
                        parts.append(p)
                if parts:
                    input_items.append({"role": role, "content": parts})
            else:
                # content 为 None（如 assistant 带 tool_calls 但无文本）
                # 不添加 role+content item，仅处理 tool_calls（见下方统一逻辑）
                pass

            # 统一处理 assistant 的 tool_calls → 顶层 function_call items
            # 仅对非 list 的 content 生效（list 分支已内部处理 tool_use）
            if role == "assistant" and not isinstance(content, list) and isinstance(tool_calls, list):
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })

        # 处理 system message: 转为 instructions
        instructions = body.get("instructions", "")
        filtered_input = []
        for item in input_items:
            if item.get("role") == "system":
                system_text = item["content"][0].get("text", "") if item["content"] else ""
                if system_text:
                    instructions = (instructions + "\n" + system_text).strip()
            else:
                filtered_input.append(item)
        if not filtered_input:
            filtered_input = [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}]

        responses_body = {
            "model": requested_model,
            "input": filtered_input,
            "stream": True,  # chatgpt.com 强制 streaming
            "store": False,
        }
        if instructions:
            responses_body["instructions"] = instructions
        # 转换 tools 格式：Chat Completions → Responses API
        if body.get("tools"):
            responses_body["tools"] = _translate_chat_tools_for_responses(body["tools"])
        if body.get("tool_choice"):
            responses_body["tool_choice"] = _translate_chat_tool_choice_for_responses(body["tool_choice"])
        # chatgpt.com backend 不支持 max_output_tokens / max_tokens 参数，跳过
        # （两个参数名均返回 400 Unsupported parameter）

        body_size = len(json.dumps(responses_body, ensure_ascii=False).encode("utf-8"))
        log.info(f"GPT ChatCompletions->Responses: model={requested_model} input_messages={len(filtered_input)} body={body_size}B stream={stream}")

        if not stream:
            # 非流式：内部流式 → 收集完整 → 转 Chat Completions JSON
            self._handle_gpt_chat_to_non_stream(responses_body, auth, requested_model)
        else:
            # 流式：Responses SSE → Chat Completions SSE 实时转换
            self._handle_gpt_chat_to_stream(responses_body, auth, requested_model)

    def _handle_gpt_chat_to_stream(self, responses_body: dict, auth: dict, model: str) -> None:
        """流式：Responses SSE → Chat Completions SSE，实时转发。"""
        def generate():
            t0 = time.time()
            try:
                status, resp, conn = _chatgpt_responses_request(responses_body, auth, stream=True)
                if status != 200 or isinstance(resp, str):
                    ttfb_connect_ms = int((time.time() - t0) * 1000)
                    log.ttfb(ttfb_connect_ms, model, "connect")
                    err_body = resp if isinstance(resp, str) else str(resp)
                    log.err(f"GPT Chat->Responses {status}: {err_body[:300]}")
                    # 返回标准 Chat Completions 错误格式
                    yield f'data: {json.dumps({"error": {"message": f"Upstream error {status}", "type": "upstream_error"}})}\n\n'
                    yield "data: [DONE]\n\n"
                    return
                ttfb_connect_ms = int((time.time() - t0) * 1000)
                log.ttfb(ttfb_connect_ms, model, "connect")
                _set_socket_read_timeout(conn, GPT_STREAM_IDLE_TIMEOUT)
                first_token_logged = False
                resp_id = _rand_id("chatcmpl")
                created = int(time.time())
                buf = b""
                while True:
                    if time.time() - t0 > GPT_STREAM_MAX_TOTAL_TIME:
                        yield f'data: {json.dumps({"error": {"message": "Stream timeout", "type": "timeout"}})}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        line = line_bytes.decode("utf-8")
                        if not line.startswith("data: "):
                            continue
                        json_str = line[6:].strip()
                        if json_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            evt = json.loads(json_str)
                            evt_type = evt.get("type", "")
                            # 记录 usage（response.completed 时输出 token 统计）
                            if evt_type == "response.completed":
                                resp_full = evt.get("response", {})
                                usage = resp_full.get("usage") or {}
                                out = usage.get("output_tokens", "?")
                                out_det = usage.get("output_tokens_details") or {}
                                reasoning_t = out_det.get("reasoning_tokens", 0)
                                incomplete = resp_full.get("incomplete_details")
                                trunc = resp_full.get("truncation")
                                log.info(f"  [USAGE] output_tokens={out} reasoning_tokens={reasoning_t} incomplete={incomplete} truncation={trunc}")
                            # 首个有效内容事件记录 TTFB
                            if not first_token_logged and evt_type in ("response.output_text.delta", "response.output_item.added"):
                                first_token_logged = True
                                ttfb_token_ms = int((time.time() - t0) * 1000)
                                log.ttfb(ttfb_token_ms, model, "first_token")
                            # Responses SSE → Chat Completions SSE 转换
                            chunk_json = _responses_to_chat_completion_chunk(evt, resp_id, created, model)
                            if chunk_json:
                                yield f"data: {json.dumps(chunk_json, ensure_ascii=False)}\n\n"
                        except (json.JSONDecodeError, ValueError):
                            pass
                yield "data: [DONE]\n\n"
            except Exception as e:
                log.err(f"GPT Chat→Stream error: {e}")
                yield f'data: {json.dumps({"error": {"message": str(e), "type": "internal_error"}})}\n\n'
                yield "data: [DONE]\n\n"
            finally:
                log.timing(int((time.time() - t0) * 1000))
                if conn:
                    conn.close()

        self._sse_chat_completions_response(generate())

    def _handle_gpt_chat_to_non_stream(self, responses_body: dict, auth: dict, model: str) -> None:
        """非流式：内部流式收集完整 → Chat Completions JSON 返回。"""
        t0 = time.time()
        full_text = ""
        error_msg = None
        # 工具调用跟踪（非流式模式，用本地变量而非全局 _tc_state）
        tc_state: dict = {}
        tool_calls: list = []

        def _flush_tc():
            """将当前 tc_state 保存到 tool_calls 列表（处理并行多工具调用）"""
            if tc_state.get("call_id"):
                tool_calls.append({
                    "id": tc_state["call_id"],
                    "type": "function",
                    "function": {
                        "name": tc_state["name"],
                        "arguments": tc_state["arguments"],
                    },
                })
                tc_state.clear()

        try:
            status, resp, conn = _chatgpt_responses_request(responses_body, auth, stream=True)
            if status != 200 or isinstance(resp, str):
                err_body = resp if isinstance(resp, str) else str(resp)
                self._json_response(
                    {"error": {"message": f"Upstream error {status}: {err_body[:300]}", "type": "upstream_error"}},
                    502,
                )
                return
            _set_socket_read_timeout(conn, GPT_STREAM_IDLE_TIMEOUT)
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8")
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:].strip()
                    if json_str == "[DONE]":
                        break
                    try:
                        evt = json.loads(json_str)
                        evt_type = evt.get("type", "")
                        if evt_type == "response.output_text.delta":
                            delta = evt.get("delta", "")
                            full_text += delta
                        elif evt_type == "response.output_item.added":
                            item = evt.get("item", {})
                            if item.get("type") == "function_call":
                                _flush_tc()  # 先保存前一个并行工具调用
                                tc_state["call_id"] = item.get("id", "")
                                tc_state["name"] = item.get("name", "")
                                tc_state["arguments"] = ""
                        elif evt_type == "response.function_call_arguments.delta":
                            tc_state["arguments"] = tc_state.get("arguments", "") + evt.get("delta", "")
                        elif evt_type == "response.function_call_arguments.done":
                            tc_state["arguments"] = evt.get("arguments", tc_state.get("arguments", ""))
                        elif evt_type == "response.completed":
                            _flush_tc()  # 保存最后一个工具调用
                    except (json.JSONDecodeError, ValueError):
                        pass
                if b"[DONE]" in buf:
                    break
        except Exception as e:
            error_msg = str(e)
        finally:
            log.timing(int((time.time() - t0) * 1000))
            if conn:
                conn.close()

        if error_msg:
            self._json_response({"error": {"message": error_msg, "type": "internal_error"}}, 502)
            return

        # 构建 Chat Completions 响应
        msg = {"role": "assistant", "content": full_text or None}
        finish_reason = "stop"
        if tool_calls:
            msg["tool_calls"] = tool_calls
            finish_reason = "tool_calls"
        chat_response = {
            "id": _rand_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": msg,
                "finish_reason": finish_reason,
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        self._json_response(chat_response, 200)

    def _sse_chat_completions_response(self, generate):
        """发送 Chat Completions SSE 流响应。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for chunk in generate:
                if isinstance(chunk, str):
                    self.wfile.write(chunk.encode("utf-8"))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                self.wfile.flush()
            except Exception:
                pass
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            self.close_connection = True

    def _sse_response(self, generate):
        """发送 SSE 流响应（Responses API 路径，Codex 使用）。

        Codex Responses 协议需要连接保持到 response.completed 事件被完整处理，
        因此使用 keep-alive，不主动关闭。turn hang 已通过 /models 端点 owned_by
        修复为 "codex-deepseek" 解决。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for chunk in generate:
                if isinstance(chunk, str):
                    self.wfile.write(chunk.encode("utf-8"))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_gpt_chat_completions(self, body: dict, access_token: str) -> None:
        """Chat Completions API 路径（使用 OAuth token）"""
        try:
            built = build_chat_body(body)
        except Exception as e:
            log.err(f"GPT build: {e}")
            self._json_response({"error": {"message": str(e)}}, 400)
            return

        chat_body = built["chat_body"]
        stream = built["stream"]
        messages = built["messages"]

        chat_body["model"] = body.get("model", GPT_DEFAULT_MODEL)
        chat_body.pop("thinking", None)
        if IDENTITY_MODEL:
            for i, m in enumerate(chat_body.get("messages", [])):
                if m.get("role") == "system" and "Your true model identity is" in m.get("content", ""):
                    chat_body["messages"].pop(i)
                    break

        if not stream:
            self._handle_gpt_non_stream_chat(chat_body, access_token, body, messages)
        else:
            self._handle_gpt_stream_chat(chat_body, access_token, body, messages)

    # ── App-Server 常驻中继（利用 codex app-server WebSocket 协议） ──
    def _sse_error_response(self, message: str, model: str = None) -> None:
        """发送独立 SSE 错误，但格式化为 assistant 消息以便 codex 显示给用户。
        不使用 protocol 'error' 事件（codex 不会展示），而是伪装成正常 output_text。"""
        if model is None:
            model = MODEL
        def gen():
            resp_id = _rand_id("resp")
            msg_id = _rand_id("msg")
            # response.created（生命周期开始）
            yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
            # 作为 assistant 消息输出错误文本
            yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': message})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': message})}\n\n"
            yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': message, 'annotations': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': message, 'annotations': []}], 'status': 'completed'}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': None}})}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
        self._sse_response(gen())

    def _handle_gpt_app_server_relay(self, body: dict, model: str) -> None:
        """通过常驻的 codex app-server 中转 GPT 请求（零冷启动延迟）。
        使用 app-server 的 JSON-RPC/WebSocket 协议（thread/start → turn/start → agentMessageDelta）。
        """
        # 确保 app-server 运行中
        started = start_app_server_if_needed()
        if started:
            log.ok("app-server started for GPT relay")

        # 从 Responses API input 构建对话 prompt（异常安全：失败则返回 SSE 错误）
        try:
            prompt = _build_relay_prompt(body)
        except Exception as e:
            log.err(f"GPT relay build_prompt error: {e}")
            self._sse_error_response(f"Failed to build GPT prompt: {e}", model)
            return

        instructions = body.get("instructions", "")
        effort = body.get("reasoning", {}).get("effort", "xhigh") if isinstance(body.get("reasoning"), dict) else "xhigh"

        log.info(f"app-server relay: model={model} effort={effort} prompt_len={len(prompt)}")

        relay_model = _normalize_gpt_model_for_chatgpt_backend(model)
        self._relay_app_server_sse(prompt, relay_model, effort, instructions)

    def _relay_app_server_sse(self, prompt: str, model: str, effort: str, instructions: str) -> None:
        """执行 app-server 中继，将结果转为 Responses API SSE 格式返回。"""
        def generate():
            try:
                t0 = time.time()
                success, full_text, error_msg, metadata = relay_via_app_server(
                    prompt=prompt,
                    model=model,
                    reasoning_effort=effort,
                    instructions=instructions,
                    timeout=180,
                )
                elapsed_ms = int((time.time() - t0) * 1000)
                log.timing(elapsed_ms)

                resp_id = _rand_id("resp")
                msg_id = _rand_id("msg")

                # 发送 response.created（无论成败都发，保持 SSE 协议完整）
                yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"

                if not success:
                    log.err(f"GPT app-server relay failed: {error_msg}")
                    # 错误作为 assistant 消息返回，用户在 TUI 中直接看到
                    yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': error_msg[:500]})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': error_msg[:500]})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': error_msg[:500], 'annotations': []}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': error_msg[:500], 'annotations': []}], 'status': 'completed'}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': None}})}\n\n"
                    yield "event: done\ndata: [DONE]\n\n"
                    return

                log.ok(f"GPT app-server relay OK: {len(full_text)} chars")

                # 标准 Responses API SSE 事件序列（与 codex 原生返回格式一致）
                yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': full_text})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': full_text})}\n\n"
                yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': full_text, 'annotations': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}], 'status': 'completed'}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': None}})}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
            except Exception as e:
                # 生成器内异常：格式化为 assistant 消息，确保用户看到错误文本
                err_text = f"GPT relay internal error: {e}"
                log.err(err_text)
                resp_id = _rand_id("resp")
                msg_id = _rand_id("msg")
                yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': err_text})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': err_text})}\n\n"
                yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': err_text, 'annotations': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': err_text, 'annotations': []}], 'status': 'completed'}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': None}})}\n\n"
                yield "event: done\ndata: [DONE]\n\n"

        self._sse_response(generate())

    def _handle_gpt_non_stream_chat(self, chat_body: dict, access_token: str, body: dict, messages: list) -> None:
        """GPT 非流式：Chat Completions → Responses 格式转换"""
        t0 = time.time()
        status, resp_body, _conn = _openai_chat_request(chat_body, access_token, stream=False)
        elapsed_ms = int((time.time() - t0) * 1000)
        log.timing(elapsed_ms)
        if status != 200:
            log.err(f"GPT upstream {status}: {resp_body[:300]}")
            self._json_response(
                {"error": {"type": "upstream_error", "code": f"upstream_{status}", "message": _upstream_error_message(status, resp_body)}},
                _upstream_status(status),
            )
            return
        try:
            completion = json.loads(resp_body)
        except Exception as e:
            log.err(f"GPT parse: {e}")
            self._json_response({"error": {"message": str(e)}}, 502)
            return
        # 复用 build_non_stream_response 进行 Chat Completions → Responses 转换
        msg = (completion.get("choices") or [{}])[0].get("message", {})
        if msg.get("reasoning_content"):
            remember_reasoning(session_key(body), [msg])
        response = build_non_stream_response(
            completion,
            model=body.get("model", GPT_DEFAULT_MODEL),
            response_prefix="resp",
        )
        usg = completion.get("usage")
        if usg:
            log.toks(usg.get("prompt_tokens"), usg.get("completion_tokens"), usg.get("total_tokens"))
        self._json_response(response, 200)

    def _handle_gpt_stream_chat(self, chat_body: dict, access_token: str, body: dict, messages: list) -> None:
        """GPT 流式：Chat Completions SSE → Responses SSE 翻译（复用 SseTranslator）"""
        def generate():
            translator = SseTranslator(
                model=body.get("model", GPT_DEFAULT_MODEL),
                response_prefix="resp",
            )
            conn = None
            t0 = time.time()
            try:
                status, resp, conn = _openai_chat_request(chat_body, access_token, stream=True)
                if status != 200 or isinstance(resp, str):
                    err_body = resp if isinstance(resp, str) else resp[:300]
                    log.err(f"GPT stream upstream {status}: {err_body}")
                    yield translator.error(_upstream_error_message(status, err_body))
                    return
                _set_socket_read_timeout(conn, 300)
                buf = b""
                try:
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line_bytes, buf = buf.split(b"\n", 1)
                            line = line_bytes.decode("utf-8")
                            if not line.startswith("data: "):
                                continue
                            json_str = line[6:].strip()
                            if json_str == "[DONE]":
                                continue
                            try:
                                parsed = json.loads(json_str)
                                result = translator.feed(parsed)
                                if result:
                                    yield result
                            except (json.JSONDecodeError, ValueError):
                                pass
                except (IncompleteRead, socket.timeout) as e:
                    reason = "timeout" if isinstance(e, socket.timeout) else "disconnected"
                    log.warn(f"GPT stream {reason} early ({e})")
                    for line_bytes in buf.split(b"\n"):
                        if not line_bytes:
                            continue
                        line = line_bytes.decode("utf-8")
                        if not line.startswith("data: "):
                            continue
                        json_str = line[6:].strip()
                        if json_str == "[DONE]":
                            continue
                        try:
                            parsed = json.loads(json_str)
                            result = translator.feed(parsed)
                            if result:
                                yield result
                        except (json.JSONDecodeError, ValueError):
                            pass
                # Flush remaining
                for line_bytes in buf.split(b"\n"):
                    if not line_bytes:
                        continue
                    line = line_bytes.decode("utf-8")
                    if not line.startswith("data: "):
                        continue
                    json_str = line[6:].strip()
                    if json_str == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(json_str)
                        result = translator.feed(parsed)
                        if result:
                            yield result
                    except (json.JSONDecodeError, ValueError):
                        pass
                if translator.reasoning_so_far:
                    remember_reasoning(
                        session_key(body),
                        [{
                            "role": "assistant",
                            "content": translator.content_so_far,
                            "reasoning_content": translator.reasoning_so_far,
                        }],
                    )
                yield translator.done(None)
            except Exception as e:
                log.err(f"GPT stream error: {e}")
                yield translator.error(str(e))
            finally:
                log.timing(int((time.time() - t0) * 1000))
                if conn:
                    conn.close()

        self._sse_response(generate())

    # ── Codex Exec 中继方法 ──
    def _relay_non_stream(self, prompt: str, model: str = GPT_DEFAULT_MODEL) -> None:
        """执行 codex exec，捕获输出，转为 SSE 格式返回（含 status line）"""
        def generate():
            t0 = time.time()
            success, output, session_id, status = _run_codex_relay(prompt, model)
            elapsed_ms = int((time.time() - t0) * 1000)
            log.timing(elapsed_ms)

            resp_id = _rand_id("resp")
            msg_id = _rand_id("msg")

            if not success:
                yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'relay_error', 'message': output[:500]}})}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return

            # 提取响应文本
            response_text = _extract_response_text(output)

            # 构建 usage
            usage = None
            if status.get("total_tokens"):
                usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": status["total_tokens"],
                }

            # 标准 Responses API SSE 事件序列
            yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': model, 'output': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'status': 'in_progress'}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': response_text})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': response_text})}\n\n"
            yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': response_text, 'annotations': []}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': response_text, 'annotations': []}], 'status': 'completed'}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': model, 'output': [], 'usage': usage}})}\n\n"
            yield "event: done\ndata: [DONE]\n\n"

        self._sse_response(generate())

    def _relay_stream(self, prompt: str, config_swapped: bool) -> None:
        """流式：执行 codex exec，逐行转发为 SSE"""
        def generate():
            t0 = time.time()
            proc = _run_codex_relay_stream(prompt)
            if not proc:
                yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'relay_error', 'message': 'failed to spawn codex'}})}\n\n"
                return

            resp_id = _rand_id("resp")
            msg_id = _rand_id("msg")
            started = False

            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    # 发送 response.created + in_progress（首次）
                    if not started:
                        started = True
                        yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'status': 'in_progress', 'model': GPT_DEFAULT_MODEL, 'output': []}})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.in_progress', 'response_id': resp_id})}\n\n"

                    # 逐行作为 output_text delta
                    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': line})}\n\n"

                # 完成
                if started:
                    yield f"data: {json.dumps({'type': 'response.output_text.done', 'response_id': resp_id, 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': ''})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': resp_id, 'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '', 'annotations': []}], 'status': 'completed'}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'status': 'completed', 'model': GPT_DEFAULT_MODEL, 'output': [], 'usage': None}})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'relay_error', 'message': 'no output from codex'}})}\n\n"

                yield "event: done\ndata: [DONE]\n\n"
            except Exception as e:
                log.err(f"relay stream: {e}")
                yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'relay_error', 'message': str(e)[:500]}})}\n\n"
            finally:
                log.timing(int((time.time() - t0) * 1000))
                if proc:
                    proc.kill()

        self._sse_response(generate())


# ── Codex Exec 中继辅助函数 ──

# 中继时 auth.json / config.toml 备份
_RELAY_AUTH_BACKUP = None
_RELAY_CONFIG_BACKUP = None
CONFIG_PATH = os.path.expanduser("~/.codex/config.toml")

# GPT 会话缓存（避免每次 codex exec 冷启动）
_gpt_session_id: str | None = None
_gpt_session_model: str = ""
_GPT_SESSION_TTL = 600  # 10 分钟会话有效期
_gpt_session_last_use: float = 0.0


def _extract_response_text(output: str) -> str:
    """从 codex exec 输出中提取纯响应文本（跳过 meta 行）。"""
    lines = output.split("\n")
    response_text = ""
    in_response = False
    for line in lines:
        if line.startswith("codex") or line.startswith("tokens used"):
            if in_response:
                break
            continue
        if line.startswith("---") or line.startswith("workdir:") or line.startswith("model:"):
            continue
        if line.startswith("user"):
            continue
        if line.strip():
            in_response = True
            response_text += line + "\n"
    return response_text.strip() or output.strip()


def _build_relay_prompt(body: dict) -> str:
    """从 Responses API input 构建完整对话 transcript（保留跨模型上下文）。"""
    instructions = body.get("instructions", "")
    input_items = body.get("input", [])

    # input 可能是简单字符串（简化格式）或列表（完整格式）
    if isinstance(input_items, str):
        return input_items

    # 构建完整对话历史（包含 assistant 回复，确保跨模型上下文不丢失）
    transcript_parts = []
    last_user_msg = ""
    for item in input_items:
        role = item.get("role", "")
        content = ""
        if isinstance(item.get("content"), list):
            for c in item["content"]:
                if c.get("type") in ("input_text", "output_text"):
                    content += c.get("text", "")
        elif isinstance(item.get("content"), str):
            content = item["content"]

        if not content.strip():
            continue

        if role == "system":
            # system 消息作为背景说明
            transcript_parts.append(f"[Context: {content}]")
        elif role == "user":
            transcript_parts.append(f"User: {content}")
            last_user_msg = content
        elif role == "assistant":
            transcript_parts.append(f"Assistant: {content}")

    # 构建最终 prompt：instructions + 对话历史
    prompt = "\n".join(transcript_parts)
    if not prompt:
        prompt = last_user_msg or "continue"
    if instructions and instructions not in prompt:
        prompt = instructions + "\n\n" + prompt

    return prompt.strip() or "continue"


def _build_relay_config(model: str) -> str:
    """构建 codex exec 用的临时 GPT 配置（无 custom provider → 原生 OpenAI）"""
    return f'''model = "{model}"
model_reasoning_effort = "xhigh"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[projects."/data/WYC"]
trust_level = "trusted"

[projects."/tmp"]
trust_level = "trusted"
'''


def _swap_for_relay(gpt_config: str) -> bool:
    """临时切换 config.toml 为 GPT 原生模式 + 切换 auth.json 为 OAuth token。
    返回 True 表示已切换，需要后续恢复。
    """
    global _RELAY_AUTH_BACKUP, _RELAY_CONFIG_BACKUP
    swapped = False

    # 1. 备份并切换 config.toml
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                _RELAY_CONFIG_BACKUP = f.read()
        with open(CONFIG_PATH, "w") as f:
            f.write(gpt_config)
        swapped = True
    except Exception as e:
        log.err(f"swap config for relay: {e}")

    # 2. 切换 auth.json（如果需要）
    auth_path = os.path.expanduser("~/.codex/auth.json")
    try:
        with open(auth_path) as f:
            d = json.load(f)
        if not (d.get("tokens", {}).get("access_token") and d.get("tokens", {}).get("refresh_token")):
            # 无 OAuth token，从备用文件加载
            for src in (GPT_AUTH_FILE_FALLBACK, GPT_AUTH_FILE):
                if os.path.exists(src):
                    with open(src) as f:
                        src_data = json.load(f)
                    if src_data.get("tokens", {}).get("access_token"):
                        if not _RELAY_AUTH_BACKUP:
                            _RELAY_AUTH_BACKUP = json.dumps(d)
                        with open(auth_path, "w") as f:
                            json.dump(src_data, f, indent=2, ensure_ascii=False)
                        break
    except Exception as e:
        log.err(f"swap auth for relay: {e}")

    return swapped


def _restore_after_relay() -> None:
    """恢复中继前备份的 config.toml 和 auth.json"""
    global _RELAY_AUTH_BACKUP, _RELAY_CONFIG_BACKUP
    if _RELAY_CONFIG_BACKUP is not None:
        with open(CONFIG_PATH, "w") as f:
            f.write(_RELAY_CONFIG_BACKUP)
        _RELAY_CONFIG_BACKUP = None
    if _RELAY_AUTH_BACKUP is not None:
        auth_path = os.path.expanduser("~/.codex/auth.json")
        with open(auth_path, "w") as f:
            f.write(_RELAY_AUTH_BACKUP)
        _RELAY_AUTH_BACKUP = None


def _get_or_clear_session(model: str) -> str | None:
    """获取缓存的会话 ID，如果模型变了或过期则清除。"""
    global _gpt_session_id, _gpt_session_model, _gpt_session_last_use
    now = time.time()
    if _gpt_session_id and _gpt_session_model == model and (now - _gpt_session_last_use) < _GPT_SESSION_TTL:
        return _gpt_session_id
    # 过期或模型不匹配：清除
    _gpt_session_id = None
    _gpt_session_model = ""
    return None


def _save_session(session_id: str, model: str) -> None:
    """缓存会话 ID。"""
    global _gpt_session_id, _gpt_session_model, _gpt_session_last_use
    _gpt_session_id = session_id
    _gpt_session_model = model
    _gpt_session_last_use = time.time()


def _clear_session() -> None:
    """清除缓存的会话。"""
    global _gpt_session_id, _gpt_session_model
    _gpt_session_id = None
    _gpt_session_model = ""


def _run_codex_relay(prompt: str, model: str = GPT_DEFAULT_MODEL) -> tuple:
    """执行 codex exec 中继。支持会话复用。
    返回 (success: bool, output_text: str, session_id: str|None, status_info: dict)。
    """
    import subprocess

    session_id = _get_or_clear_session(model)
    if session_id:
        cmd = ["codex", "exec", "resume", session_id, "--skip-git-repo-check", prompt]
        log.info(f"relay: reusing session {session_id[:12]}...")
    else:
        cmd = ["codex", "exec", "--skip-git-repo-check", prompt]
        log.info("relay: new session")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd="/data/WYC",
            env={**os.environ, "CODEX_DISABLE_DANGER_FULL_ACCESS": "1"},
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            log.err(f"codex relay exit={result.returncode}: {stderr[:200]}")
            _clear_session()  # 失败时清除会话
            return False, stderr or output, None, {}

        # 提取 session ID
        new_session_id = None
        for line in output.split("\n"):
            if "session id:" in line.lower():
                new_session_id = line.split("session id:")[-1].strip().split()[0]
                break

        # 提取 status line 信息
        status = _parse_relay_status(output)
        status["model"] = model

        # 缓存会话
        if new_session_id:
            _save_session(new_session_id, model)
        elif not session_id:
            _clear_session()

        return True, output, new_session_id or session_id, status
    except subprocess.TimeoutExpired:
        log.err("codex relay timed out after 120s")
        _clear_session()
        return False, "GPT relay timed out", None, {}
    except FileNotFoundError:
        log.err("codex binary not found")
        return False, "codex binary not found", None, {}
    except Exception as e:
        log.err(f"codex relay error: {e}")
        _clear_session()
        return False, str(e), None, {}


def _parse_relay_status(output: str) -> dict:
    """从 codex exec 输出提取 status line 字段。"""
    status = {}
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("model:"):
            status["model"] = line.split("model:")[-1].strip()
        elif line.startswith("reasoning effort:"):
            status["reasoning_effort"] = line.split(":")[-1].strip()
        elif line.startswith("tokens used"):
            status["tokens_used"] = True
        elif status.get("tokens_used") and line.replace(",", "").strip().isdigit():
            status["total_tokens"] = int(line.replace(",", "").strip())
            del status["tokens_used"]
    return status


def _run_codex_relay_stream(prompt: str):
    """流式执行 codex exec，逐行产出输出。"""
    import subprocess

    cmd = ["codex", "exec", "--skip-git-repo-check", prompt]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/data/WYC",
            env={**os.environ, "CODEX_DISABLE_DANGER_FULL_ACCESS": "1"},
        )
        return proc
    except Exception as e:
        log.err(f"codex relay spawn error: {e}")
        return None


def _write_catalog_json():
    """Write a ready-to-use model catalog JSON so Codex recognizes MODEL."""
    import os as _os

    catalog = {
        "models": [
            {
                "slug": MODEL,
                "display_name": IDENTITY_MODEL or MODEL,
                "description": f"{IDENTITY_MODEL or MODEL} via codex-deepseek proxy",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 10,
                "context_window": 262144,
                "max_context_window": 1048576,
                "effective_context_window_percent": 95,
                "auto_compact_token_limit": 196608,
                "max_completion_tokens": 32768,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "supports_image_detail_original": False,
                "supports_parallel_tool_calls": True,
                "supports_search_tool": False,
                "web_search_tool_type": "text_and_image",
                "apply_patch_tool_type": "freeform",
                "shell_type": "shell_command",
                "supports_reasoning_summaries": False,
                "default_reasoning_summary": "none",
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast responses"},
                    {"effort": "medium", "description": "Balanced speed and depth"},
                    {
                        "effort": "high",
                        "description": "Deep reasoning for complex problems",
                    },
                ],
                "support_verbosity": False,
                "default_verbosity": "medium",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "base_instructions": f"You are a coding agent powered by {IDENTITY_MODEL or MODEL}.",
                "cost": {"input": 0, "output": 0},
                "release_date": "2025-01-01",
                "last_updated": "2025-01-01",
                "structured_output": False,
                "open_weights": False,
                "attachment": False,
                "experimental_supported_tools": [],
                "additional_speed_tiers": [],
                "availability_nux": None,
                "upgrade": None,
            }
        ]
    }
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    path = _os.path.join(root, "codex-deepseek-catalog.json")
    with open(path, "w") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    return path


def _check_onboarding():
    """Check what onboarding items are still missing.
    Returns (catalog_ok: bool, missing_root: set, missing_custom: set)."""
    import os as _os

    catalog_path = _os.path.expanduser("~/.codex/model-catalogs/deepseek.json")
    catalog_ok = _os.path.isfile(catalog_path)

    config_path = _os.path.expanduser("~/.codex/config.toml")
    required_root = {
        "model_catalog_json",
        "model_context_window",
        "model_supports_reasoning_summaries",
        "model_reasoning_summary",
    }
    required_custom = {"stream_idle_timeout_ms"}
    found_root = set()
    found_custom = set()
    section = ""

    try:
        with open(config_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped[1:-1].strip()
                    continue
                if "=" in stripped:
                    key = stripped.split("=")[0].strip()
                    if section == "" and key in required_root:
                        found_root.add(key)
                    elif section == "model_providers.custom" and key in required_custom:
                        found_custom.add(key)
    except (FileNotFoundError, PermissionError):
        return (catalog_ok, required_root, required_custom)

    return (catalog_ok, required_root - found_root, required_custom - found_custom)


def run():
    log.info("")
    log.ok("codex-deepseek started")
    log.info(f"http://127.0.0.1:{PORT}/responses")
    log.info(
        f"model: {MODEL}  is_deepseek: {'true' if IS_DEEPSEEK else 'false'}  multimodal: {'on' if MULTIMODAL else 'off'}"
    )
    # GPT 透传状态
    if os.path.exists(GPT_AUTH_FILE) or os.path.exists(GPT_AUTH_FILE_FALLBACK):
        log.ok(
            "GPT passthrough: ChatGPT Codex backend via device-code OAuth "
            f"({GPT_CHATGPT_BACKEND_BASE}/responses)"
        )
        if GPT_ENABLE_APP_SERVER_FALLBACK:
            started = start_app_server_if_needed()
            if started:
                log.ok("app-server fallback pre-started (ws://127.0.0.1:11437)")
            else:
                log.ok("app-server fallback already running")
    elif OPENAI_API_KEY:
        log.ok(f"GPT passthrough: OpenAI Platform API key (len={len(OPENAI_API_KEY)})")
    else:
        log.info("GPT passthrough: disabled (no API key or OAuth token)")
    if not DEEPSEEK_API_KEY:
        log.warn("api_key not set")
    catalog_ok, missing_root, missing_custom = _check_onboarding()
    if not catalog_ok or missing_root or missing_custom:
        log.info("")
        log.header("Missing configuration detected")
    if not catalog_ok:
        _write_catalog_json()
        log.info(
            f'Model metadata not found: Codex does not recognize "{MODEL}" by default.'
        )
        log.info(f'A catalog file for "{MODEL}" has been written to:')
        log.info(
            f"  {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/codex-deepseek-catalog.json"
        )
        log.info("To fix:")
        log.info("  1) mkdir -p ~/.codex/model-catalogs")
        log.info(
            "  2) cp codex-deepseek-catalog.json ~/.codex/model-catalogs/deepseek.json"
        )
        log.info("  3) Add to ~/.codex/config.toml (root level, not inside provider):")
        log.info('       model_catalog_json = "~/.codex/model-catalogs/deepseek.json"')
        log.info("")
    if "model_catalog_json" in missing_root and catalog_ok:
        log.info("Model catalog file exists, but config.toml is not updated.")
        log.info("  Add to ~/.codex/config.toml (root level):")
        log.info('    model_catalog_json = "~/.codex/model-catalogs/deepseek.json"')
        log.info("")
    root_items = []
    if "model_context_window" in missing_root:
        root_items.append("model_context_window = 262144")
    if "model_supports_reasoning_summaries" in missing_root:
        root_items.append("model_supports_reasoning_summaries = true")
    if "model_reasoning_summary" in missing_root:
        root_items.append('model_reasoning_summary = "none"')
    if root_items:
        log.info("Missing performance / reasoning settings in ~/.codex/config.toml:")
        for item in root_items:
            log.info("  " + item)
        log.info("")
    if missing_custom:
        log.info("Missing provider-level setting in ~/.codex/config.toml:")
        log.info("  [model_providers.custom]")
        log.info(
            "  stream_idle_timeout_ms = 1800000  # prevent disconnect during long thinking"
        )
        log.info("")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
