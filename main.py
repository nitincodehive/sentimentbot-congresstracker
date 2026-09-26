"""
Congressional and executive-branch trade tracker - entry point.

Once per weekday, for each enabled source (House Clerk PTRs, Senate eFD PTRs,
OGE 278-Ts): pull the year-to-date index, find newly filed reports, parse
them, store them in Google Sheets, and send one Telegram message per new
filing.

Modes
  (default)      incremental run; notifies on filings not seen before
  --seed         records each not-yet-seeded source's full year to date and
                 sends NOTHING to Telegram. Sources already seeded are
                 skipped (see --force-reseed).
  --dry-run      prints to console; writes to neither Sheets nor Telegram
  --source NAME  restrict the run to one source

Graceful degradation: any single filing that fails is logged, recorded and
skipped. A source whose index is unreachable is skipped for the run while
the others continue; the run then exits non-zero.
"""

import argparse
import logging
import sys
import time
from datetime import datetime

import config
import sheets_db
from filing import IndexUnavailable, source_of
from formatter import format_filing_message, format_summary, lag_days, parse_date
from sources import SOURCES
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


def process_source(source, args, year, known, sheet, universe, stats) -> str:
    """Run one source end to end.

    Returns "ok", "skipped" (seeding requested but already seeded),
    "not_seeded", "index_unavailable" or "sheets_error". Mutates `known`
    (reloaded after new rows are appended) and `stats`.
    """
    filings_ws, transactions_ws = sheet
    name = source.name

    # 1. Index - fatal for this source only.
    try:
        filings = source.fetch_filings(year)
    except IndexUnavailable as exc:
        logger.error("[%s] SKIPPING SOURCE: %s", name, exc)
        stats["unavailable"].append(name)
        return "index_unavailable"

    by_doc_id = {f.doc_id: f for f in filings}
    logger.info("[%s] Index has %d filings for %d", name, len(by_doc_id), year)

    # 2. Seeding guards - per source, explicit, and safe in both directions.
    #    A source counts as seeded once any of its rows is in the sheet, so a
    #    source added later can never flood the channel with its backlog.
    source_seeded = any(source_of(doc_id) == name for doc_id in known)
    if args.seed:
        if source_seeded and not args.force_reseed:
            logger.info(
                "[%s] Already seeded (%d rows); skipping. Pass --force-reseed "
                "only after deliberately clearing this source's rows.",
                name, sum(source_of(d) == name for d in known),
            )
            return "skipped"
        logger.info(
            "[%s] SEED MODE: recording the full year to date. "
            "No Telegram messages will be sent.", name,
        )
        if not by_doc_id:
            logger.warning(
                "[%s] Nothing to seed yet, so the next normal run will still "
                "treat this source as unseeded.", name,
            )
    elif not source_seeded and not args.dry_run:
        logger.error(
            "[%s] SKIPPING SOURCE: it has never been seeded. A normal run would "
            "notify on all %d of its filings at once. Run once with --seed "
            "(sources already seeded are skipped automatically).",
            name, len(by_doc_id),
        )
        return "not_seeded"

    # 3. Reconcile on doc_id, the dedup key.
    new_filings = [f for doc_id, f in by_doc_id.items() if doc_id not in known]
    logger.info("[%s] New filings since last run: %d", name, len(new_filings))
    stats["new_filings"] += len(new_filings)
    stats["new_by_source"][name] = len(new_filings)

    if not args.dry_run and new_filings:
        try:
            sheets_db.append_filings(
                filings_ws,
                [
                    {
                        "doc_id": f.doc_id,
                        "member": f.member,
                        "filing_date": f.filing_date,
                        "pdf_url": f.url,
                        "parse_status": "pending",
                        # Seeded filings are permanently excluded from alerts.
                        "notify_status": "seeded" if args.seed else "pending",
                        "transaction_count": "",
                        "notes": "seeded baseline" if args.seed else "",
                    }
                    for f in new_filings
                ],
            )
            known.clear()
            known.update(sheets_db.load_filings(filings_ws))
        except Exception as exc:
            logger.error("[%s] SKIPPING SOURCE: could not record new filings: %s", name, exc)
            return "sheets_error"

    # 4. Work queue: anything not yet parsed, plus anything parsed but not
    #    yet notified. Notifiable first, then priority members, then newest.
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
    stats["backlog"] += backlog
    queue = queue[:cap]
    logger.info(
        "[%s] Processing %d filing(s) this run (%d left for next run)",
        name, len(queue), backlog,
    )

    # 5. Process one filing at a time; nothing here may crash the run.
    for i, filing in enumerate(queue, 1):
        logger.info("[%s %d/%d] %s - doc %s", name, i, len(queue), filing.member, filing.doc_id)
        record = known.get(filing.doc_id, {})
        row_number = record.get("_row")
        # A filing seeded into the baseline must never notify, even later.
        suppress_notify = args.seed or record.get("notify_status") == "seeded"

        if i > 1:
            time.sleep(config.FETCH_DELAY_SECONDS)  # be polite to the server

        parsed, tx_rows, notes = [], [], ""
        content = source.download(filing)
        if content is None:
            notes = "download failed"
        else:
            try:
                parsed = source.parse(content)
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
                    "no text layer (paper filing)"
                    if filing.is_paper
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

        # Notify. A parse failure still sends a message with the link.
        notify_status = "seeded" if suppress_notify else "pending"
        if not suppress_notify:
            message = format_filing_message(
                filing, tx_rows, parse_ok, stats["universe_available"], is_priority(filing)
            )
            if telegram_send(message, dry_run=args.dry_run):
                notify_status = "dry-run" if args.dry_run else "sent"
                stats["notified"] += 1
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

    stats["processed"] += len(queue)
    return "ok"


def run(args) -> int:
    year = args.year or datetime.now().year
    names = config.ENABLED_SOURCES if args.source == "all" else [args.source]

    # 1. Sheets state, shared by every source.
    known = {}
    sheet = (None, None)
    if not args.dry_run:
        try:
            spreadsheet = sheets_db.init_sheets()
            sheet = sheets_db.ensure_tabs(spreadsheet)
            known = sheets_db.load_filings(sheet[0])
        except Exception as exc:
            logger.error("ABORTING: Sheets unavailable: %s", exc)
            return 1
    else:
        logger.info("[DRY RUN] Skipping Sheets; treating every filing as new.")

    # 2. Universe (optional; degrades to "show everything").
    universe = load_universe()

    stats = {
        "new_filings": 0,
        "new_by_source": {},
        "processed": 0,
        "parse_ok": 0,
        "parse_failed": 0,
        # Filings actually alerted on. Backfilling seeded filings does not
        # count, so a quiet run stays completely silent.
        "notified": 0,
        "notify_failed": 0,
        "backlog": 0,
        "unavailable": [],
        "universe_available": bool(universe),
    }

    # 3. Each source independently; one failing never stops the others.
    outcomes = {
        name: process_source(SOURCES[name], args, year, known, sheet, universe, stats)
        for name in names
    }
    logger.info("Source outcomes: %s", outcomes)

    # 4. Summary. Sent only when the run had something to report, so that
    #    silence reliably means "nothing new was filed":
    #      - never during seeding
    #      - not while quietly backfilling seeded filings
    #      - always if a filing was alerted on, or a notification failed
    summary = format_summary(stats)
    logger.info("Run complete.\n%s", summary)
    if args.seed:
        logger.info("SEED MODE: no Telegram messages were sent, by design.")
    elif stats["notified"] or stats["notify_failed"]:
        telegram_send(summary, dry_run=args.dry_run)
    else:
        logger.info(
            "Nothing to alert on (%d filing(s) backfilled); no summary sent.",
            stats["processed"],
        )

    # 5. Exit code. Non-zero (a red Actions run) whenever a source was
    #    skipped for a reason that needs attention, even though the other
    #    sources completed normally.
    if args.seed and all(o == "skipped" for o in outcomes.values()):
        logger.error(
            "ABORTING: every requested source is already seeded, so nothing "
            "was seeded. Run without --seed for a normal incremental run."
        )
        return 1
    if any(o in ("index_unavailable", "not_seeded", "sheets_error") for o in outcomes.values()):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track newly filed congressional and executive-branch trade reports."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to console; write to neither Sheets nor Telegram.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Record each unseeded source's year to date, send no messages.",
    )
    parser.add_argument(
        "--force-reseed",
        action="store_true",
        help="Allow --seed for a source that already has rows in the sheet.",
    )
    parser.add_argument(
        "--source",
        choices=["all", *SOURCES],
        default="all",
        help="Restrict the run to one source (default: every enabled source).",
    )
    parser.add_argument(
        "--year", type=int, default=None, help="Disclosure year (default: current year)."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Override the per-source, per-run document cap.",
    )
    args = parser.parse_args()

    try:
        return run(args)
    except Exception as exc:  # last-resort guard; never crash noisily
        logger.exception("Unhandled error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
