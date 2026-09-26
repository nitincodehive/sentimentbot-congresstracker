"""
OGE (Office of Government Ethics) executive-branch disclosure index.

The President, Vice President, Cabinet and other presidentially appointed
officials report trades on OGE Form 278-T. OGE's public search page
    https://www.oge.gov/web/oge.nsf/Officials%20Individual%20Disclosures%20Search%20Collection
is a DataTables front end over a JSON endpoint on extapps2.oge.gov, which is
what this module reads. The endpoint:

  - requires the full DataTables parameter set (columns[i][...] for all six
    columns) and returns HTTP 400 without it
  - ignores search[value]; filtering happens here
  - sorts server-side, so ordering by column 0 (docDate) descending gives
    newest first, and paging stops once a page reaches the previous year

Each row's `type` field is HTML. A directly downloadable document carries an
<a href=".../PAS+Index/<32-hex UNID>/$FILE/<name>.pdf">; most 278-Ts instead
say "Request this Document" and are released only on a manual OGE Form 201
request, which cannot and should not be automated. Only direct links are
tracked. In 2026 that is roughly 1 in 10 278-Ts, but it covers the President
and most Cabinet secretaries.

The UNID in the URL is stable per document and becomes the doc_id.
"""

import logging
import re
from datetime import datetime

import requests

from config import INDEX_TIMEOUT, USER_AGENT
from filing import Filing, IndexUnavailable

logger = logging.getLogger(__name__)

API_URL = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest"
COLUMNS = ("docDate", "title", "type", "name", "agency", "level")
PAGE_SIZE = 1000   # ~4 months of OGE postings per page in 2026
MAX_PAGES = 10

TRANSACTION_TYPE = "278 Transaction"
PDF_LINK_RE = re.compile(r"href='([^']*/PAS\+Index/([0-9A-Fa-f]{32})/\$FILE/[^']+)'")


def _page(start: int) -> list[dict]:
    params = {
        "draw": 1,
        "start": start,
        "length": PAGE_SIZE,
        "order[0][column]": 0,
        "order[0][dir]": "desc",
        "search[value]": "",
        "search[regex]": "false",
    }
    for i, col in enumerate(COLUMNS):
        params.update({
            f"columns[{i}][data]": col,
            f"columns[{i}][name]": "",
            f"columns[{i}][searchable]": "true",
            f"columns[{i}][orderable]": "true",
            f"columns[{i}][search][value]": "",
            f"columns[{i}][search][regex]": "false",
        })
    resp = requests.get(
        API_URL, params=params, headers={"User-Agent": USER_AGENT},
        timeout=INDEX_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("data")
    if not isinstance(data, list):
        raise ValueError("response has no 'data' list")
    return data


def _display_name(name: str) -> tuple[str, str]:
    """'Trump, Donald J' -> ('Donald J Trump', 'Trump')."""
    last, _, first = (p.strip() for p in name.partition(","))
    return (f"{first} {last}".strip() or name.strip(), last)


def fetch_filings(year: int) -> list[Filing]:
    """Directly downloadable 278-Ts posted during `year`, newest first.

    Raises IndexUnavailable if the API cannot be read at all. Zero matching
    filings is not an error: early in a year there may be none yet.
    """
    rows: list[dict] = []
    try:
        for page in range(MAX_PAGES):
            batch = _page(page * PAGE_SIZE)
            rows.extend(batch)
            if len(batch) < PAGE_SIZE or batch[-1].get("docDate", "")[:4] < str(year):
                break
        else:
            logger.warning("OGE: stopped after %d pages without reaching %d", MAX_PAGES, year - 1)
    except Exception as exc:
        raise IndexUnavailable(f"Could not read OGE disclosure API: {exc}") from exc
    if not rows:
        raise IndexUnavailable("OGE disclosure API returned no documents at all")

    filings, request_only = [], 0
    for row in rows:
        posted = row.get("docDate", "")
        if posted[:4] != str(year) or TRANSACTION_TYPE not in row.get("type", ""):
            continue
        link = PDF_LINK_RE.search(row["type"])
        if not link:
            request_only += 1
            continue
        try:
            d = datetime.strptime(posted[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning("OGE: skipping row with bad docDate %r", posted)
            continue
        member, last = _display_name(row.get("name", ""))
        detail = ", ".join(
            p for p in (row.get("title", "").strip(), row.get("agency", "").strip()) if p
        )
        filings.append(
            Filing(
                doc_id=f"oge-{link.group(2).upper()}",
                source="oge",
                member=member or "(unknown)",
                last=last,
                # Date OGE posted it, not the date the filer signed it; OGE
                # typically posts one to three weeks after signature.
                filing_date=f"{d.month}/{d.day}/{d.year}",
                url=link.group(1),
                label="OGE 278-T",
                detail=detail,
            )
        )

    logger.info(
        "OGE index: %d documents read, %d downloadable 278-Ts in %d "
        "(%d more available only by manual request)",
        len(rows), len(filings), year, request_only,
    )
    return filings
