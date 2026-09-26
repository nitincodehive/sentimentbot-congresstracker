# CLAUDE.md — Congress PTR Tracker Production Reference

## Project Overview

Polls three official disclosure sources once per weekday, detects newly filed trade reports, parses the transactions, stores them in Google Sheets, and sends one Telegram message per new filing. Runs via GitHub Actions on a weekday cron (22:30 UTC = 18:30 ET during EDT, 17:30 during EST).

| Source | Who | Report |
|---|---|---|
| `house` | House members | Periodic Transaction Report (PTR), House Clerk |
| `senate` | Senators and Senate candidates | PTR, Senate eFD |
| `oge` | President, Cabinet, other executive officials | OGE Form 278-T, directly downloadable ones only |

Every source runs through the same pipeline: reconcile → queue → download → parse → notify. A source is an index module plus a parser, registered in `sources.py`.

Standalone. Shares **no code** with `swingscanner`, `swingscanner-bot`, `market-signals` or `sentimentbot-xsearch`. Where an approach was borrowed (the Sheets credential pattern, the ticker universe source) it was reimplemented here, not imported. Do not add cross-project imports.

The repository is **public**. No credential, token, spreadsheet ID or service-account file may ever be committed.

## Non-Negotiable Invariants

These are the properties the system exists to guarantee. Do not weaken them for convenience.

| Invariant | Where enforced | Why |
|---|---|---|
| **DocID is the dedup key; no filing is ever notified twice** | `main.py` reconciles the index against the `filings` tab by `doc_id` | The whole point of the tracker |
| **A seeded filing can never alert, ever** | `notify_status = "seeded"`, checked via `suppress_notify` in the per-filing loop | Prevents 375 messages landing at once |
| **An unseeded source can never alert** | Per-source guard in `process_source`: a normal run skips any source with no rows in the sheet | Adding a source must not dump its year-to-date backlog |
| **A filing is never dropped because parsing failed** | Parse failure still records the row and still sends a message with the link | ~13% of House PTRs, all Senate paper filings and (in 2026) every presidential 278-T are unparseable by nature (see below) |
| **Every document is parsed; none is assumed unparseable** | No source, filer or position special-cases the parser | The next filer in a given office may e-file cleanly |
| **A partial parse is a failed parse** | `oge_parser` rejects gapped 1..N row numbers and low type/date coverage | A half-list that looks complete is worse than a link |
| **Silence means nothing was filed** | Summary sent only when `notified or notify_failed` | A summary on every quiet run trains you to ignore the channel |
| **Only an unfetchable index skips a source** | `IndexUnavailable` skips that source for the run; the others continue and the run exits 1 | Partial results beat no results |
| **Outbound Telegram only** | `telegram_notify.py` calls `sendMessage` and nothing else | No webhook, no `getUpdates`, no command handling |
| **No LLM anywhere in the pipeline** | — | Explicit design constraint |

## File-by-File Architecture

| File | Purpose | Project Imports |
|---|---|---|
| `config.py` | All tunables and non-secret configuration, including `ENABLED_SOURCES`. Editable by hand. | None (standalone) |
| `filing.py` | The source-independent `Filing` dataclass, `IndexUnavailable`, and `source_of(doc_id)`. | None |
| `house_index.py` | Fetches the House year-to-date archive ZIP, parses the inner XML, filters to `FilingType == "P"`. | `config`, `filing` |
| `senate_index.py` | Accepts the eFD agreement, pages the PTR search JSON, and downloads report pages through that session. | `config`, `filing` |
| `oge_index.py` | Pages OGE's disclosure JSON API and keeps directly downloadable 278-Ts. | `config`, `filing` |
| `ptr_parser.py` | pdfplumber text-layer parsing of a House PTR PDF. Returns `[]` rather than raising on unparseable input. | None |
| `senate_parser.py` | Stdlib HTML table parsing of an e-filed Senate PTR page. `[]` for paper filings. | None |
| `oge_parser.py` | pdfplumber parsing of a 278-T PDF, with the completeness checks. `[]` when unparseable or incomplete. | None |
| `sources.py` | The `SOURCES` registry: (index, download, parse) per source. | the index and parser modules |
| `universe.py` | Top 3,000 US-listed tickers by market cap from the Nasdaq screener API, at runtime. Returns `set()` on failure. | `config` |
| `sheets_db.py` | gspread datastore. Auth, tab creation, read/append/update for both tabs. | `config` |
| `telegram_notify.py` | Outbound `sendMessage`, 4000-char splitting, HTML escaping. | `config` |
| `formatter.py` | Message composition, lag-days derivation, date parsing. | `config`, `telegram_notify` |
| `main.py` | Orchestration, per-source seeding guards, work queue, CLI. | all of the above |

## Data Flow

```
sheets_db.load_filings()            existing state, keyed on doc_id (once per run)

for each source in ENABLED_SOURCES:
  source.fetch_filings(year)        house: {YEAR}FD.zip -> XML -> FilingType "P"
      |                             senate: agreement -> PTR search JSON
      |                             oge: API JSON -> direct-link 278-Ts
      |                             (SKIPS THIS SOURCE if unavailable)
      v
  seeding guard                     source has no rows yet? seed it (--seed)
      |                             or skip it (normal run)
      v
  reconcile                         new = index doc_ids - sheet doc_ids
      |                             append as parse_status=pending,
      |                             notify_status = seeded | pending
      v
  work queue                        not-yet-parsed + parsed-but-not-notified
      |                             sorted: notifiable > priority > newest
      |                             capped at MAX_PDFS_PER_RUN per source
      v
  per filing:  source.download() -> source.parse()
      |                          |
      |                          +-- rows -> sheets_db.append_transactions()
      |                          +-- [] -> parse_status=failed, STILL notifies
      |
      +-- formatter.format_filing_message() -> telegram_notify.send()
      +-- sheets_db.update_filing_status()

summary                             only if something was actually alerted on
```

## Data Sources (verified live, 2026-09)

### House Clerk

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

### Senate eFD (verified live, 2026-09)

| Item | Value |
|---|---|
| Agreement | `GET /search/home/` for the CSRF token, then `POST` it with `prohibition_agreement=1`; success lands on `/search/` |
| Index | `POST https://efdsearch.senate.gov/search/report/data/`, `report_types=[11]` (PTR), `X-CSRFToken` header, paged 100 at a time |
| Row shape | `[first, last, office, link_html, date_received]` |
| E-filed report | `/search/view/ptr/<uuid>/` — HTML table: `#, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, Comment` |
| Paper report | `/search/view/paper/<uuid>/` — GIF page images, no text |
| doc_id | `senate-<uuid>` |

2026 to date: 131 PTRs, 9 of them paper.

The agreement restates the 5 U.S.C. § 13107 limits on using disclosure reports (no commercial use, credit rating or solicitation). The project owner accepted those terms for this personal tracker on 2026-09-26; the bot re-submits it each run. **Keep the tracker personal and non-commercial.**

### OGE (verified live, 2026-09)

| Item | Value |
|---|---|
| Index | `GET https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest`, the JSON behind OGE's public search page |
| Params | the full DataTables set for all six columns (`docDate, title, type, name, agency, level`); sort column 0 desc; 1,000 per page |
| Filter | `type` contains `278 Transaction` **and** a `$FILE` PDF link |
| PDF | `.../PAS+Index/<32-hex UNID>/$FILE/<name>.pdf` |
| doc_id | `oge-<UNID>` |

2026 to date: 713 278-Ts, of which **66 are directly downloadable**. The rest say "Request this Document" and are released only on a manual OGE Form 201 request, which is not automatable and must not be attempted. The downloadable ones cover the President and most Cabinet secretaries.

`filing_date` for OGE is the date OGE **posted** the report, typically 1–3 weeks after the filer signed it, so OGE lag figures overstate the filer's own delay.

## The Seeding Model

Each source is seeded independently. A source counts as seeded once any of its rows is in the `filings` tab (`source_of(doc_id)` tells them apart). Seeding records that source's entire year to date with `notify_status = "seeded"` and sends nothing.

Guards, per source, in `main.process_source`:

- `--seed` for an **already seeded** source → that source is skipped (override: `--force-reseed`, only after deliberately clearing its rows). If *every* requested source is already seeded, the run exits 1.
- a **normal** run for an **unseeded** source → that source is skipped with an error and the run exits 1. The other sources still run.

The second guard is the important one: without it, the first cron run after adding a source would notify on every filing of that source's year at once. So after adding a source, run the workflow once in `seed` mode; sources already seeded are skipped automatically.

Seeded rows keep `notify_status = "seeded"` permanently. Later runs still parse their PDFs (backfill) but the `suppress_notify` check means they can never alert. **There is no third "meta" tab** — seeded-ness lives in the data itself, which is why the state cannot drift out of sync with the rows it describes.

## Parse Reality — read before "improving" the parser

### House

Measured on a random 30-filing sample of the 2026 index: **26 parsed, 4 failed (13.3%)**.

Every failure was a **legacy paper scan with no text layer at all**. These are identifiable by DocID length: e-filed PTRs have 8-digit IDs (`20034201`), legacy scans have 7 (`9116142`, `8221321`). `house_index` sets `Filing.is_paper` from this and the failure message says so.

**No parser change and no LLM can fix these** — the PDFs contain zero extractable text, only page images. OCR would be the only route, and it is explicitly out of scope. The per-run failure rate in the summary exists so this can be judged over time from the `filings` tab, not so the parser can be blamed.

### Senate and OGE (full 2026 year-to-date sweep, 2026-09-26)

| Source | Filings | Parsed | Failed | Transactions | Every failure was… |
|---|---|---|---|---|---|
| Senate | 131 | 122 | 9 (6.9%) | 1,647 | a paper filing (all 9 were Blumenthal's), GIF page images only |
| OGE | 66 | 55 | 11 (16.7%) | 991 | 10 presidential copier scans, plus 1 report listing only two swap contracts with no type or amount |

Every e-filed Senate report and every Integrity-generated 278-T with ordinary trades parsed completely. The OGE failures are not parser gaps:

- **Presidential 278-Ts (2026)**: printed and scanned. One has no text layer; the others carry Acrobat OCR too noisy to trust (`$1 000 001 -$5 000 000`, `717/2026`). Relaxed patterns matched up to ~70% of rows with garbage in them, which is exactly why `oge_parser` refuses partial results (gapped 1..N numbering, a readable row N+1, or under 90% of rows with a type and date). Don't loosen those checks to "rescue" these.
- **Blank fields are legitimate.** Filers sometimes omit a type or date on a single row (Mody's 306-row report has 5), so both are optional per row. A cleanly formatted amount and the Yes/No column are what anchor a row.

### House parser notes

- **Table extraction is unreliable on these documents** and was tried first — `extract_tables()` merges multi-row cells inconsistently. The text layer is parsed line by line instead. Do not switch back.
- Small-caps label glyphs ("Filing Status:", "Subholding Of:", "Description:") decode to **NUL characters**, hence `_clean()` and the `\x00` patterns in `META_RE`. This looks like mojibake but is load-bearing.
- Asset descriptions wrap across lines, and an amount range can split (`$15,001 -` / `$50,000`). The continuation lookahead in `parse_ptr` handles both.
- Owner codes: `SP` spouse, `DC` dependent, `JT` joint, absent = self.
- Transaction types: `P`, `S`, `S (partial)`, `E`.
- The page header row repeats mid-document; `META_RE` excludes it from continuations.

## Sheets Schema

**`filings`** — one row per filing across all sources, the dedup key is `doc_id`:

`doc_id, member, filing_date, pdf_url, parse_status, notify_status, transaction_count, notes, first_seen, updated_at`

- `doc_id`: bare Clerk DocID for House (unchanged from the House-only version, so its existing rows still match), `senate-<uuid>`, `oge-<UNID>`. The source is derived from this prefix by `filing.source_of()`; **there is no source column**, and none is needed.
- `pdf_url` holds the document link. For Senate that is the HTML report page, not a PDF; the header was kept so existing sheets need no migration.

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

Each source has its own queue and its own cap. `PRIORITY_MEMBERS` matches last names across all sources (it includes `Trump` for OGE and `Tuberville` for the Senate).

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
python main.py --seed           # seeds every unseeded source; sends nothing
python main.py                  # normal incremental run, all enabled sources
python main.py --source oge     # one source only (house | senate | oge)
python main.py --limit 5        # override the per-source, per-run document cap
python main.py --year 2025      # a different disclosure year
python main.py --force-reseed --seed --source senate   # only after deliberately clearing that source's rows
```

The workflow's manual trigger exposes the same choices as `mode` and `source` inputs.

`--dry-run` needs no secrets at all — it skips Sheets entirely and treats every filing as new. It is the fastest way to check a parser or formatting change against live data.

## Run Limits

| Setting | Default | Rationale |
|---|---|---|
| `MAX_PDFS_PER_RUN` | 25 | Per source. Bounds a backlog so it cannot exhaust the Actions timeout; remainder carries to the next run |
| `MAX_PDFS_PER_SEED_RUN` | 40 | Per source. Seeding records all filings but parses only this many |
| `FETCH_DELAY_SECONDS` | 1.5 | Politeness to a government server. **Do not set to 0.** |
| `MAX_MESSAGE_CHARS` | 4000 | Telegram's hard limit is 4096 |
| `MAX_LISTED_TRANSACTIONS` | 60 | Lines listed per filing message; a 278-T can hold thousands of trades. The rest are counted, and all are in the sheet |
| Job `timeout-minutes` | 45 | In `daily.yml`. Presidential 278-T scans are 10–27 MB and take several seconds each to parse |

## Gotchas

- **Two endpoints return 200 while serving the wrong thing.** iShares (now unused) and potentially Nasdaq. Always check the body, not the status.
- **Nasdaq and iShares both reject non-browser User-Agents.** `universe.py` and `senate_index.py` use `config.BROWSER_UA` deliberately; the descriptive `config.USER_AGENT` is used for the House Clerk and OGE, which accept it.
- **The OGE API returns HTTP 400 unless every DataTables column parameter is sent**, and it ignores `search[value]` entirely. Filter client-side; don't "simplify" the parameter block.
- **Most OGE 278-Ts are not downloadable.** "Request this Document" rows need a manual OGE Form 201. Never try to automate that form.
- **Senate report links need the agreement session.** Opened from Telegram in a fresh browser they first show the agreement page; the message says so. `senate_index.download()` re-agrees once if its own session lapses.
- **Presidential 278-Ts in 2026 are copier scans.** Some have no text layer, some have noisy OCR ("lourchaso 717/2026"). They fail parsing by design and alert with the link. Do not add OCR or filer-specific handling; a future filer may e-file cleanly and will then parse normally.
- **Share-class separators differ per source**: Nasdaq `BRK/B`, iShares `BRK-B`, House PTRs `BRK.B`. `_normalize()` converts to dots.
- **`update_filing_status` writes fixed A1 ranges** (E:H, J). Reordering `FILINGS_HEADERS` silently corrupts rows.
- **GitHub Actions deprecates Node runtimes periodically.** `checkout` and `setup-python` are pinned at v7. A yellow annotation about Node is a warning, not a failure.
- **The summary is suppressed on quiet runs.** If you are testing and see no Telegram message, that is correct behaviour, not a broken token — check the log for `Nothing to alert on`.
