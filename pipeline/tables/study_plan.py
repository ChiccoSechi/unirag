"""Study plan (one PDF per academic year): study plans of the three tracks.

Structure (2023/24 – 2026/27): for each track a table for the 1st year and one for the 2nd year,
then "Tabella A" (related), "Tabella 1" and "Tabella 2" (core). Columns ANNO (only in tables
A/1/2), SEM, INSEGNAMENTO, SSD, TAF, CFU, ORE. Between years only details change, absorbed by the
engine: from 2024/25 the hours are split into "frontali" and "lab." on two header rows; in 2023/24
"INSEGNAMENTO" is split into two cells; some tables continue on the next page.
If a new year changed columns or rules, the check reports it and the table does not reach the bot.
"""

import re

from pipeline.tables.engine import Format, Table, check, read_tables

FORMAT = Format(
    headers={
        "ANNO": "Anno",
        "SEM": "Semestre",
        "INSEGNAMENTO": "Insegnamento",
        "SSD": "SSD",
        "TAF": "TAF",
        "CFU": "CFU",
        "ORE": "Ore",
        "OREFRONTALI": "Ore frontali",
        "ORELAB": "Ore laboratorio",
    },
    required=frozenset({"INSEGNAMENTO", "TAF", "CFU"}),
)
# Kind of educational activity (TAF) → how it is written in the sentences
TAF = {
    "CA": "caratterizzante",
    "AF": "affine",
    "ST": "a scelta dello studente",
    "FI": "prova finale",
    "AA": "altre attività",
}
CFU = re.compile(r"\d+(\s*\+\s*\d+)?")


def _row_check(c: dict[str, str]) -> str | None:
    if not c.get("Insegnamento"):
        return "missing subject"
    if c.get("TAF") not in TAF:
        return "unknown TAF"
    if not CFU.fullmatch(c.get("CFU", "")):
        return "invalid CFU"
    return None


def read(content: bytes, end: tuple[int, float] | None = None) -> list[Table]:
    tables = read_tables(content, FORMAT, end)
    check(tables, _row_check)
    return tables


def sentences(tables: list[Table], document: str) -> list[str]:
    result = []
    for t in tables:
        prefix = document + (f", {t.context}" if t.context else "")
        for c in t.fields():
            parts = [f"{c['Insegnamento']}:"]
            if c.get("Anno"):
                parts.append(f"{c['Anno']}° anno,")
            if c.get("Semestre"):
                parts.append(f"semestre {c['Semestre']},")
            parts.append(f"{c['CFU']} CFU, attività {TAF[c['TAF']]} ({c['TAF']}),")
            parts.append(f"settore {c.get('SSD') or '-'}")
            hours = [
                f"{k.lower()} {c[k]}"
                for k in ("Ore", "Ore frontali", "Ore laboratorio")
                if c.get(k) not in (None, "", "-")
            ]
            result.append(
                f"{prefix} — " + " ".join(parts) + ("; " + ", ".join(hours) if hours else "") + "."
            )
    return result
