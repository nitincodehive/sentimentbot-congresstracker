# sentimentbot-congresstracker

Polls the US House Clerk's financial disclosure site once per weekday, detects
newly filed **Periodic Transaction Reports (PTRs)**, parses the transactions,
stores them in Google Sheets, and sends one Telegram message per new filing.

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

## Behaviour

- **DocID is the dedup key.** A filing is never notified twice.
- **Seeding.** The first run must be `--seed`: it records the full year to date
  with `notify_status = seeded` and sends nothing. Those rows can never notify
  later. A normal run refuses to start against an empty sheet, and `--seed`
  refuses to run against a populated one (override: `--force-reseed`).
- **Parse failures are never dropped.** A filing whose PDF cannot be parsed is
  still recorded with `parse_status = failed` and still triggers a Telegram
  message naming the member with the PDF link. Roughly 13% of 2026 PTRs are
  legacy paper scans with no text layer; these are detectable by their short
  DocIDs and will always fail. Each run reports its parse failure rate so you
  can judge later whether an LLM fallback is worth adding. **No LLM is used.**
- **Backlog cap.** At most `MAX_PDFS_PER_RUN` (default 25) PDFs per run; the
  remainder is picked up on following runs. `FETCH_DELAY_SECONDS` (default 1.5)
  spaces out requests to the government server.
- **Graceful degradation.** Individual download, parse, Sheets and Telegram
  failures are logged and skipped. The run aborts only if the index itself
  cannot be fetched.
- **Outbound only.** Telegram is used via `sendMessage` alone. No webhook is
  registered and no commands are received.

## Message format

One message per filing, split at 4000 characters. Transactions in tickers in
the Russell 3000 (iShares IWV holdings, fetched at runtime, never committed)
are listed in full; the rest are compressed to a single counted line. Members
in `PRIORITY_MEMBERS` (in `config.py`) are surfaced first and marked.

> **Known issue:** iShares currently serves the product page instead of the
> holdings CSV for scripted requests from some networks. When the universe
> cannot be loaded the run does not fail — it lists every transaction in full
> and says so in the summary. If this persists on the Actions runner, the fix
> belongs in `universe.py` alone.

## Usage

```bash
pip install -r requirements.txt

python main.py --dry-run            # console only; no Sheets, no Telegram
python main.py --seed               # first run; records baseline, sends nothing
python main.py                      # normal incremental run
python main.py --limit 5            # override the per-run PDF cap
```

## Schedule

`.github/workflows/daily.yml` runs at `30 22 * * 1-5` UTC — 18:30 US Eastern
during EDT, 17:30 during EST, always on a weekday in Eastern terms.
`workflow_dispatch` allows manual runs and exposes a `mode` input
(`normal` / `dry-run` / `seed`).

## Files

| File | Role |
| --- | --- |
| `main.py` | Orchestration, seeding guards, CLI |
| `house_index.py` | ZIP/XML index fetch and PTR filter |
| `ptr_parser.py` | pdfplumber PDF text parsing |
| `sheets_db.py` | gspread datastore (`filings`, `transactions`) |
| `telegram_notify.py` | Outbound-only Telegram send + 4000-char splitting |
| `formatter.py` | Message composition, lag-days derivation |
| `universe.py` | Russell 3000 tickers from iShares IWV |
| `config.py` | Editable non-secret configuration |
