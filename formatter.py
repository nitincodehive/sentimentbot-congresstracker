"""Message formatting for Telegram (HTML parse mode)."""

from datetime import datetime

from config import MAX_LISTED_TRANSACTIONS
from telegram_notify import esc

TYPE_ICON = {
    "purchase": "\U0001F7E2",        # green circle
    "sale": "\U0001F534",            # red circle
    "sale (partial)": "\U0001F7E0",  # orange circle
    "exchange": "\U0001F501",        # arrows
}


def parse_date(value: str):
    """Accept the several date shapes the Clerk publishes. None if unparseable."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def lag_days(transaction_date: str, filing_date: str):
    """Days between the transaction and the filing. '' if not derivable."""
    tx, fd = parse_date(transaction_date), parse_date(filing_date)
    if tx and fd:
        return (fd - tx).days
    return ""


def format_transaction(tx: dict) -> str:
    icon = TYPE_ICON.get(tx.get("transaction_type", ""), "\u2022")
    label = tx.get("ticker") or tx.get("asset_description", "")[:48]
    bits = [
        f"{icon} <b>{esc(label)}</b>",
        esc(tx.get("transaction_type", "")),
        esc(tx.get("amount_range", "")),
    ]
    line = " · ".join(b for b in bits if b.strip())

    detail = [f"tx {esc(tx.get('transaction_date', ''))}"]
    if tx.get("owner") and tx["owner"] != "self":
        detail.append(esc(tx["owner"]))
    if tx.get("lag_days") != "":
        detail.append(f"lag {tx['lag_days']}d")
    return f"{line}\n    <i>{' · '.join(detail)}</i>"


def _listed(transactions: list[dict]) -> list[str]:
    """Format up to MAX_LISTED_TRANSACTIONS lines, counting the remainder."""
    lines = [format_transaction(t) for t in transactions[:MAX_LISTED_TRANSACTIONS]]
    extra = len(transactions) - MAX_LISTED_TRANSACTIONS
    if extra > 0:
        lines.append(f"<i>… and {extra} more in the sheet.</i>")
    return lines


def format_filing_message(filing, transactions: list[dict], parse_ok: bool,
                          universe_available: bool, is_priority: bool) -> str:
    """Build the per-filing message.

    In-universe transactions are listed in full (up to
    MAX_LISTED_TRANSACTIONS); the remainder is compressed to a counted line.
    """
    header = "\u2b50 " if is_priority else "\U0001F3DB "
    lines = [
        f"{header}<b>{esc(filing.member)}</b>"
        + (f" ({esc(filing.detail)})" if filing.detail else ""),
        f"{esc(filing.label)} filed {esc(filing.filing_date)} · doc <code>{esc(filing.doc_id)}</code>",
        f'<a href="{esc(filing.url)}">{esc(filing.link_text)}</a>',
    ]
    if filing.link_note:
        lines.append(f"<i>{esc(filing.link_note)}</i>")
    lines.append("")

    if not parse_ok:
        reason = (
            "paper filing — no text layer"
            if filing.is_paper
            else "no transactions could be extracted"
        )
        lines += [
            f"\u26a0\ufe0f <b>Parse failed</b> ({reason}).",
            "This filing is recorded but not itemised — open the link above.",
        ]
        return "\n".join(lines)

    in_univ = [t for t in transactions if t.get("in_universe") == "yes"]
    others = [t for t in transactions if t.get("in_universe") != "yes"]

    if not universe_available:
        # No universe to filter against — show everything rather than hide it.
        lines.append(f"<b>{len(transactions)} transaction(s)</b>")
        lines += _listed(transactions)
        return "\n".join(lines)

    if in_univ:
        lines.append(f"<b>In Russell 3000 ({len(in_univ)})</b>")
        lines += _listed(in_univ)
    else:
        lines.append("<i>No transactions in the Russell 3000 universe.</i>")

    if others:
        lines.append("")
        lines.append(
            f"<i>+ {len(others)} other transaction(s) outside the universe "
            f"(funds, bonds, unlisted) — see the sheet or the filing.</i>"
        )
    return "\n".join(lines)


def format_summary(stats: dict) -> str:
    total = stats["parse_ok"] + stats["parse_failed"]
    rate = (stats["parse_failed"] / total * 100) if total else 0.0
    by_source = " · ".join(
        f"{name} {count}" for name, count in stats.get("new_by_source", {}).items()
    )
    lines = [
        "\U0001F4CA <b>Congress PTR tracker — run summary</b>",
        f"New filings detected: {stats['new_filings']}"
        + (f" ({by_source})" if by_source else ""),
        f"Processed this run: {total}",
        f"Parsed OK: {stats['parse_ok']} · failed: {stats['parse_failed']}",
        f"<b>Parse failure rate: {rate:.1f}%</b>",
    ]
    if stats.get("backlog"):
        lines.append(f"Backlog carried to next run: {stats['backlog']}")
    if stats.get("notify_failed"):
        lines.append(f"\u26a0\ufe0f Notifications failed: {stats['notify_failed']}")
    for name in stats.get("unavailable", []):
        lines.append(f"\u26a0\ufe0f {esc(name)} index unavailable this run; skipped.")
    if not stats.get("universe_available"):
        lines.append("\u26a0\ufe0f Russell 3000 universe unavailable this run.")
    return "\n".join(lines)
