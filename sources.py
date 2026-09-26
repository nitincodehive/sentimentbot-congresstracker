"""
The disclosure sources, as uniform (index, download, parse) triples.

main.py runs the same reconcile -> queue -> download -> parse -> notify loop
for each one; nothing downstream of this table knows which source a filing
came from. A new source is a new index module, a parser, and a row here.

Every document goes through its parser. None is assumed unparseable from
who filed it: a filing whose document yields no rows is still recorded and
still notified with its link, whatever the source.
"""

import io
import logging
from dataclasses import dataclass
from typing import Callable

import requests

import house_index
import oge_index
import senate_index
from config import PDF_TIMEOUT, USER_AGENT
from filing import Filing
from oge_parser import parse_278t
from ptr_parser import parse_ptr
from senate_parser import parse_senate_ptr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    name: str
    fetch_filings: Callable[[int], list[Filing]]  # raises IndexUnavailable
    download: Callable[[Filing], bytes | None]   # None on failure
    parse: Callable[[bytes], list[dict]]         # [] when unparseable


def download_pdf(filing: Filing) -> bytes | None:
    try:
        resp = requests.get(
            filing.url, headers={"User-Agent": USER_AGENT}, timeout=PDF_TIMEOUT
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.error("PDF download failed (%s): %s", filing.url, exc)
        return None


SOURCES = {
    "house": Source(
        "house", house_index.fetch_filings, download_pdf,
        lambda content: parse_ptr(io.BytesIO(content)),
    ),
    "senate": Source(
        "senate", senate_index.fetch_filings, senate_index.download,
        parse_senate_ptr,
    ),
    "oge": Source(
        "oge", oge_index.fetch_filings, download_pdf,
        lambda content: parse_278t(io.BytesIO(content)),
    ),
}
