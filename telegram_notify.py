"""
Telegram output. Outbound only.

This module calls sendMessage and nothing else — it never registers a
webhook, never calls getUpdates, and never processes commands.
"""

import html
import logging
import os
import time

import requests

from config import MAX_MESSAGE_CHARS

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def get_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars are required."
        )
    return token, chat_id


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so no chunk exceeds `limit` characters.

    A single line longer than the limit is hard-split as a last resort.
    """
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def send(text: str, dry_run: bool = False) -> bool:
    """Send one logical message, split as needed. Returns True if all parts sent.

    Failures are logged and reported, never raised — a Telegram outage must
    not crash the run or lose the Sheets write.
    """
    parts = split_message(text)

    if dry_run:
        for i, part in enumerate(parts, 1):
            print(f"\n--- [DRY RUN] telegram message {i}/{len(parts)} "
                  f"({len(part)} chars) ---")
            print(part)
        return True

    try:
        token, chat_id = get_credentials()
    except RuntimeError as exc:
        logger.error("Telegram not configured: %s", exc)
        return False

    ok = True
    for i, part in enumerate(parts, 1):
        try:
            resp = requests.post(
                API.format(token=token),
                json={
                    "chat_id": chat_id,
                    "text": part,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error("Telegram part %d/%d failed: %s %s",
                             i, len(parts), resp.status_code, resp.text[:300])
                ok = False
            if i < len(parts):
                time.sleep(0.5)  # stay under Telegram's rate limit
        except Exception as exc:
            logger.error("Telegram part %d/%d error: %s", i, len(parts), exc)
            ok = False
    return ok


def esc(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(str(text or ""), quote=False)
