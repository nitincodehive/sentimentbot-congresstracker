"""
House PTR tracker - entry point.

Once per weekday: pull the House Clerk year-to-date index, find newly filed
Periodic Transaction Reports, parse their PDFs, store them in Google Sheets,
and send one Telegram message per new filing.

Modes
  (default)      incremental run; notifies on filings not seen before
  --seed         first-ever run; records the full year to date and sends
                 NOTHING to Telegram. Refuses to run if the sheet is
                 already populated (see --force-reseed).
  --dry-run      prints to console; writes to neither Sheets nor Telegram

Graceful degradation: any single filing that fails is logged, recorded and
skipped. The only fatal condition is an unreachable index.
"""

import argparse
import io
import logging
import sys
import time
from datetime import datetime

import requests

import config
import sheets_db
from formatter import format_filing_message, format_summary, lag_days, parse_date
from house_index import IndexUnavailable, fetch_filings
from ptr_parser import parse_ptr
from telegram_notify import send as telegram_send
from universe import load_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("congresstracker")


# -- Helpers --------------------------------------------------------------


def is_priority(filing) -> bool:
    names = {n.strip().lower() for n in config.PRIORITY_MEMBERS if n.strip()}
    return filing.last.strip().lower() in names


def sort_key(filing, record=None):
    """Queue order: notifiable first, then priority members, then newest.

    Notifiability outranks priority deliberately. A seeded filing can never
    alert, so processing one is pure backfill; letting an old backfill item
    outrank a genuinely new filing just because its member is on the priority
    list would delay a real alert past the per-run cap. Priority still decides
    the order among filings that can actually alert.
    """
    date = parse_date(filing.filing_date)
    backfill_only = bool(record) and record.get("notify_status") == "seeded"
    return (
        1 if backfill_only else 0,
        0 if is_priority(filing) else 1,
        -(date.toordinal() if date else 0),
    )


def download_pdf(url: str):
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.PDF_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.error("PDF download failed (%s): %s", url, exc)
        return None


def build_transaction_rows(filing, parsed, universe):
    rows = []
    for tx in parsed:
        ticker = (tx.get("ticker") or "").upper()
        rows.append(
            {
                "doc_id": filing.doc_id,
                "member": filing.member,
                "ticker": ticker,
                "asset_description": tx.get("asset", ""),
                "transaction_type": tx.get("transaction_type", ""),
                "owner": tx.get("owner", "self"),
                "amount_range": tx.get("amount", ""),
                "transaction_date": tx.get("transaction_date", ""),
                "filing_date": filing.filing_date,
                "lag_days": lag_days(tx.get("transaction_date", ""), filing.filing_date),
                "in_universe": "yes" if (ticker and ticker in universe) else "no",
            }
        )
    return rows


# -- Main -----------------------------------------------------------------


def run(args) -> int:
    year = args.year or datetime.now().year

    # 1. Index - the only fatal dependency.
    try:
        filings = fetch_filings(year)
    except IndexUnavailable as exc:
        logger.error("ABORTING: %s", exc)
        return 1

    by_doc_id = {f.doc_id: f for f in filings}
    logger.info("Index has %d PTR filings for %d", len(by_doc_id), year)

    # 2. Sheets state.
    known = {}
    filings_ws = transactions_ws = None
    if not args.dry_run:
        try:
            spreadsheet = sheets_db.init_sheets()
            filings_ws, transactions_ws = sheets_db.ensure_tabs(spreadsheet)
            known = sheets_db.load_filings(filings_ws)
        except Exception as exc:
            logger.error("ABORTING: Sheets unavailable: %s", exc)
            return 1
    else:
        logger.info("[DRY RUN] Skipping Sheets; treating every filing as new.")

    already_seeded = bool(known)

    # 3. Seeding guards - explicit, and safe in both directions.
    if args.seed:
        if already_seeded and not args.force_reseed:
            logger.error(
                "ABORTING: the filings tab already holds %d rows, so seeding has "
                "already happened. Re-seeding would duplicate rows. Run without "
                "--seed for a normal incremental run, or pass --force-reseed if "
                "you have deliberately cleared the sheet.",
                len(known),
            )
            return 1
        logger.info(
            "SEED MODE: recording the full year to date. "
            "No Telegram messages will be sent."
        )
    elif not already_seeded and not args.dry_run:
        logger.error(
            "ABORTING: the filings tab is empty, so this repository has never "
            "been seeded. A normal run would notify on all %d filings at once. "
            "Run once with --seed first.",
            len(by_doc_id),
        )
        return 1

    # 4. Reconcile on DocID, the dedup key.
    new_filings = [f for doc_id, f in by_doc_id.items() if doc_id not in known]
    logger.info("New filings since last run: %d", len(new_filings))

    if not args.dry_run and new_filings:
        try:
            sheets_db.append_filings(
                filings_ws,
                [
                    {
                        "doc_id": f.doc_id,
                        "member": f.member,
                        "filing_date": f.filing_date,
                        "pdf_url": f.pdf_url,
                        "parse_status": "pending",
                        # Seeded filings are permanently excluded from alerts.
                        "notify_status": "seeded" if args.seed else "pending",
                        "transaction_count": "",
                        "notes": "seeded baseline" if args.seed else "",
                    }
                    for f in new_filings
                ],
            )
            known = sheets_db.load_filings(filings_ws)
        except Exception as exc:
            logger.error("ABORTING: could not record new filings: %s", exc)
            return 1

    # 5. Work queue: anything not yet parsed, plus anything parsed but not
    #    yet notified. Priority members first, then newest.
    if args.dry_run:
        queue = sorted(new_filings, key=lambda f: sort_key(f, None))
    else:
        queue = [
            f
            for f, _ in sorted(
                (
                    (by_doc_id[doc_id], rec)
                    for doc_id, rec in known.items()
                    if doc_id in by_doc_id
                    and (
                        rec.get("parse_status", "") in ("", "pending")
                        or rec.get("notify_status", "") in ("", "pending", "failed")
                    )
                ),
                key=lambda pair: sort_key(pair[0], pair[1]),
            )
        ]

    cap = args.limit or (
        config.MAX_PDFS_PER_SEED_RUN if args.seed else config.MAX_PDFS_PER_RUN
    )
    backlog = max(0, len(queue) - cap)
    queue = queue[:cap]
    logger.info(
        "Processing %d filing(s) this run (%d left for next run)", len(queue), backlog
    )

    # 6. Universe (optional; degrades to "show everything").
    universe = load_universe()
    universe_available = bool(universe)

    stats = {
        "new_filings": len(new_filings),
        "parse_ok": 0,
        "parse_failed": 0,
        "notify_failed": 0,
        "backlog": backlog,
        "universe_available": universe_available,
    }
    # Filings actually alerted on this run. Backfilling seeded filings does
    # not count, so a quiet run stays completely silent.
    notified = 0

    # 7. Process one filing at a time; nothing here may crash the run.
    for i, filing in enumerate(queue, 1):
        logger.info("[%d/%d] %s - doc %s", i, len(queue), filing.member, filing.doc_id)
        record = known.get(filing.doc_id, {})
        row_number = record.get("_row")
        # A filing seeded into the baseline must never notify, even later.
        suppress_notify = args.seed or record.get("notify_status") == "seeded"

        if i > 1:
            time.sleep(config.FETCH_DELAY_SECONDS)  # be polite to the server

        parsed, tx_rows, notes = [], [], ""
        content = download_pdf(filing.pdf_url)
        if content is None:
            notes = "pdf download failed"
        else:
            try:
                parsed = parse_ptr(io.BytesIO(content))
            except Exception as exc:
                logger.error("Parse error for %s: %s", filing.doc_id, exc)
                notes = ("parse error: %s" % exc)[:400]

        parse_ok = bool(parsed)
        if parse_ok:
            stats["parse_ok"] += 1
            tx_rows = build_transaction_rows(filing, parsed, universe)
        else:
            stats["parse_failed"] += 1
            if not notes:
                notes = (
                    "no text layer (legacy scan)"
                    if filing.is_scanned_legacy
                    else "no transactions matched"
                )

        # Write transactions (a Sheets failure must not block the alert).
        if tx_rows and not args.dry_run:
            try:
                sheets_db.append_transactions(transactions_ws, tx_rows)
            except Exception as exc:
                logger.error(
                    "Could not write transactions for %s: %s", filing.doc_id, exc
                )
                notes = (notes + " | tx write failed: %s" % exc)[:400]

        # Notify. A parse failure still sends a message with the PDF link.
        notify_status = "seeded" if suppress_notify else "pending"
        if not suppress_notify:
            message = format_filing_message(
                filing, tx_rows, parse_ok, universe_available, is_priority(filing)
            )
            if telegram_send(message, dry_run=args.dry_run):
                notify_status = "dry-run" if args.dry_run else "sent"
                notified += 1
            else:
                notify_status = "failed"
                stats["notify_failed"] += 1

        if not args.dry_run and row_number:
            sheets_db.update_filing_status(
                filings_ws,
                row_number,
                parse_status="ok" if parse_ok else "failed",
                notify_status=notify_status,
                transaction_count=len(tx_rows),
                notes=notes,
            )

    # 8. Summary. Sent only when the run had something to report, so that
    #    silence reliably means "nothing new was filed":
    #      - never during seeding
    #      - not while quietly backfilling seeded filings
    #      - always if a filing was alerted on, or a notification failed
    summary = format_summary(stats)
    logger.info("Run complete.\n%s", summary)
    if args.seed:
        logger.info("SEED MODE: no Telegram messages were sent, by design.")
    elif notified or stats["notify_failed"]:
        telegram_send(summary, dry_run=args.dry_run)
    else:
        logger.info(
            "Nothing to alert on (%d filing(s) backfilled); no summary sent.",
            len(queue),
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track newly filed US House Periodic Transaction Reports."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to console; write to neither Sheets nor Telegram.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="First run: record the year to date, send no messages.",
    )
    parser.add_argument(
        "--force-reseed",
        action="store_true",
        help="Allow --seed even though the sheet is already populated.",
    )
    parser.add_argument(
        "--year", type=int, default=None, help="Disclosure year (default: current year)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Override the per-run PDF cap."
    )
    args = parser.parse_args()

    try:
        return run(args)
    except Exception as exc:  # last-resort guard; never crash noisily
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
