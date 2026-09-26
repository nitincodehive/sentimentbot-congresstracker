"""
Senate eFD PTR parsing.

An e-filed Senate PTR is an HTML page with one transactions table:

    # | Transaction Date | Owner | Ticker | Asset Name | Asset Type | Type | Amount | Comment

Cells are read by header name rather than position, so a reordered or added
column does not silently shift values. A ticker cell holds a Yahoo Finance
link, or "--" when the asset has no ticker (bonds, private funds).

A paper filing's page carries GIF page images and no table; it yields [] and,
as everywhere else, the caller still records and notifies it with the link.
"""

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

OWNER_MAP = {
    "self": "self",
    "spouse": "spouse",
    "joint": "joint",
    "child": "dependent",
    "dependent child": "dependent",
}

TYPE_MAP = {
    "purchase": "purchase",
    "sale (full)": "sale",
    "sale": "sale",
    "sale (partial)": "sale (partial)",
    "exchange": "exchange",
}


class _TableParser(HTMLParser):
    """Collects every table as a list of rows of cell text."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_senate_ptr(html_bytes: bytes) -> list[dict]:
    """Parse a Senate PTR page into transaction dicts. [] for paper filings
    or anything unrecognised; never raises for content reasons."""
    parser = _TableParser()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))

    for table in parser.tables:
        if not table:
            continue
        header = [h.lower() for h in table[0]]
        if "transaction date" not in header or "asset name" not in header:
            continue
        col = {name: header.index(name) for name in header}

        def cell(row, name):
            i = col.get(name)
            return row[i] if i is not None and i < len(row) else ""

        rows = []
        for row in table[1:]:
            ticker = cell(row, "ticker")
            tx_type = cell(row, "type").lower()
            owner = cell(row, "owner").lower()
            rows.append(
                {
                    "ticker": "" if ticker in ("", "--") else ticker,
                    "asset": cell(row, "asset name"),
                    "transaction_type": TYPE_MAP.get(tx_type, tx_type),
                    "owner": OWNER_MAP.get(owner, owner or "self"),
                    "amount": cell(row, "amount"),
                    "transaction_date": cell(row, "transaction date"),
                }
            )
        logger.info("Parsed %d transaction rows", len(rows))
        return rows

    return []
