# sentimentbot-congresstracker

Polls three official financial-disclosure sources once per weekday, detects
newly filed trade reports, parses the transactions, stores them in Google
Sheets, and sends one Telegram message per new filing:

| Source | Covers | Report |
| --- | --- | --- |
| House Clerk | House members | Periodic Transaction Report (PTR) |
| Senate eFD | Senators and Senate candidates | PTR |
| OGE | President, Cabinet, other executive officials | OGE Form 278-T (directly downloadable ones) |

Standalone project. It shares no code with `swingscanner`, `swingscanner-bot`,
`market-signals` or `sentiment-bot-xsearch`.

## No secrets in this repository

This repo is public. It contains **no** tokens, credentials, spreadsheet IDs or
service-account files. Everything sensitive is read from environment variables,
supplied in CI as GitHub Actions secrets:

| Secret | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | The bot dedicated to this project |
| `TELEGRAM_CHAT_ID` | The existing sentiment-bot chat |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | Service-account JSON, as a single-line string |
| `SPREADSHEET_ID` | The spreadsheet key from its URL |

`.gitignore` covers `.env` and any local credential file. See `.env.example`.

## Data sources (verified live)

| Item | Value |
| --- | --- |
| Index | `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip` |
| Inner file | `{YEAR}FD.xml`, root `<FinancialDisclosure>`, one `<Member>` per filing |
| Fields | `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID` |
| Filter | `FilingType == "P"` |
| PDFs | `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf` |

Both URL patterns and the XML schema were confirmed against the live site and
are unchanged.

**Senate eFD** (`efdsearch.senate.gov`) is gated by a click-through agreement
restating the legal limits on using disclosure reports (no commercial use,
credit rating or solicitation). The bot accepts it at the start of each run;
keep this tracker personal and non-commercial. E-filed Senate PTRs are HTML
tables; paper ones are page images.

**OGE** reports are read from the JSON API behind OGE's public disclosure
search. Only 278-Ts with a direct PDF link are tracked; most executive-branch
278-Ts are released only on a manual request form, which is not automated.

See `CLAUDE.md` for the verified endpoint details.

## Behaviour

- **DocID is the dedup key.** A filing is never notified twice.
- **Seeding, per source.** Each source must be seeded once with `--seed`: it
  records that source's full year to date with `notify_status = seeded` and
  sends nothing. Those rows can never notify later. A normal run skips (and
  fails loudly on) any source that has never been seeded; `--seed` skips any
  source already seeded (override: `--force-reseed`). After adding a source,
  run the workflow once in `seed` mode.
- **Parse failures are never dropped.** A filing whose document cannot be
  parsed is still recorded with `parse_status = failed` and still triggers a
  Telegram message naming the filer with the link. Roughly 13% of 2026 House
  PTRs are legacy paper scans with no text layer, Senate paper filings are
  page images, and the President's 2026 278-Ts are copier scans; all of these
  fail and alert with the link. Every document is still run through the
  parser, so a filer who e-files parses normally. A 278-T that parses only
  partly counts as a failure rather than showing an incomplete list. Each run
  reports its parse failure rate. **No LLM is used.**
- **Backlog cap.** At most `MAX_PDFS_PER_RUN` (default 25) documents per
  source per run; the remainder is picked up on following runs.
  `FETCH_DELAY_SECONDS` (default 1.5) spaces out requests to the government
  servers.
- **Graceful degradation.** Individual download, parse, Sheets and Telegram
  failures are logged and skipped. A source whose index cannot be fetched is
  skipped for that run while the others continue, and the run exits non-zero.
- **Outbound only.** Telegram is used via `sendMessage` alone. No webhook is
  registered and no commands are received.
- **Silence means nothing new.** A run-summary message is sent only when a
  filing was actually alerted on (or a notification failed). Runs that merely
  backfill seeded filings, and runs with nothing to do, send nothing at all.

## Message format

One message per filing, split at 4000 characters. Transactions in tickers
inside the universe are listed in full; the rest are compressed to a single
counted line. Members in `PRIORITY_MEMBERS` (in `config.py`) are surfaced
first and marked.

The universe is the largest `UNIVERSE_TARGET_SIZE` (3,000) US-listed stocks
by market cap, from the Nasdaq screener API at runtime, never committed. This
matches the source swingscanner moved to after iShares began serving its
product page instead of the IWV holdings CSV for scripted clients. Non-equity
lines (baby bonds, warrants, preferreds, rights) are excluded so they cannot
displace real companies out of the ranking; ADRs are kept, since members trade
them. If the universe cannot be loaded the run does not fail — it lists every
transaction in full and says so in the summary.

## Usage

```bash
pip install -r requirements.txt

python main.py --dry-run            # console only; no Sheets, no Telegram
python main.py --seed               # seeds unseeded sources; sends nothing
python main.py                      # normal incremental run
python main.py --source senate      # one source only (house | senate | oge)
python main.py --limit 5            # override the per-source document cap
```

## Schedule

`.github/workflows/daily.yml` runs at `30 22 * * 1-5` UTC — 18:30 US Eastern
during EDT, 17:30 during EST, always on a weekday in Eastern terms.
`workflow_dispatch` allows manual runs and exposes a `mode` input
(`normal` / `dry-run` / `seed`) and a `source` input (`all` / `house` /
`senate` / `oge`).

## Files

| File | Role |
| --- | --- |
| `main.py` | Orchestration, per-source seeding guards, CLI |
| `sources.py` | Registry of sources: index, download, parse |
| `filing.py` | Source-independent `Filing` record |
| `house_index.py` | House ZIP/XML index fetch and PTR filter |
| `senate_index.py` | Senate eFD agreement, PTR search, report download |
| `oge_index.py` | OGE disclosure API, downloadable 278-Ts |
| `ptr_parser.py` | House PTR PDF text parsing |
| `senate_parser.py` | Senate PTR HTML table parsing |
| `oge_parser.py` | 278-T PDF text parsing with completeness checks |
| `sheets_db.py` | gspread datastore (`filings`, `transactions`) |
| `telegram_notify.py` | Outbound-only Telegram send + 4000-char splitting |
| `formatter.py` | Message composition, lag-days derivation |
| `universe.py` | Ticker universe from the Nasdaq screener API |
| `config.py` | Editable non-secret configuration |
| `CLAUDE.md` | Architecture, invariants and gotchas reference |
