"""
House Clerk financial-disclosure index.

Source of record is the year-to-date archive ZIP:
    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip

The ZIP contains {YEAR}FD.xml, a <FinancialDisclosure> document with one
<Member> element per filing. Verified live schema (fields, in order):
    Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate, DocID

FilingType 'P' is a Periodic Transaction Report — the only type we track.

Individual PTR PDFs live at:
    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf
"""

import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from config import INDEX_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

FILING_TYPE_PTR = "P"


@dataclass
class Filing:
    doc_id: str
    member: str
    last: str
    state_dst: str
    filing_date: str  # as published, M/D/YYYY
    year: str

    @property
    def pdf_url(self) -> str:
        return PDF_URL.format(year=self.year, doc_id=self.doc_id)

    @property
    def is_scanned_legacy(self) -> bool:
        """Legacy paper filings have short numeric DocIDs (e.g. 9116142) and
        are image-only scans with no text layer — they will not parse."""
        return len(self.doc_id) < 8


class IndexUnavailable(RuntimeError):
    """Raised when the index itself cannot be fetched — the only fatal error."""


def fetch_filings(year: int) -> list[Filing]:
    """Download and parse the year-to-date index. Returns PTR filings only.

    Raises IndexUnavailable on any failure — this is the one condition that
    aborts a run, since without the index there is nothing to reconcile.
    """
    url = INDEX_URL.format(year=year)
    logger.info("Fetching index: %s", url)
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=INDEX_TIMEOUT
        )
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise IndexUnavailable(f"No XML inside {url} (got {zf.namelist()})")
        raw = zf.read(xml_names[0]).decode("utf-8-sig")
        root = ET.fromstring(raw)
    except IndexUnavailable:
        raise
    except Exception as exc:
        raise IndexUnavailable(f"Could not fetch/parse index {url}: {exc}") from exc

    filings, skipped = [], 0
    for el in root:
        try:
            if (el.findtext("FilingType") or "").strip() != FILING_TYPE_PTR:
                continue
            doc_id = (el.findtext("DocID") or "").strip()
            if not doc_id:
                skipped += 1
                continue
            first = (el.findtext("First") or "").strip()
            last = (el.findtext("Last") or "").strip()
            suffix = (el.findtext("Suffix") or "").strip()
            member = " ".join(p for p in (first, last, suffix) if p)
            filings.append(
                Filing(
                    doc_id=doc_id,
                    member=member or last or "(unknown)",
                    last=last,
                    state_dst=(el.findtext("StateDst") or "").strip(),
                    filing_date=(el.findtext("FilingDate") or "").strip(),
                    year=(el.findtext("Year") or str(year)).strip(),
                )
            )
        except Exception as exc:  # never let one bad element kill the run
            skipped += 1
            logger.warning("Skipping malformed index entry: %s", exc)

    logger.info(
        "Index: %d total entries, %d PTR filings, %d skipped",
        len(root), len(filings), skipped,
    )
    if not filings:
        raise IndexUnavailable(
            f"Index parsed but contained zero FilingType='P' entries — "
            f"the schema may have changed. Root tag was <{root.tag}>."
        )
    return filings
