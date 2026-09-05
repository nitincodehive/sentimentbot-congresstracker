"""
Plain configuration for the House PTR tracker. Edit this file freely —
it contains no secrets. All credentials come from environment variables.
"""

# ── Members surfaced first ────────────────────────────────────────────────
# Filings from these members are processed and notified ahead of everyone
# else in a run. Match is case-insensitive on the member's LAST name as it
# appears in the House Clerk XML. Add or remove names as you like.
PRIORITY_MEMBERS = [
    "Pelosi",
    "Greene",
    "Khanna",
    "Crenshaw",
    "Gottheimer",
    "McCaul",
    "Tuberville",
]

# ── Run limits ────────────────────────────────────────────────────────────
# Maximum PDFs downloaded + parsed in a single run. Anything left over is
# picked up on the next run, so a backlog can never exhaust the Actions
# timeout. Roughly (cap x (DELAY + parse time)) seconds of work.
MAX_PDFS_PER_RUN = 25

# Seeding runs only record the filing index and parse this many PDFs; the
# rest are back-filled by later runs. Seeded filings NEVER notify.
MAX_PDFS_PER_SEED_RUN = 40

# Politeness delay, in seconds, between consecutive PDF fetches from the
# government server. Do not set this to 0.
FETCH_DELAY_SECONDS = 1.5

# HTTP timeouts (seconds)
INDEX_TIMEOUT = 120
PDF_TIMEOUT = 60

# ── Telegram ──────────────────────────────────────────────────────────────
# Telegram's hard limit is 4096; we split below this for safety.
MAX_MESSAGE_CHARS = 4000

# ── Sheets ────────────────────────────────────────────────────────────────
TAB_FILINGS = "filings"
TAB_TRANSACTIONS = "transactions"

# ── Identification ────────────────────────────────────────────────────────
USER_AGENT = "sentimentbot-congresstracker/1.0 (personal research; contact via GitHub)"
