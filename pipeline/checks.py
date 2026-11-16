"""Checks on the real data of a run, before anything is written to Vectorize and D1.

- Personal data: an email address (any domain, also written "[at]") or a phone number left in a
  chunk or in a whole table means the extraction let something slip: the run stops. The patterns
  are looser than those that remove contacts (`extract.py`), so they catch what those missed.
- Silent losses: a source that had at least `MIN_CHUNKS` chunks and now has less than
  `MIN_SHARE` of them, or a PDF that had tables and now has none, probably changed on the site in
  a way the extraction does not see: only a warning, the run goes on.

Logs and errors hold only source codes and the kind of problem, never the text.
"""

import re
from collections.abc import Iterable
from typing import Protocol

from pipeline.extract import not_a_phone

EMAIL = re.compile(r"[\w.%+-]+\s?(?:@|\[at\]|\(at\))\s?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)
PHONE = re.compile(
    r"(?<![\w/.,-])(?:\+39|0039)?[\s./-]?\(?(?:0\d{1,3}|3\d{2})\)?(?:[\s./-]?\d){6,8}(?![\w/])"
)

MIN_CHUNKS = 5
MIN_SHARE = 0.3


class PersonalDataFound(RuntimeError):
    """Personal data in the texts about to be written. The message holds only codes and kinds."""


class Source(Protocol):
    code: str
    extract: dict
    chunks: list


def _kinds(text: str) -> set[str]:
    kinds = {"email"} if EMAIL.search(text) else set()
    if any(not not_a_phone(text, m) for m in PHONE.finditer(text)):  # 09.00-11.00 is not a phone
        kinds.add("phone")
    return kinds


def personal_data(sources: Iterable[Source]) -> list[str]:
    """ "code: kind" for each source with an email or a phone number in a chunk or a table."""
    found = []
    for s in sources:
        texts = [c.text for c in s.chunks] + [t["markdown"] for t in s.extract.get("tables") or []]
        found += [f"{s.code}: {kind}" for kind in sorted(set().union(*map(_kinds, texts)))]
    return found


def losses(
    today: dict[str, Source], registry: dict[str, dict], tables: dict[str, int]
) -> list[str]:
    """Warnings for sources that lost most of their chunks, or all their tables, since last run.
    `registry`: the rows of `sources` in D1; `tables`: source code → tables in D1."""
    warnings = []
    for code, s in sorted(today.items()):
        before = int((registry.get(code) or {}).get("chunk_count") or 0)
        if before >= MIN_CHUNKS and len(s.chunks) < MIN_SHARE * before:
            warnings.append(f"{code}: {len(s.chunks)} chunks instead of {before}")
        if tables.get(code) and not s.extract.get("tables"):
            warnings.append(f"{code}: no tables instead of {tables[code]}")
    return warnings
