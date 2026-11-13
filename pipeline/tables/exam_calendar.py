"""Exam calendar (one PDF per academic year): exam dates and board for each subject.

Structure (2026/27): columns Anno, Codice, Insegnamento, Commissione (Titolare, Membro on a second
header row) and one column per session month ("GEN '27"); the cells hold the days, even more than
one ("11 25", "15 e 29"). The months are read from the header: if they change, they are followed.
"""

import re

from pipeline.tables.engine import Format, Table, check, read_tables

FORMAT = Format(
    headers={
        "ANNO": "Anno",
        "CODICE": "Codice",
        "INSEGNAMENTO": "Insegnamento",
        "COMMISSIONETITOLARE": "Commissione titolare",
        "COMMISSIONEMEMBRO": "Commissione membro",
    },
    required=frozenset({"CODICE", "INSEGNAMENTO"}),
)
MONTH = re.compile(r"^(GEN|FEB|MAR|APR|MAG|GIU|LUG|AGO|SET|OTT|NOV|DIC)\s*'?\s*(\d{2})$", re.I)
MONTHS = {
    "GEN": "gennaio", "FEB": "febbraio", "MAR": "marzo", "APR": "aprile", "MAG": "maggio",
    "GIU": "giugno", "LUG": "luglio", "AGO": "agosto", "SET": "settembre", "OTT": "ottobre",
    "NOV": "novembre", "DIC": "dicembre",
}  # fmt: skip


def _row_check(c: dict[str, str]) -> str | None:
    if not c.get("Insegnamento"):
        return "missing subject"
    for name, value in c.items():
        if (
            MONTH.match(name)
            and value
            and not re.fullmatch(r"\d{1,2}((\s+|\s*e\s*)\d{1,2})*", value)
        ):
            return f"invalid day in {name}"  # the month column
    return None


def read(content: bytes) -> list[Table]:
    tables = read_tables(content, FORMAT)
    check(tables, _row_check)
    return tables


def exam_dates(c: dict[str, str]) -> list[str]:
    """The exam dates of the row, e.g. "18 gennaio 2027"."""
    dates = []
    for name, value in c.items():
        if m := MONTH.match(name):
            month = f"{MONTHS[m.group(1).upper()]} 20{m.group(2)}"
            dates += [f"{day} {month}" for day in re.findall(r"\d+", value)]
    return dates


def sentences(tables: list[Table], document: str) -> list[str]:
    result = []
    for t in tables:
        for c in t.fields():
            board = ", ".join(
                f"{c[k]} ({k.split()[1]})"
                for k in ("Commissione titolare", "Commissione membro")
                if c.get(k)
            )
            result.append(
                f"{document} — {c['Insegnamento']} ({c.get('Codice')}, {c.get('Anno')}° anno): "
                f"appelli {', '.join(exam_dates(c)) or 'non indicati'}"
                + (f"; commissione {board}" if board else "")
                + "."
            )
    return result
