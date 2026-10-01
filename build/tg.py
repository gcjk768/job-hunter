"""The one Telegram send path for the job watcher (nas_agent.py) and the PC watchdog (watchdog.py).

Messages are Telegram HTML in James's card style: a `head()` line, then one block per item separated by
blank lines. Callers escape every dynamic value with `esc()` (LLM output included) before wrapping it in
tags. `telegram()` splits long text only between blocks, and resends a part as plain text if Telegram
rejects its HTML, so an alert is never lost. Stdlib only: the watchdog runs on the PC without the
watcher's dependencies.
"""
from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request

LIMIT = 3900  # Telegram caps a message at 4096 characters; leave room
DIVIDER = "━━━━━━━━━━━━━━━━"
# One fixed emoji + title per message type, so every message of a kind looks the same.
SECTION_TITLES = {
    "fit": ("🆕", "NEW FIT"),
    "urgent": ("🔥", "APPLY TODAY"),
    "notfit": ("❌", "NOT A FIT"),
    "online": ("🟢", "JOB WATCHER"),
    "status": ("🟢", "STATUS"),
    "failing": ("⚠️", "JUDGE FAILING"),
    "error": ("⚠️", "ERROR"),
    "working": ("⏳", "WORKING"),
    "login": ("🔒", "LOGIN WALL"),
    "ask": ("🤔", "ASK"),
    "help": ("📖", "COMMANDS"),
    "usage": ("📖", "USAGE"),
    "outcome": ("✅", "RECORDED"),
    "pipeline": ("🗂", "PIPELINE"),
    "prep": ("📚", "PREP PACK"),
    "followup": ("⏰", "FOLLOW UP"),
    "digest": ("📊", "WEEKLY DIGEST"),
    "selfcheck": ("🩺", "SELF-CHECK"),
    "heal": ("🩹", "SELF-HEAL"),
    "restored": ("🩹", "STATE RESTORED"),
    "key": ("🔑", "CLAUDE LOGIN"),
    "restart": ("🔁", "RESTARTING"),
    "down": ("🔴", "JOB WATCHER DOWN"),
    "back": ("🟢", "JOB WATCHER BACK"),
}


def esc(value) -> str:
    """Escape text for Telegram HTML (& < >). Quotes stay readable; use href() inside attributes."""
    return html.escape(str(value), quote=False)


def link(url: str, label: str) -> str:
    """A tappable label instead of a raw URL; just the label when there is no URL."""
    return f'<a href="{html.escape(url)}">{esc(label)}</a>' if url else esc(label)


def head(kind: str, subtitle: str = "") -> str:
    emoji, title = SECTION_TITLES[kind]
    return f"{emoji} <b>{title}</b>" + (f" · {esc(subtitle)}" if subtitle else "")


def quote(title: str, body: str) -> str:
    """Background detail as an expandable quote. `body` is already HTML; blank lines are dropped so
    chunks() never splits the quote."""
    return f"<blockquote expandable>{title}\n" + re.sub(r"\n\s*\n", "\n", body.strip()) + "</blockquote>"


def plain(text: str) -> str:
    """Telegram HTML to plain text, keeping link targets: for the parse-error fallback, logs and prompts."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split between blocks (blank lines) so no tag is cut. A block over the limit falls back to line
    boundaries, a single over-long line to a hard cut.
    ponytail: those two fallbacks can cut a multi-line tag; telegram() then resends that part as plain text."""
    out, cur = [], ""
    for block in text.split("\n\n"):
        for piece in ([block] if len(block) <= limit else _by_line(block, limit)):
            if cur and len(cur) + 2 + len(piece) > limit:
                out, cur = out + [cur], ""
            cur = f"{cur}\n\n{piece}" if cur else piece
    return out + ([cur] if cur else [])


def _by_line(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out, cur, line = out + ([cur] if cur else []) + [line[:limit]], "", line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out, cur = out + [cur], ""
        cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])


def telegram(text: str, thread: str | int | None = None) -> None:
    """Send `text` (Telegram HTML) to TELEGRAM_CHAT_ID, in `thread` or else TELEGRAM_THREAD_ID."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[TG] not configured:\n" + plain(text))
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    thread = thread or os.environ.get("TELEGRAM_THREAD_ID")
    for part in chunks(text):
        fields = {"chat_id": chat, "text": part, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if thread:  # a forum topic, e.g. t.me/c/<chat>/<topic>
            fields["message_thread_id"] = str(thread)
        try:
            urllib.request.urlopen(url, data=urllib.parse.urlencode(fields).encode(), timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or "can't parse entities" not in exc.read().decode("utf-8", "replace"):
                raise
            del fields["parse_mode"]
            fields["text"] = plain(part)
            urllib.request.urlopen(url, data=urllib.parse.urlencode(fields).encode(), timeout=30)
