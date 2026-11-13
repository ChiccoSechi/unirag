"""List of the optional elective exams (one PDF per academic year): exams of other courses.

Structure (2026/27): columns CdS, Insegnamento, Docenti, CFU, Semestre; the degree course is a cell
merged over several rows (the engine repeats it on each).
"""

import re

from pipeline.tables.engine import Format, Table, check, read_tables

FORMAT = Format(
    headers={
        "CDS": "CdS",
        "INSEGNAMENTO": "Insegnamento",
        "DOCENTI": "Docenti",
        "CFU": "CFU",
        "SEMESTRE": "Semestre",
    },
    required=frozenset({"CDS", "INSEGNAMENTO", "CFU"}),
)


def _row_check(c: dict[str, str]) -> str | None:
    if not (c.get("CdS") and c.get("Insegnamento")):
        return "missing course or subject"
    if not re.fullmatch(r"\d+", c.get("CFU", "")):
        return "invalid CFU"
    return None


def read(content: bytes) -> list[Table]:
    tables = read_tables(content, FORMAT)
    check(tables, _row_check)
    return tables


def sentences(tables: list[Table], document: str) -> list[str]:
    return [
        f"{document} — esame a scelta: {c['Insegnamento']} (corso di studi {c['CdS']}"
        + (f", docenti {c['Docenti']}" if c.get("Docenti") else "")
        + f"), {c['CFU']} CFU, semestre {c.get('Semestre') or 'non indicato'}."
        for t in tables
        for c in t.fields()
    ]
