"""
Russell 3000 ticker universe, from the iShares IWV holdings CSV.

Fetched at runtime — never committed. Mirrors the approach swingscanner
uses (same URL and header-detection strategy), but this is a standalone
implementation; no code is shared or imported across projects.

Degrades gracefully: on any failure it returns an empty set, and callers
treat "no universe" as "show every transaction in full".
"""

import csv
import io
import logging

import requests

logger = logging.getLogger(__name__)

# iShares serves an HTML page instead of the CSV unless the User-Agent looks
# like a browser, so this request does not use the project's own UA string.
IWV_USER_AGENT = "Mozilla/5.0"

IWV_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239714/"
    "ishares-russell-3000-etf/1467271812596.ajax"
    "?tab=holdings&fileType=csv"
)


def load_universe() -> set[str]:
    """Return the Russell 3000 tickers as an uppercase set, or empty on failure."""
    try:
        resp = requests.get(
            IWV_HOLDINGS_URL, headers={"User-Agent": IWV_USER_AGENT}, timeout=45
        )
        resp.raise_for_status()

        # The CSV carries ~9 rows of fund metadata before the real header.
        lines = resp.text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            if "Ticker" in line and "Asset Class" in line:
                header_idx = i
                break
        if header_idx is None:
            logger.error("IWV CSV: header row not found; universe unavailable")
            return set()

        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        tickers: set[str] = set()
        for row in reader:
            if (row.get("Asset Class") or "").strip() != "Equity":
                continue
            ticker = (row.get("Ticker") or "").strip().upper()
            if not ticker or ticker in {".", ".."}:
                continue
            # iShares uses dashes for share classes; House PTRs use dots.
            ticker = ticker.replace("-", ".")
            if any(c.isalpha() for c in ticker) and all(
                c.isalnum() or c == "." for c in ticker
            ):
                tickers.add(ticker)

        logger.info("Loaded %d Russell 3000 tickers from iShares IWV", len(tickers))
        return tickers

    except Exception as exc:
        logger.error("Failed to load IWV universe (%s); treating as unavailable", exc)
        return set()
