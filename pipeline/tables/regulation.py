"""Course regulation: only the course data, the item → value table at the top.

The annexes are not read (decided on 9 October 2026): the study plan has its own PDF
(`study_plan.py`) and the Tuning matrix is excluded. The annexes start at the line "Allegato 1";
`end_of_text` also tells where to cut the regulation's text.
"""

import io
import re

import pdfplumber

from pipeline.tables.engine import Table, check, read_pairs

# The annex's title, not its entry in the index (which ends with dots and the page)
ANNEXES = re.compile(r"^\s*Allegato\s+1\b(?!.*\.{5})", re.I)


def end_of_text(content: bytes) -> tuple[int, float] | None:
    """(page, y) of the "Allegato 1" line: from there on the document is not used."""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            for line in page.extract_text_lines():
                if ANNEXES.match(line["text"]):
                    return number, line["top"]
    return None


def _row_check(c: dict[str, str]) -> str | None:
    return None if c.get("Voce") and c.get("Valore") else "missing item or value"


def read(content: bytes) -> list[Table]:
    tables = read_pairs(content, end_of_text(content))
    check(tables, _row_check)
    return tables


def sentences(tables: list[Table], document: str) -> list[str]:
    return [f"{document} — {c['Voce']}: {c['Valore']}." for t in tables for c in t.fields()]
