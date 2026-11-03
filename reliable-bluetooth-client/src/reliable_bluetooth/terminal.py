from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from datetime import datetime


_RESET = "\033[0m"
_BOLD = "\033[1m"
_COLORS = {
    "accent": "\033[36m",
    "success": "\033[32m",
    "warning": "\033[33m",
    "error": "\033[31m",
    "receive": "\033[35m",
    "muted": "\033[90m",
}
_LABELS = {
    "info": "INFO",
    "success": " OK ",
    "warning": "WAIT",
    "error": "FAIL",
    "receive": "RECV",
}


def _color_enabled() -> bool:
    return (
        os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM", "").lower() != "dumb"
        and sys.stdout.isatty()
    )


def style(text: str, color: str | None = None, *, bold: bool = False) -> str:
    if not _color_enabled():
        return text
    prefix = (_BOLD if bold else "") + (_COLORS.get(color or "", ""))
    return f"{prefix}{text}{_RESET}" if prefix else text


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def status(kind: str, message: str, *, leading_newline: bool = False) -> None:
    label = _LABELS.get(kind, kind.upper()[:4].ljust(4))
    prefix = style(f"[{label}]", kind if kind in _COLORS else "accent", bold=True)
    clock = style(f"[{timestamp()}]", "muted")
    if leading_newline:
        print()
    print(f"{clock} {prefix} {message}", flush=True)


def panel(title: str, rows: Iterable[tuple[str, str]]) -> None:
    values = [(str(key), str(value)) for key, value in rows]
    width = max(
        52,
        len(title) + 4,
        *(len(key) + len(value) + 5 for key, value in values),
    )
    border = "+" + "-" * (width - 2) + "+"
    print(style(border, "accent", bold=True))
    print(
        style("| ", "accent", bold=True)
        + style(title.ljust(width - 4), bold=True)
        + style(" |", "accent", bold=True)
    )
    print(style("+" + "-" * (width - 2) + "+", "accent"))
    for key, value in values:
        content = f"{key}: {value}"
        print(
            style("| ", "accent")
            + content.ljust(width - 4)
            + style(" |", "accent")
        )
    print(style(border, "accent", bold=True), flush=True)


def progress(label: str, current: int, total: int, *, width: int = 22) -> None:
    percent = 100 if total == 0 else min(100, current * 100 // total)
    filled = width if total == 0 else min(width, current * width // total)
    bar = "#" * filled + "-" * (width - filled)
    clock = style(f"[{timestamp()}]", "muted")
    print(
        f"{clock} {style('[FILE]', 'accent', bold=True)} {label} "
        f"[{bar}] {percent:3d}%  {current}/{total} bytes",
        flush=True,
    )


def prompt(text: str = "> ") -> str:
    return style(text, "accent", bold=True)
