"""
Minimal synchronous WebSocket client using only Python stdlib.
Used by the proxy to communicate with codex app-server for GPT model relay.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import time
from typing import Optional

OP_TEXT = 0x1
OP_CLOSE = 0x8


class WebSocketClient:
    """Minimal sync WebSocket client (RFC 6455), no extensions, no compression."""

    def __init__(self, url: str, timeout: float = 30.0):
        self._sock: Optional[socket.socket] = None
        self._timeout = timeout
        self._url = url
        self._buffer = b""

    def connect(self) -> None:
        """Open WebSocket connection with HTTP upgrade handshake."""
        from urllib.parse import urlparse

        parsed = urlparse(self._url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect((host, port))

        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())

        # Read handshake response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake failed: no response")
            response += chunk

        if b"101" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")

        self._sock = sock

    def send(self, message: str) -> None:
        """Send a text frame."""
        if not self._sock:
            raise ConnectionError("Not connected")
        data = message.encode("utf-8")
        self._send_frame(OP_TEXT, data)

    def recv(self, timeout: Optional[float] = None) -> str:
        """Receive a text frame, blocking until one arrives or timeout."""
        if not self._sock:
            raise ConnectionError("Not connected")

        deadline = time.monotonic() + (timeout or self._timeout)

        while True:
            # Check buffer first
            if len(self._buffer) >= 2:
                try:
                    return self._parse_frame()
                except NeedMoreData:
                    pass

            # Read more data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket recv timeout")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError("WebSocket connection closed")
                self._buffer += chunk
            except socket.timeout:
                raise TimeoutError("WebSocket recv timeout")

    def close(self) -> None:
        """Send close frame and close socket."""
        if self._sock:
            try:
                self._send_frame(OP_CLOSE, b"")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """Build and send a WebSocket frame (client → server, always masked)."""
        assert self._sock is not None
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        frame.extend(masked)
        self._sock.sendall(bytes(frame))

    def _parse_frame(self) -> str:
        """Parse a complete frame from buffer. Raises NeedMoreData if incomplete."""
        if len(self._buffer) < 2:
            raise NeedMoreData()

        byte1 = self._buffer[0]
        byte2 = self._buffer[1]
        # opcode = byte1 & 0x0F
        masked = (byte2 & 0x80) != 0
        payload_len = byte2 & 0x7F

        offset = 2
        if payload_len == 126:
            if len(self._buffer) < offset + 2:
                raise NeedMoreData()
            payload_len = struct.unpack(">H", self._buffer[offset:offset + 2])[0]
            offset += 2
        elif payload_len == 127:
            if len(self._buffer) < offset + 8:
                raise NeedMoreData()
            payload_len = struct.unpack(">Q", self._buffer[offset:offset + 8])[0]
            offset += 8

        if masked:
            if len(self._buffer) < offset + 4:
                raise NeedMoreData()
            mask_key = self._buffer[offset:offset + 4]
            offset += 4
        else:
            mask_key = None

        if len(self._buffer) < offset + payload_len:
            raise NeedMoreData()

        payload = self._buffer[offset:offset + payload_len]
        if mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        # Consume frame from buffer
        self._buffer = self._buffer[offset + payload_len:]

        return payload.decode("utf-8")


class NeedMoreData(Exception):
    """Internal: buffer incomplete, need more data from socket."""
    pass


# ── App-Server relay helpers ──

APP_SERVER_URL = "ws://127.0.0.1:11437"
APP_SERVER_PID_FILE = "/tmp/codex-app-server.pid"


def start_app_server_if_needed() -> bool:
    """Start codex app-server with GPT native config if not already running.
    Uses -c model=... flag so no config swap is needed.
    Returns True if started, False if already running.
    """
    # Check if already listening
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", 11437))
        s.close()
        return False  # Already running
    except (socket.error, OSError):
        pass
    finally:
        s.close()

    # Not running — start fresh
    _stop_app_server()  # Clean any stale PID
    return _do_start_app_server()


def _stop_app_server() -> None:
    """Kill existing app-server by PID file only (no pkill — too dangerous)."""
    if os.path.exists(APP_SERVER_PID_FILE):
        try:
            with open(APP_SERVER_PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 9)  # SIGKILL
        except (OSError, ValueError, ProcessLookupError):
            pass
        try:
            os.remove(APP_SERVER_PID_FILE)
        except OSError:
            pass
    time.sleep(1)


def _do_start_app_server() -> bool:
    """Start the app-server process with GPT model via -c flag (no config swap needed)."""
    import subprocess

    try:
        proc = subprocess.Popen(
            [
                "codex", "app-server", "--listen", "ws://127.0.0.1:11437",
                "-c", 'model="gpt-5.6-sol"',
                "-c", 'model_provider="openai"',
            ],
            stdout=open("/tmp/codex-app-server.log", "ab"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Write PID
        with open(APP_SERVER_PID_FILE, "w") as f:
            f.write(str(proc.pid))

        # Wait for startup
        time.sleep(5)

        return True
    except Exception as e:
        print(f"[ws_client] Failed to start app-server: {e}")
        return False


def relay_via_app_server(
    prompt: str,
    model: str = "gpt-5.6-sol",
    reasoning_effort: str = "xhigh",
    instructions: str = "",
    timeout: float = 120.0,
) -> tuple:
    """Relay a GPT request through the persistent app-server.
    
    Protocol:
      1. thread/start → get threadId
      2. turn/start with input → get streaming AgentMessageDelta events
      3. Collect deltas, detect turn/completed
    
    Returns:
      (success: bool, full_text: str, error_msg: str, metadata: dict)
    """
    ws = WebSocketClient(APP_SERVER_URL, timeout=30.0)
    try:
        ws.connect()

        # 1. Initialize
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "proxy-relay", "version": "1.0"},
                "capabilities": {"experimentalApi": True, "requestAttestation": True},
            },
            "id": 1,
        }))
        init_resp = _recv_with_timeout(ws, 10)
        ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))

        # 2. Start thread
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "thread/start",
            "params": {
                "model": model,
                "modelProvider": "openai",
                "cwd": os.getcwd(),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "developerInstructions": (
                    "You are being used as a GPT text relay behind a Responses API proxy. "
                    "Answer directly and do not run shell commands or modify files unless "
                    "the user explicitly asks for environment inspection."
                ),
            },
            "id": 2,
        }))
        thread_id = None
        while thread_id is None:
            msg = _recv_with_timeout(ws, 10)
            ev = json.loads(msg)
            if ev.get("id") == 2 and "result" in ev:
                thread_id = ev["result"]["thread"]["id"]

        # 3. Start turn
        ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "model": model,
                "effort": reasoning_effort,
            },
            "id": 3,
        }))

        # 4. Collect streaming response
        full_text = ""
        error_msg = ""
        metadata = {"thread_id": thread_id, "model": model}

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = _recv_with_timeout(ws, min(remaining, 30))
            ev = json.loads(msg)
            method = ev.get("method", "")
            ev_id = ev.get("id", "")

            if method == "item/agentMessage/delta":
                full_text += ev["params"]["delta"]
            elif method == "turn/completed":
                metadata["turn_id"] = ev["params"]["turn"]["id"]
                turn_error = ev["params"]["turn"].get("error")
                if turn_error:
                    # turn/completed 中包含错误信息（如 quota exhausted）
                    error_msg = str(turn_error.get("message", turn_error))
                break
            elif method == "error":
                params = ev.get("params", {})
                error_info = params.get("error", params)
                error_msg = error_info.get("message", str(ev)) if isinstance(error_info, dict) else str(error_info)
                # OpenAI quota 错误通常在这里
                break
            elif method == "item/completed":
                item = ev.get("params", {}).get("item", {})
                if item.get("type") == "agentMessage" and not full_text:
                    full_text = item.get("text", "")
            elif ev_id == 3 and "result" in ev:
                # turn/start 响应可能直接包含错误
                turn_err = ev["result"].get("turn", {}).get("error")
                if turn_err:
                    error_msg = str(turn_err.get("message", turn_err))
                    break

        if not full_text and not error_msg:
            error_msg = (
                "GPT request timed out after 180s — OpenAI API may be unavailable "
                "or usage quota exhausted. Check your ChatGPT Plus subscription status."
            )

        success = bool(full_text) and not error_msg
        return success, full_text, error_msg, metadata

    except TimeoutError:
        return False, "", (
            "GPT relay timed out (WebSocket connection to app-server lost). "
            "This usually means the OpenAI API is overloaded or quota exhausted. "
            "Retry later or check your usage limits at https://platform.openai.com/usage."
        ), {}
    except ConnectionError as e:
        return False, "", f"App-server connection error: {e}", {}
    except Exception as e:
        return False, "", f"GPT relay error: {e}", {}
    finally:
        ws.close()


def _recv_with_timeout(ws: WebSocketClient, timeout: float) -> str:
    """Receive a message with clean timeout handling."""
    return ws.recv(timeout=timeout)
