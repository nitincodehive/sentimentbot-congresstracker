# CLAUDE.md — Congress PTR Tracker Production Reference

## Project Overview

Polls the US House Clerk's financial disclosure site once per weekday, detects newly filed **Periodic Transaction Reports (PTRs)**, parses the transactions out of the PDFs, stores them in Google Sheets, and sends one Telegram message per new filing. Runs via GitHub Actions on a weekday cron (22:30 UTC = 18:30 ET during EDT, 17:30 during EST).

Standalone. Shares **no code** with `swingscanner`, `swingscanner-bot`, `market-signals` or `sentimentbot-xsearch`. Where an approach was borrowed (the Sheets credential pattern, the ticker universe source) it was reimplemented here, not imported. Do not add cross-project imports.

The repository is **public**. No credential, token, spreadsheet ID or service-account file may ever be committed.

## Non-Negotiable Invariants

These are the properties the system exists to guarantee. Do not weaken them for convenience.

| Invariant | Where enforced | Why |
|---|---|---|
| **DocID is the dedup key; no filing is ever notified twice** | `main.py` reconciles the index against the `filings` tab by `doc_id` | The whole point of the tracker |
| **A seeded filing can never alert, ever** | `notify_status = "seeded"`, checked via `suppress_notify` in the per-filing loop | Prevents 375 messages landing at once |
| **A filing is never dropped because parsing failed** | Parse failure still records the row and still sends a message with the PDF link | ~13% of PTRs are unparseable by nature (see below) |
| **Silence means nothing was filed** | Summary sent only when `notified or notify_failed` | A summary on every quiet run trains you to ignore the channel |
| **The run aborts only if the index is unfetchable** | `IndexUnavailable` is the sole fatal error; everything else logs and skips | Partial results beat no results |
| **Outbound Telegram only** | `telegram_notify.py` calls `sendMessage` and nothing else | No webhook, no `getUpdates`, no command handling |
| **No LLM anywhere in the pipeline** | — | Explicit design constraint |

## File-by-File Architecture

| File | Purpose | Project Imports |
|---|---|---|
| `config.py` | All tunables and non-secret configuration. Editable by hand. | None (standalone) |
| `house_index.py` | Fetches the year-to-date archive ZIP, parses the inner XML, filters to `FilingType == "P"`. Defines the `Filing` dataclass and `IndexUnavailable`. | `config` |
| `ptr_parser.py` | pdfplumber text-layer parsing of a PTR PDF into transaction dicts. Returns `[]` rather than raising on unparseable input. | None |
| `universe.py` | Top 3,000 US-listed tickers by market cap from the Nasdaq screener API, at runtime. Returns `set()` on failure. | `config` |
| `sheets_db.py` | gspread datastore. Auth, tab creation, read/append/update for both tabs. | `config` |
| `telegram_notify.py` | Outbound `sendMessage`, 4000-char splitting, HTML escaping. | `config` |
| `formatter.py` | Message composition, lag-days derivation, date parsing. | `telegram_notify` |
| `main.py` | Orchestration, seeding guards, work queue, CLI. | all of the above |

## Data Flow

```
house_index.fetch_filings(year)     {YEAR}FD.zip -> {YEAR}FD.xml -> FilingType "P"
      |                             (ABORTS the run if unavailable)
      v
sheets_db.load_filings()            existing state, keyed on doc_id
      |
      v
reconcile                           new = index doc_ids - sheet doc_ids
      |                             append as parse_status=pending,
      |                             notify_status = seeded | pending
      v
work queue                          not-yet-parsed + parsed-but-not-notified
      |                             sorted: notifiable > priority > newest
      |                             capped at MAX_PDFS_PER_RUN
      v
per filing:  download PDF -> ptr_parser.parse_ptr()
      |                          |
      |                          +-- rows -> sheets_db.append_transactions()
      |                          +-- [] -> parse_status=failed, STILL notifies
      |
      +-- formatter.format_filing_message() -> telegram_notify.send()
      +-- sheets_db.update_filing_status()
      v
summary                             only if something was actually alerted on
```

## Data Sources (verified live, 2026-09)

| Item | Value |
|---|---|
| Index | `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip` |
| Inner file | `{YEAR}FD.xml` (a `{YEAR}FD.txt` twin also exists; unused) |
| XML shape | root `<FinancialDisclosure>`, one `<Member>` per filing |
| Fields | `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID` |
| Filter | `FilingType == "P"` |
| PDFs | `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf` |

2026 `FilingType` distribution, for sanity-checking a future schema change: `C: 766, P: 375, X: 247, W: 101, D: 68, A: 33, H: 2, T: 2` out of 1,594 entries.

`FilingDate` is `M/D/YYYY` (not zero-padded); transaction dates inside the PDFs are `MM/DD/YYYY`. `formatter.parse_date()` accepts both.

## The Seeding Model

The first run must be `--seed`. It records the entire year to date with `notify_status = "seeded"` and sends nothing.

Guards, in `main.py`:

- `--seed` against a **populated** sheet → abort (override: `--force-reseed`, only after deliberately clearing the sheet)
- a **normal** run against an **empty** sheet → abort, telling you to seed first

The second guard is the important one: without it, a first cron run would notify on every filing of the year at once.

Seeded rows keep `notify_status = "seeded"` permanently. Later runs still parse their PDFs (backfill) but the `suppress_notify` check means they can never alert. **There is no third "meta" tab** — seeded-ness lives in the data itself, which is why the state cannot drift out of sync with the rows it describes.

## Parse Reality — read before "improving" the parser

Measured on a random 30-filing sample of the 2026 index: **26 parsed, 4 failed (13.3%)**.

Every failure was a **legacy paper scan with no text layer at all**. These are identifiable by DocID length: e-filed PTRs have 8-digit IDs (`20034201`), legacy scans have 7 (`9116142`, `8221321`). `Filing.is_scanned_legacy` tests this and the failure message says so.

**No parser change and no LLM can fix these** — the PDFs contain zero extractable text, only page images. OCR would be the only route, and it is explicitly out of scope. The per-run failure rate in the summary exists so this can be judged over time from the `filings` tab, not so the parser can be blamed.

Other parser notes:

- **Table extraction is unreliable on these documents** and was tried first — `extract_tables()` merges multi-row cells inconsistently. The text layer is parsed line by line instead. Do not switch back.
- Small-caps label glyphs ("Filing Status:", "Subholding Of:", "Description:") decode to **NUL characters**, hence `_clean()` and the `\x00` patterns in `META_RE`. This looks like mojibake but is load-bearing.
- Asset descriptions wrap across lines, and an amount range can split (`$15,001 -` / `$50,000`). The continuation lookahead in `parse_ptr` handles both.
- Owner codes: `SP` spouse, `DC` dependent, `JT` joint, absent = self.
- Transaction types: `P`, `S`, `S (partial)`, `E`.
- The page header row repeats mid-document; `META_RE` excludes it from continuations.

## Sheets Schema

**`filings`** — one row per PTR, the dedup key is `doc_id`:

`doc_id, member, filing_date, pdf_url, parse_status, notify_status, transaction_count, notes, first_seen, updated_at`

- `parse_status`: `pending` | `ok` | `failed`
- `notify_status`: `pending` | `sent` | `failed` | `seeded` | `dry-run`

`update_filing_status()` writes columns **E:H** and **J** by row number. **If you reorder the headers you must update those ranges.**

**`transactions`** — one row per parsed line:

`doc_id, member, ticker, asset_description, transaction_type, owner, amount_range, transaction_date, filing_date, lag_days, in_universe, created_at`

Both tabs are created automatically with correct headers on first run. Do not hand-create them.

## Ticker Universe

The largest `UNIVERSE_TARGET_SIZE` (3,000) US-listed stocks by market cap, from the Nasdaq screener API at runtime. Never committed.

Used for exactly one thing: deciding whether a transaction is shown in full in the Telegram message or folded into the compressed "+ N other" count. It is **presentation, not a trading decision**, so failure degrades to "show everything in full" and says so in the summary.

- **Originally iShares IWV (Russell 3000).** iShares now serves its product page — with a `text/csv` content-type and a **200 status** — instead of the CSV for scripted clients. Neither the status code nor the header catches that; only inspecting the body does, which is what `_reject_html()` is for. Keep that guard.
- **Non-equity lines are excluded** (baby bonds, warrants, preferreds, rights) because each one otherwise displaces a real company out of the ranking — leaving them in pushed the market-cap cutoff from ~$600M to ~$780M.
- **ADRs are deliberately kept.** Members trade them, and there is no country filter, so foreign issuers stay in. This is intentionally more inclusive than a strict index reconstruction.

## Work Queue Ordering

Sort terms in `sort_key(filing, record)`, in order:

1. **Notifiable before backfill-only** (`notify_status == "seeded"` sorts last)
2. Priority member before everyone else
3. Newest filing date first

Term 1 matters and was a real bug: the 2026 index has exactly **25** priority-member filings against a cap of **25**, so ranking priority first unconditionally put a genuinely new non-priority filing at position 26 — one slot outside the cap, delayed a day. Priority now only orders filings that can actually alert.

## Secrets

Four GitHub Actions repository secrets, read from environment variables. Nothing sensitive is ever written to disk, to the sheet, or to a log line.

| Secret | Notes |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Shared with the `sentimentbot-xsearch` bot; deliberately **not** swingscanner's |
| `TELEGRAM_CHAT_ID` | The existing sentiment-bot chat |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | Service-account JSON as a single-line string; the `\n` in `private_key` must stay escaped |
| `SPREADSHEET_ID` | This project's **own** spreadsheet, not swingscanner's |

The service account is shared with swingscanner. That is safe: Sheets access is granted per-spreadsheet by sharing, and `sheets_db` only ever calls `open_by_key(SPREADSHEET_ID)` — no listing, no open-by-name. Tab names don't collide either (`filings`/`transactions` here vs `signals`/`trades`/`trade_updates`/`account` there).

`.gitignore` covers `.env`, `*credentials*.json`, `*service_account*.json` and key files. It was written **before** the first commit.

## Commands

```bash
pip install -r requirements.txt

python main.py --dry-run        # console only; no Sheets, no Telegram
python main.py --seed           # first run; records baseline, sends nothing
python main.py                  # normal incremental run
python main.py --limit 5        # override the per-run PDF cap
python main.py --year 2025      # a different disclosure year
python main.py --force-reseed --seed   # only after deliberately clearing the sheet
```

`--dry-run` needs no secrets at all — it skips Sheets entirely and treats every filing as new. It is the fastest way to check a parser or formatting change against live data.

## Run Limits

| Setting | Default | Rationale |
|---|---|---|
| `MAX_PDFS_PER_RUN` | 25 | Bounds a backlog so it cannot exhaust the Actions timeout; remainder carries to the next run |
| `MAX_PDFS_PER_SEED_RUN` | 40 | Seeding records all filings but parses only this many |
| `FETCH_DELAY_SECONDS` | 1.5 | Politeness to a government server. **Do not set to 0.** |
| `MAX_MESSAGE_CHARS` | 4000 | Telegram's hard limit is 4096 |
| Job `timeout-minutes` | 30 | In `daily.yml` |

## Gotchas

- **Two endpoints return 200 while serving the wrong thing.** iShares (now unused) and potentially Nasdaq. Always check the body, not the status.
- **Nasdaq and iShares both reject non-browser User-Agents.** `universe.py` uses a browser UA deliberately; the descriptive `config.USER_AGENT` is for the House Clerk only, which does not care.
- **Share-class separators differ per source**: Nasdaq `BRK/B`, iShares `BRK-B`, House PTRs `BRK.B`. `_normalize()` converts to dots.
- **`update_filing_status` writes fixed A1 ranges** (E:H, J). Reordering `FILINGS_HEADERS` silently corrupts rows.
- **GitHub Actions deprecates Node runtimes periodically.** `checkout` and `setup-python` are pinned at v7. A yellow annotation about Node is a warning, not a failure.
- **The summary is suppressed on quiet runs.** If you are testing and see no Telegram message, that is correct behaviour, not a broken token — check the log for `Nothing to alert on`.
