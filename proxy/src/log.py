import os
import sys
from typing import Optional

_USE_COLOR = sys.stderr.isatty() and not os.getenv("NO_COLOR")
C = (
    {
        "reset": "\033[0m",
        "cyan": "\033[36m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "magenta": "\033[35m",
        "gray": "\033[90m",
        "bold": "\033[1m",
    }
    if _USE_COLOR
    else {key: "" for key in ("reset", "cyan", "green", "yellow", "red", "magenta", "gray", "bold")}
)


def info(msg: str, *args) -> None:
    print(f"{C['cyan']}[INFO]{C['reset']} {msg}", *args, file=sys.stderr)


def ok(msg: str, *args) -> None:
    print(f"{C['green']}[ OK ]{C['reset']} {msg}", *args, file=sys.stderr)


def warn(msg: str, *args) -> None:
    print(f"{C['yellow']}[WARN]{C['reset']} {msg}", *args, file=sys.stderr)


def err(msg: str, *args) -> None:
    print(f"{C['red']}[ERR ]{C['reset']} {msg}", *args, file=sys.stderr)


def req(msg: str, *args) -> None:
    print(f"\n{C['magenta']}[REQ ]{C['reset']} {msg}", *args, file=sys.stderr)


def resp(msg: str, *args) -> None:
    print(f"{C['green']}[RESP]{C['reset']} {msg}", *args, file=sys.stderr)


def skip(msg: str, *args) -> None:
    print(f"{C['gray']}[SKIP]{C['reset']} {msg}", *args, file=sys.stderr)


def toks(
    prompt: Optional[int] = None,
    completion: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    parts = []
    if prompt is not None:
        parts.append(f"in:{prompt}")
    if completion is not None:
        parts.append(f"out:{completion}")
    if total is not None:
        parts.append(f"total:{total}")
    print(f"{C['gray']}[TOKS]{C['reset']} {' '.join(parts)}", file=sys.stderr)


def timing(ms: int) -> None:
    sec = ms / 1000
    if sec >= 1:
        label = f"{sec:.1f}s"
    else:
        label = f"{ms}ms"
    print(f"{C['gray']}[TIME]{C['reset']} {label}", file=sys.stderr)


def ttfb(ms: int, model: str = "", stage: str = "") -> None:
    """记录 TTFB（Time To First Byte），用于区分网关延迟 vs 推理延迟。

    stage 可以是 "connect"（TCP/HTTP 连接+TLS+response headers）或
    "first_token"（首个 SSE 数据块到达），默认为 "connect"。
    """
    sec = ms / 1000
    label = f"{sec:.2f}s"
    stage_str = f" stage={stage}" if stage else ""
    model_str = f" model={model}" if model else ""
    print(
        f"{C['cyan']}[TTFB]{C['reset']} {label}{stage_str}{model_str}",
        file=sys.stderr,
    )


def header(msg: str) -> None:
    print(f"\n{C['bold']}{C['cyan']}=== {msg} ==={C['reset']}", file=sys.stderr)
