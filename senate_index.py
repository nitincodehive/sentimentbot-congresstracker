"""
Senate eFD (electronic Financial Disclosure) index.

Senators and Senate candidates file PTRs with the Secretary of the Senate,
published at https://efdsearch.senate.gov. The site is gated by a
click-through agreement restating the 5 U.S.C. § 13107 limits on using
disclosure reports (no commercial use, credit rating or solicitation). The
project owner has accepted those terms for this personal tracker; the bot
re-submits the agreement at the start of each run, because the session it
creates does not persist between runs.

Flow, verified live:
  1. GET  /search/home/          -> csrftoken cookie + csrfmiddlewaretoken
  2. POST /search/home/          prohibition_agreement=1 -> lands on /search/
  3. POST /search/report/data/   DataTables query, report_types=[11] (PTR)
     -> {"recordsTotal": n, "data": [[first, last, office, link_html, date]]}

link_html points at either
  /search/view/ptr/<uuid>/    an e-filed report, rendered as an HTML table
  /search/view/paper/<uuid>/  a paper filing, rendered as GIF page images

Both need the agreement session to open, which is why download() goes
through the same session, and why a Telegram link to one first shows the
agreement page in a fresh browser.
"""

import logging
import re

import requests

from config import BROWSER_UA, INDEX_TIMEOUT, PDF_TIMEOUT
from filing import Filing, IndexUnavailable

logger = logging.getLogger(__name__)

BASE_URL = "https://efdsearch.senate.gov"
HOME_URL = BASE_URL + "/search/home/"
SEARCH_URL = BASE_URL + "/search/"
DATA_URL = BASE_URL + "/search/report/data/"

REPORT_TYPE_PTR = "[11]"
PAGE_SIZE = 100

LINK_RE = re.compile(r'href="(/search/view/(ptr|paper)/([0-9a-f-]{36})/)"')
CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')

_session: requests.Session | None = None


def _open_session() -> requests.Session:
    """Accept the eFD agreement and return the authorised session."""
    session = requests.Session()
    # Like Nasdaq, eFD is only reliable with a browser User-Agent.
    session.headers["User-Agent"] = BROWSER_UA
    home = session.get(HOME_URL, timeout=INDEX_TIMEOUT)
    home.raise_for_status()
    token = CSRF_RE.search(home.text)
    if not token:
        raise ValueError("agreement form has no csrfmiddlewaretoken")
    agreed = session.post(
        HOME_URL,
        data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token.group(1)},
        headers={"Referer": HOME_URL},
        timeout=INDEX_TIMEOUT,
    )
    agreed.raise_for_status()
    if not agreed.url.rstrip("/").endswith("/search"):
        raise ValueError(f"agreement not accepted (landed on {agreed.url})")
    return session


def _display_name(first: str, last: str) -> tuple[str, str]:
    """('RICHARD ', 'BLUMENTHAL') -> ('Richard Blumenthal', 'Blumenthal');
    ('A. Mitchell', 'McConnell, Jr.') -> ('A. Mitchell McConnell, Jr.', 'McConnell')."""
    first, last = first.strip(), last.strip()
    if first.isupper() and last.isupper():
        first, last = first.title(), last.title()
    return f"{first} {last}".strip(), last.split(",")[0].strip()


def fetch_filings(year: int) -> list[Filing]:
    """All PTRs submitted to the Senate during `year`.

    Raises IndexUnavailable if the agreement or the search cannot be completed.
    """
    global _session
    rows: list[list] = []
    try:
        _session = _open_session()
        csrf = _session.cookies.get("csrftoken", "")
        while True:
            resp = _session.post(
                DATA_URL,
                data={
                    "start": len(rows),
                    "length": PAGE_SIZE,
                    "report_types": REPORT_TYPE_PTR,
                    "filer_types": "[]",
                    "submitted_start_date": f"01/01/{year} 00:00:00",
                    "submitted_end_date": f"12/31/{year} 23:59:59",
                    "candidate_state": "",
                    "senator_state": "",
                    "office_id": "",
                    "first_name": "",
                    "last_name": "",
                    "csrfmiddlewaretoken": csrf,
                },
                headers={"Referer": SEARCH_URL, "X-CSRFToken": csrf},
                timeout=INDEX_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("data")
            if not isinstance(batch, list):
                raise ValueError("response has no 'data' list")
            rows.extend(batch)
            if not batch or len(rows) >= int(body.get("recordsTotal", 0)):
                break
    except Exception as exc:
        raise IndexUnavailable(f"Could not read Senate eFD index: {exc}") from exc

    filings, skipped = [], 0
    for row in rows:
        try:
            first, last, office, link_html, date = row[:5]
            link = LINK_RE.search(link_html)
            if not link:
                skipped += 1
                continue
            member, last_name = _display_name(first, last)
            # office is "McConnell, A. Mitchell Jr. (Senator)", or bare "Senator"
            role = re.search(r"\(([^)]+)\)\s*$", office)
            filings.append(
                Filing(
                    doc_id=f"senate-{link.group(3)}",
                    source="senate",
                    member=member or "(unknown)",
                    last=last_name,
                    filing_date=date.strip(),
                    url=BASE_URL + link.group(1),
                    label="Senate PTR",
                    detail=(role.group(1) if role else office).strip(),
                    is_paper=link.group(2) == "paper",
                    link_note=(
                        "eFD shows its access agreement first; accept it, "
                        "then open the link again."
                    ),
                )
            )
        except Exception as exc:  # never let one bad row kill the run
            skipped += 1
            logger.warning("Skipping malformed Senate index row: %s", exc)

    logger.info(
        "Senate index: %d PTRs in %d (%d paper), %d skipped",
        len(filings), year, sum(f.is_paper for f in filings), skipped,
    )
    return filings


def download(filing: Filing) -> bytes | None:
    """Fetch a report page through the agreement session, re-agreeing once if
    the session has lapsed (eFD then redirects to the agreement page)."""
    global _session
    for attempt in (1, 2):
        try:
            if _session is None:
                _session = _open_session()
            resp = _session.get(filing.url, timeout=PDF_TIMEOUT)
            resp.raise_for_status()
            if resp.url.rstrip("/").endswith("/search/home"):
                _session = None
                continue
            return resp.content
        except Exception as exc:
            logger.error("Senate report download failed (%s, attempt %d): %s",
                         filing.url, attempt, exc)
            _session = None
    return None
