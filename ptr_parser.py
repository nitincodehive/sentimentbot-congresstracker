"""
Periodic Transaction Report PDF parsing, via pdfplumber.

The e-filed PTR renders each transaction as a text line of the form:

    [SP|DC|JT] <asset description> <type> <tx date> <notification date> <amount>

with the asset description and occasionally the amount wrapping onto the
following line(s), and small-caps label glyphs ("Filing Status:", "Subholding
Of:", "Description:") decoding to NUL characters. Table extraction merges
cells unreliably on these documents, so we parse the text layer by line.

Legacy paper filings (short DocIDs, e.g. 9116142) are image-only scans with
no text layer. They yield zero rows — by design the caller still records and
still notifies them, with the PDF link, rather than dropping them.
"""

import logging
import re

import pdfplumber

logger = logging.getLogger(__name__)

# One transaction line. Asset is non-greedy so the trailing fixed-shape
# fields (type, two dates, amount) anchor the match.
TX_RE = re.compile(
    r"^(?:(?P<owner>SP|DC|JT)\s+)?"
    r"(?P<asset>.*?)\s+"
    r"(?P<type>P|S \(partial\)|S|E)\s+"
    r"(?P<txdate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+(?:\s*-\s*(?:\$[\d,]+)?)?)\s*$"
)

# Lines that are metadata or repeated page furniture, never asset continuations.
META_RE = re.compile(
    r"^(F\x00+ S\x00+:|S\x00+ O\x00+:|D\x00+:|ID Owner Asset|Type Date"
    r"|\$200\?|\* For the complete|Filing ID #|Name:|Status:|State/District:"
    r"|Clerk of the House|Digitally Signed|Yes No)"
)

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")

OWNER_MAP = {"SP": "spouse", "DC": "dependent", "JT": "joint"}

TYPE_MAP = {
    "P": "purchase",
    "S": "sale",
    "S (partial)": "sale (partial)",
    "E": "exchange",
}


def _clean(text: str) -> str:
    """Strip small-caps NUL artefacts and collapse whitespace."""
    return re.sub(r"\s+", " ", text.replace("\x00", "")).strip()


def _extract_lines(pdf_bytes_or_path) -> list[str]:
    lines: list[str] = []
    with pdfplumber.open(pdf_bytes_or_path) as pdf:
        for page in pdf.pages:
            try:
                lines.extend((page.extract_text() or "").splitlines())
            except Exception as exc:  # one bad page must not lose the rest
                logger.warning("Page extraction failed: %s", exc)
    return lines


def parse_ptr(pdf_bytes_or_path) -> list[dict]:
    """Parse a PTR PDF into transaction dicts.

    Returns [] when the document has no text layer or nothing matches.
    Never raises for content reasons — callers treat [] as a parse failure
    but still record and still notify the filing.
    """
    lines = _extract_lines(pdf_bytes_or_path)
    rows: list[dict] = []

    for i, raw_line in enumerate(lines):
        match = TX_RE.match(raw_line.strip())
        if not match:
            continue

        asset = match.group("asset")
        amount = match.group("amount")

        # Absorb continuation lines: wrapped asset text, and a wrapped
        # amount whose upper bound spilled to the next line.
        for nxt in lines[i + 1 : i + 3]:
            stripped = nxt.strip()
            if not stripped or META_RE.match(stripped) or TX_RE.match(stripped):
                break
            parts = stripped.split()
            if amount.rstrip().endswith("-") and re.fullmatch(r"\$[\d,]+", parts[-1]):
                amount = f"{amount} {parts[-1]}"
                asset = f"{asset} {' '.join(parts[:-1])}"
            else:
                asset = f"{asset} {stripped}"

        asset = _clean(asset)
        if not asset:
            continue

        ticker_match = TICKER_RE.search(asset)
        tx_type = _clean(match.group("type"))

        rows.append(
            {
                "ticker": ticker_match.group(1) if ticker_match else "",
                "asset": asset,
                "transaction_type": TYPE_MAP.get(tx_type, tx_type),
                "owner": OWNER_MAP.get(match.group("owner"), "self"),
                "amount": _clean(amount),
                "transaction_date": match.group("txdate"),
                "notification_date": match.group("notif"),
            }
        )

    logger.info("Parsed %d transaction rows", len(rows))
    return rows
