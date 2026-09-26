"""
The source-independent filing record, and the one fatal-per-source error.

Every source module (house_index, senate_index, oge_index) turns its index
into a list of Filing objects, so everything downstream of the index —
reconcile, queue, parse, notify, Sheets — handles all sources the same way.

doc_id is the dedup key across the whole filings tab. House doc_ids are the
bare Clerk DocID (unchanged from before multi-source support, so existing
rows still match); other sources are prefixed "<source>-" so they can never
collide. source_of() recovers the source from a doc_id alone, which is why
the filings tab needs no separate source column.
"""

from dataclasses import dataclass


@dataclass
class Filing:
    doc_id: str
    source: str        # "house" | "senate" | "oge"
    member: str        # display name, "First Last"
    last: str          # last name, for PRIORITY_MEMBERS matching
    filing_date: str   # M/D/YYYY or MM/DD/YYYY; formatter.parse_date reads both
    url: str           # the document itself: a PDF, or a Senate report page
    label: str         # report kind shown in messages, e.g. "House PTR"
    detail: str = ""   # shown after the name: district, office or position
    is_paper: bool = False  # known image-only filing; will not parse
    link_note: str = ""     # shown under the link, e.g. access caveats

    @property
    def link_text(self) -> str:
        return "Open PDF" if self.url.lower().endswith(".pdf") else "Open report"


class IndexUnavailable(RuntimeError):
    """A source's index could not be fetched. That source is skipped for the
    run; the other sources still run."""


def source_of(doc_id: str) -> str:
    prefix, sep, _ = doc_id.partition("-")
    return prefix if sep and prefix in ("senate", "oge") else "house"
