"""Documents with one version per cohort (e.g. the 2025/26 and 2026/27 regulations): identical
parts are stored once, in the most recent document, with a link to the other one.

The comparison happens after extraction and before chunking (`pipeline/chunk.py`), section by
section (articles are matched by title, without the number) and table by table. It compares the
normalised text (`key`): academic years, article numbers, case, punctuation and spaces do not count.
The stored text stays the extracted one. Same PDFs, same decisions at every run; if UniCa changes a
shared part, at the next run it goes back into both documents.

Which documents: `merged_cohort_documents` in `pipeline/sources.yaml` (expressions on the title).
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

# Texts are already rewritten by `extract.normalise_years`: only the "2026/2027" form is left
ACADEMIC_YEAR = re.compile(r"\b20\d\d\s*/\s*20\d\d\b")
ARTICLE_NUMBER = re.compile(r"\bart\.?\s*\d+\b")

Sections = list[tuple[str, str]]  # (title, text), as `pipeline.chunk.sections`


def key(text: str) -> str:
    """The text reduced to what matters for the comparison: two texts with the same key say the
    same thing apart from academic year, article number, case, punctuation and spaces."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = ACADEMIC_YEAR.sub(" ", t)
    t = ARTICLE_NUMBER.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


@dataclass
class Merge:
    """What to do with a document of the pair. In the most recent one the shared parts are kept and
    marked (`other` = the other document); in the older one they are skipped."""

    other: dict
    recent: bool
    sections: set[int] = field(default_factory=set)  # indexes of the shared sections
    tables: set[int] = field(default_factory=set)  # indexes of the shared tables


def _by_title(sections: Sections) -> dict[tuple[str, int], int]:
    """(normalised title, times already seen) → index: also matches repeated titles."""
    seen: Counter[str] = Counter()
    result = {}
    for i, (title, _) in enumerate(sections):
        k = key(title)
        result[(k, seen[k])] = i
        seen[k] += 1
    return result


def compare(older: Sections, recent: Sections) -> list[tuple[int, int]]:
    """The pairs (index in the older, index in the recent) of sections with the same title and the
    same text, both normalised."""
    in_recent = _by_title(recent)
    pairs = []
    for place, i in _by_title(older).items():
        j = in_recent.get(place)
        if j is not None and key(older[i][1]) == key(recent[j][1]):
            pairs.append((i, j))
    return pairs


def _same_tables(older: dict, recent: dict) -> list[tuple[int, int]]:
    """The pairs of tables with the same normalised text (first free table for each)."""
    free = {j: key(t["markdown"]) for j, t in enumerate(recent.get("tables") or [])}
    pairs = []
    for i, t in enumerate(older.get("tables") or []):
        k = key(t["markdown"])
        j = next((j for j, kj in free.items() if kj == k), None)
        if j is not None:
            pairs.append((i, j))
            del free[j]
    return pairs


def merges(
    extracts: list[dict], rules: list[str], sections: Callable[[dict], Sections]
) -> dict[str, Merge]:
    """URL → what to do, for the two most recent documents of each rule (with different years)."""
    result: dict[str, Merge] = {}
    for rule in rules:
        same = sorted(
            (
                e
                for e in extracts
                if e["kind"] == "pdf" and e.get("year") and re.search(rule, e["title"])
            ),
            key=lambda e: e["year"],
        )
        if len(same) < 2 or same[-1]["year"] == same[-2]["year"]:
            continue
        older, recent = same[-2], same[-1]
        m_older = Merge(other=recent, recent=False)
        m_recent = Merge(other=older, recent=True)
        for i, j in compare(sections(older), sections(recent)):
            m_older.sections.add(i)
            m_recent.sections.add(j)
        for i, j in _same_tables(older, recent):
            m_older.tables.add(i)
            m_recent.tables.add(j)
        result[older["url"]] = m_older
        result[recent["url"]] = m_recent
    return result
