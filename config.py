"""
Plain configuration for the congressional / executive-branch trade tracker.
Edit this file freely — it contains no secrets. All credentials come from
environment variables.
"""

# ── Sources ───────────────────────────────────────────────────────────────
# Disclosure sources polled by a normal run, in processing order:
#   house   House Clerk PTRs
#   senate  Senate eFD PTRs (senators and Senate candidates)
#   oge     OGE Form 278-T (President, Cabinet and other executive officials
#           whose reports OGE publishes for direct download)
# Each source is seeded independently; see CLAUDE.md.
ENABLED_SOURCES = ["house", "senate", "oge"]

# ── Members surfaced first ────────────────────────────────────────────────
# Filings from these people are processed and notified ahead of everyone
# else in a run. Match is case-insensitive on the filer's LAST name as it
# appears in the source's index, across all sources. Add or remove freely.
PRIORITY_MEMBERS = [
    "Pelosi",
    "Greene",
    "Khanna",
    "Crenshaw",
    "Gottheimer",
    "McCaul",
    "Tuberville",
    "Trump",
]

# ── Run limits ────────────────────────────────────────────────────────────
# Maximum documents downloaded + parsed per source in a single run. Anything
# left over is picked up on the next run, so a backlog can never exhaust the
# Actions timeout. Roughly (cap x (DELAY + parse time)) seconds of work.
MAX_PDFS_PER_RUN = 25

# Seeding runs record the whole index but parse only this many documents per
# source; the rest are back-filled by later runs. Seeded filings NEVER notify.
MAX_PDFS_PER_SEED_RUN = 40

# Politeness delay, in seconds, between consecutive document fetches from a
# government server. Do not set this to 0.
FETCH_DELAY_SECONDS = 1.5

# HTTP timeouts (seconds)
INDEX_TIMEOUT = 120
PDF_TIMEOUT = 60

# ── Ticker universe ───────────────────────────────────────────────────────
# The largest N US-listed stocks by market cap, from the Nasdaq screener API
# at runtime. Used only to decide which transactions are shown in full in the
# Telegram message; the rest are compressed to a counted line.
UNIVERSE_TARGET_SIZE = 3000

# ── Telegram ──────────────────────────────────────────────────────────────
# Telegram's hard limit is 4096; we split below this for safety.
MAX_MESSAGE_CHARS = 4000

# Most transactions listed line by line in one filing's message. Some 278-Ts
# carry thousands of trades; the rest are counted, and all of them are in the
# transactions tab regardless.
MAX_LISTED_TRANSACTIONS = 60

# ── Sheets ────────────────────────────────────────────────────────────────
TAB_FILINGS = "filings"
TAB_TRANSACTIONS = "transactions"

# ── Identification ────────────────────────────────────────────────────────
USER_AGENT = "sentimentbot-congresstracker/1.0 (personal research; contact via GitHub)"

# For endpoints that reject non-browser clients (Nasdaq screener, Senate eFD).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
