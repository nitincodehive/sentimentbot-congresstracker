"""
Ticker universe: the largest US-listed stocks by market cap.

Source is the Nasdaq screener API, which despite the hostname covers NYSE
and NYSE American alongside Nasdaq. Fetched at runtime, never committed.

This replaces an earlier iShares IWV (Russell 3000) implementation: iShares
now serves its product page, with a `text/csv` content-type and a 200 status,
instead of the holdings CSV for scripted clients. Neither the status code nor
the header catches that - only inspecting the body does, which is what
_reject_html exists for.

The universe here decides one thing only: whether a transaction is shown in
full in the Telegram message or folded into the compressed "other" count. It
is presentation, not a trading decision, so a failure degrades to "show
everything in full" rather than stopping the run.

Deliberately more inclusive than a strict index reconstruction: there is no
country filter, because members trade ADRs and foreign issuers and burying
those in the compressed bucket would hide transactions worth reading. Only
non-equity lines (baby bonds, warrants, preferreds, rights) are excluded, and
only so they cannot displace real companies out of the market-cap ranking.
"""

import logging
import re

import requests

from config import UNIVERSE_TARGET_SIZE

logger = logging.getLogger(__name__)

NASDAQ_SCREENER_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=10000&download=true"
)

# The screener rejects non-browser clients, so this request does not use the
# project's own descriptive User-Agent.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _reject_html(text: str) -> None:
    """Raise if the body is an HTML document rather than data."""
    if text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raise ValueError(
            "Nasdaq returned an HTML document, not JSON "
            "(the endpoint is blocking this client)"
        )


# Securities that are not shares in a company. The screener returns baby
# bonds ("AT&T Inc. 5.350% Global Notes due 2066"), warrants, preferreds and
# rights alongside ordinary stock, distinguished only in the security name.
# They are excluded because each one otherwise displaces a real company out
# of the market-cap ranking - leaving them in pushed the cutoff from ~$300M
# to ~$780M and dropped genuine small-caps out of the universe.
#
# ADRs are deliberately NOT excluded: they are real operating companies and
# members trade them.
_NON_EQUITY = re.compile(
    r"warrant|preferred|debenture|notes\s+due|\brights?\b|\bunits?\b",
    re.IGNORECASE,
)


def _is_equity(row: dict) -> bool:
    return not _NON_EQUITY.search(str(row.get("name", "")))


def _market_cap(row: dict) -> float:
    try:
        return float(row.get("marketCap") or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(symbol: str) -> str:
    """Nasdaq writes share classes with slashes (BRK/B); House PTRs use dots."""
    return str(symbol or "").strip().upper().replace("-", ".").replace("/", ".")


def load_universe() -> set[str]:
    """Return the top UNIVERSE_TARGET_SIZE tickers by market cap.

    Returns an empty set on any failure; callers treat that as "no universe"
    and show every transaction in full.
    """
    try:
        resp = requests.get(
            NASDAQ_SCREENER_URL, headers={"User-Agent": BROWSER_UA}, timeout=60
        )
        resp.raise_for_status()
        _reject_html(resp.text)

        rows = resp.json()["data"]["rows"]
        ranked = sorted(
            (r for r in rows if _market_cap(r) > 0 and _is_equity(r)),
            key=_market_cap,
            reverse=True,
        )[:UNIVERSE_TARGET_SIZE]

        tickers = {
            t for t in (_normalize(r.get("symbol")) for r in ranked)
            if t and t not in {".", ".."} and any(c.isalpha() for c in t)
        }

        if ranked:
            logger.info(
                "Universe: %d tickers from Nasdaq screener (%d listings "
                "considered, market-cap cutoff $%.0fM)",
                len(tickers), len(rows), _market_cap(ranked[-1]) / 1e6,
            )
        return tickers

    except Exception as exc:
        logger.error(
            "Failed to load ticker universe (%s); every transaction will be "
            "shown in full this run", exc
        )
        return set()
