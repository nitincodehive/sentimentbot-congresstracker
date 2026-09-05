"""
Google Sheets datastore (gspread + service account).

Credential resolution follows the same pattern swingscanner uses, but this
is an independent implementation for this project's own spreadsheet.

Tabs:
  filings       — one row per PTR filing, keyed on doc_id (the dedup key)
  transactions  — one row per parsed transaction line

Nothing here ever writes a credential to disk or to the sheet.
"""

import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from config import TAB_FILINGS, TAB_TRANSACTIONS

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

FILINGS_HEADERS = [
    "doc_id", "member", "filing_date", "pdf_url",
    "parse_status", "notify_status", "transaction_count",
    "notes", "first_seen", "updated_at",
]

TRANSACTIONS_HEADERS = [
    "doc_id", "member", "ticker", "asset_description", "transaction_type",
    "owner", "amount_range", "transaction_date", "filing_date",
    "lag_days", "in_universe", "created_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Connection ────────────────────────────────────────────────────────────


def init_sheets():
    """Authenticate and return the gspread Spreadsheet handle.

    Credentials, in order:
      1. GOOGLE_SHEETS_CREDENTIALS_JSON env var (JSON string) — used in CI
      2. GOOGLE_APPLICATION_CREDENTIALS / google_credentials.json file — local only

    Spreadsheet is identified by the SPREADSHEET_ID env var.
    """
    creds = None

    json_str = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    if json_str:
        creds = Credentials.from_service_account_info(json.loads(json_str), scopes=SCOPES)

    if creds is None:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.path.join(
            os.path.dirname(__file__), "google_credentials.json"
        )
        if os.path.exists(path):
            creds = Credentials.from_service_account_file(path, scopes=SCOPES)

    if creds is None:
        raise RuntimeError(
            "No Google credentials. Set GOOGLE_SHEETS_CREDENTIALS_JSON (JSON string) "
            "or provide a local google_credentials.json (gitignored)."
        )

    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID env var is required.")

    spreadsheet = gspread.authorize(creds).open_by_key(spreadsheet_id)
    logger.info("Connected to spreadsheet: %s", spreadsheet.title)
    return spreadsheet


def _ensure_tab(spreadsheet, name: str, headers: list[str]):
    """Return the worksheet, creating it with headers if absent."""
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        logger.info("Creating tab '%s'", name)
        ws = spreadsheet.add_worksheet(title=name, rows=1000, cols=max(len(headers), 12))
        ws.update(values=[headers], range_name="A1")
        return ws

    existing = ws.row_values(1)
    if not existing:
        ws.update(values=[headers], range_name="A1")
    return ws


def ensure_tabs(spreadsheet) -> tuple:
    return (
        _ensure_tab(spreadsheet, TAB_FILINGS, FILINGS_HEADERS),
        _ensure_tab(spreadsheet, TAB_TRANSACTIONS, TRANSACTIONS_HEADERS),
    )


# ── Filings ───────────────────────────────────────────────────────────────


def load_filings(ws) -> dict[str, dict]:
    """Return {doc_id: row_dict}, with row_dict['_row'] = 1-based sheet row."""
    try:
        values = ws.get_all_values()
    except Exception as exc:
        logger.error("Could not read filings tab: %s", exc)
        raise

    if len(values) < 2:
        return {}

    headers = values[0]
    out: dict[str, dict] = {}
    for idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        rec = dict(zip(headers, padded))
        doc_id = (rec.get("doc_id") or "").strip()
        if doc_id:
            rec["_row"] = idx
            out[doc_id] = rec
    return out


def append_filings(ws, rows: list[dict]) -> None:
    """Append filing rows. Caller guarantees these doc_ids are new."""
    if not rows:
        return
    now = _now()
    payload = [
        [
            r.get("doc_id", ""), r.get("member", ""), r.get("filing_date", ""),
            r.get("pdf_url", ""), r.get("parse_status", "pending"),
            r.get("notify_status", "pending"), r.get("transaction_count", ""),
            r.get("notes", ""), now, now,
        ]
        for r in rows
    ]
    ws.append_rows(payload, value_input_option="RAW")
    logger.info("Appended %d filing rows", len(payload))


def update_filing_status(ws, row_number: int, parse_status: str,
                         notify_status: str, transaction_count, notes: str = "") -> None:
    """Update the status columns (E..H) plus updated_at (J) for one filing."""
    try:
        ws.update(
            values=[[parse_status, notify_status, transaction_count, notes[:480]]],
            range_name=f"E{row_number}:H{row_number}",
            value_input_option="RAW",
        )
        ws.update(values=[[_now()]], range_name=f"J{row_number}", value_input_option="RAW")
    except Exception as exc:
        logger.error("Failed updating filing row %s: %s", row_number, exc)


# ── Transactions ──────────────────────────────────────────────────────────


def append_transactions(ws, rows: list[dict]) -> None:
    if not rows:
        return
    now = _now()
    payload = [
        [
            r.get("doc_id", ""), r.get("member", ""), r.get("ticker", ""),
            r.get("asset_description", "")[:480], r.get("transaction_type", ""),
            r.get("owner", ""), r.get("amount_range", ""),
            r.get("transaction_date", ""), r.get("filing_date", ""),
            r.get("lag_days", ""), r.get("in_universe", ""), now,
        ]
        for r in rows
    ]
    ws.append_rows(payload, value_input_option="RAW")
    logger.info("Appended %d transaction rows", len(payload))
