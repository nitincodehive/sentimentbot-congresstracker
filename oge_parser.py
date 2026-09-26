"""
OGE Form 278-T PDF parsing, via pdfplumber.

Every 278-T goes through this parser; nothing here or upstream special-cases
a filer. Reports e-filed through Integrity.gov have a clean text layer and
parse completely. A report filed on paper and scanned (in 2026, the
President's — printed and scanned on an office copier) has either no text
layer or a noisy OCR one ("lourchaso 717/2026"), and fails here like a House
legacy scan does: the caller records it and sends the PDF link. If a future
filer's paper reports are scanned cleanly, or they e-file, they parse with no
code change.

The e-filed layout renders each transaction as a numbered text line:

    <#> <description> [See Endnote] <type> <date> <Yes|No> <amount>

with the description, and the upper bound of the amount range, wrapping onto
following lines:

    3 Arthur Ventures Growth IV, LP - Nucleus Security, See Endnote Purchase 04/08/2026 No $100,001 -
    Inc. $250,000

Rows are numbered 1..N, which gives a completeness check the House form
lacks: if the numbers that matched have gaps, some rows were unreadable, and
the whole report is treated as a parse failure rather than presented as a
partial list that looks complete.

The 278-T has no owner column (spouse/dependent holdings are described in
endnotes, if at all), so owner is always reported as "self".
"""

import logging
import re

import pdfplumber

logger = logging.getLogger(__name__)

# Type and date are optional because filers occasionally leave them blank
# (e.g. "48 Cigna Corp. (CI) Sale No $1,001 - $15,000"); the Yes/No column and
# the amount are what anchor a row. The amount must be cleanly formatted, so
# OCR noise like "$1 000 001 -$5 000 000" or "$500.001" never matches.
_MONEY = r"\$\d{1,3}(?:,\d{3})*"
ROW_RE = re.compile(
    r"^(?P<num>\d{1,5})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?:(?P<type>Purchase|Sale \(Partial\)|Sale|Exchange)\s+)?"
    r"(?:(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+)?"
    r"(?:Yes|No)\s+"
    rf"(?P<amount>{_MONEY}(?:\s*-\s*(?:{_MONEY})?)?|(?:Spouse/DC )?Over {_MONEY})\s*$",
    re.IGNORECASE,
)

# Repeated table header and page furniture; never part of a transaction.
META_RE = re.compile(
    r"^(# DESCRIPTION|RECEIVED OVER|30 DAYS AGO|Transactions$|.+ - Page \d+$)"
)

# Everything after these is endnotes or boilerplate, not transactions.
END_RE = re.compile(r"^(Endnotes|Summary of Contents)$")

TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.]{0,6})\)")
ENDNOTE_RE = re.compile(r"\s*See Endnote\s*", re.IGNORECASE)
WRAPPED_AMOUNT_RE = re.compile(_MONEY)

# Filers leave a type or date blank now and then, but a report where many
# rows lack them has been misread (a garbled "lourchase 7/9/2026" ends up in
# the description instead), and is treated as unparsed.
MIN_COMPLETE_ROW_SHARE = 0.9

TYPE_MAP = {
    "purchase": "purchase",
    "sale": "sale",
    "sale (partial)": "sale (partial)",
    "exchange": "exchange",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_lines(pdf_bytes_or_path) -> list[str]:
    lines: list[str] = []
    with pdfplumber.open(pdf_bytes_or_path) as pdf:
        for page in pdf.pages:
            try:
                lines.extend((page.extract_text() or "").splitlines())
            except Exception as exc:  # one bad page must not lose the rest
                logger.warning("Page extraction failed: %s", exc)
    return [line.strip() for line in lines]


def parse_278t(pdf_bytes_or_path) -> list[dict]:
    """Parse a 278-T PDF into transaction dicts.

    Returns [] when there is no text layer, nothing matches, or the matched
    rows are not a complete 1..N sequence. Never raises for content reasons.
    """
    lines = _extract_lines(pdf_bytes_or_path)
    rows: list[dict] = []
    numbers: list[int] = []
    section: list[str] = []  # transaction-section lines, for the tail check

    for i, line in enumerate(lines):
        if END_RE.match(line):
            break
        section.append(line)
        match = ROW_RE.match(line)
        if not match:
            continue

        desc, amount = match.group("desc"), match.group("amount")
        for nxt in lines[i + 1 : i + 4]:
            if not nxt or META_RE.match(nxt) or END_RE.match(nxt) or ROW_RE.match(nxt):
                break
            parts = nxt.split()
            if amount.endswith("-") and WRAPPED_AMOUNT_RE.fullmatch(parts[-1]):
                amount = f"{amount} {parts[-1]}"
                desc = f"{desc} {' '.join(parts[:-1])}"
            else:
                desc = f"{desc} {nxt}"

        desc = _clean(ENDNOTE_RE.sub(" ", desc))
        tickers = TICKER_RE.findall(desc)
        tx_type = (match.group("type") or "").lower()
        numbers.append(int(match.group("num")))
        rows.append(
            {
                "ticker": tickers[-1] if tickers else "",
                "asset": desc,
                "transaction_type": TYPE_MAP.get(tx_type, tx_type),
                "owner": "self",
                "amount": _clean(amount),
                "transaction_date": match.group("date") or "",
            }
        )

    if rows and sorted(numbers) != list(range(1, max(numbers) + 1)):
        logger.warning(
            "278-T incomplete: %d of %d numbered rows readable; treating as unparsed",
            len(set(numbers)), max(numbers),
        )
        return []
    # A gap-free 1..N can still be missing rows N+1 onwards.
    if rows and any(line.startswith(f"{max(numbers) + 1} ") for line in section):
        logger.warning(
            "278-T incomplete: row %d present but unreadable; treating as unparsed",
            max(numbers) + 1,
        )
        return []
    complete = sum(1 for r in rows if r["transaction_type"] and r["transaction_date"])
    if rows and complete < MIN_COMPLETE_ROW_SHARE * len(rows):
        logger.warning(
            "278-T unreliable: only %d of %d rows have a type and date; treating as unparsed",
            complete, len(rows),
        )
        return []

    logger.info("Parsed %d transaction rows", len(rows))
    return rows
