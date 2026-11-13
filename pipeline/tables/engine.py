"""Common part of the table readers: the geometry of pdfplumber's cells, without the rules of single
documents (those are in each document's file).

- Logical columns come from the header cells: each data cell goes to the column covering the centre
  of its text, so empty spacing columns disappear by themselves. If the text falls on the border
  between two columns, the nearest header wins.
- Headers on several rows ("ORE" + "frontali") and split words ("I" + "NSEGNAMENTO") are joined.
- Vertically merged cells (pdfplumber gives `None` under the first one) are filled from above.
- Rows with a single filled cell ("2026/2027", "Tabella A – …") are titles, not data.
- A table without a header (continuing from the previous page) reuses the previous columns.
- The text above the table and the title rows give the context (track, year, title).
- The common check: no value in the table's area of the PDF may get lost.
"""

import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import pdfplumber

# (cell box or None if covered by a merged cell, text, x centre of the text)
Cell = tuple[tuple[float, float, float, float] | None, str, float]


@dataclass
class Column:
    name: str
    x0: float
    x1: float
    text_centre: float


@dataclass
class Context:
    track: str = ""  # e.g. "indirizzo Cloud And Security"
    course_year: str = ""  # e.g. "1° ANNO 2026/2027"
    title: str = ""  # e.g. "Tabella 1 – CARATTERIZZANTI"

    def description(self) -> str:
        return ", ".join(x for x in (self.track, self.course_year, self.title) if x)


@dataclass
class Table:
    pdf_page: int
    context: str
    headers: list[str]
    rows: list[list[str]]
    pdf_text: str  # text of the table's area in the PDF, for the check
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems

    def fields(self) -> list[dict[str, str]]:
        return [dict(zip(self.headers, r, strict=False)) for r in self.rows]


@dataclass(frozen=True)
class Format:
    """What sets apart the tables of a document: the known headers (compact form → name) and which
    of them must all be there for a table to be of that kind."""

    headers: dict[str, str]
    required: frozenset[str]

    def recognises(self, names: list[str]) -> bool:
        return self.required <= {_compact(n) for n in names}


def text(x: str | None) -> str:
    return " ".join((x or "").split())


def _compact(x: str) -> str:
    return re.sub(r"[\s.]", "", x).upper()


def _is_data(t: str) -> bool:
    """A cell that looks like data (number, semester, "1 e 2", "-") and not like a header."""
    return bool(re.fullmatch(r"\d+(\s*(e|\+|,)\s*\d+)*°?|-", t))


def _rows(table, words: list[dict]) -> list[list[Cell]]:
    """Cells with the centre of their text: sometimes the drawn cell overflows into the next
    column, while the text stays under its header."""
    rows = []
    for row, values in zip(table.rows, table.extract(), strict=True):
        cells = []
        for cell, v in zip(row.cells, values, strict=True):
            centre = 0.0
            if cell is not None:
                inside = [
                    w for w in words
                    if cell[0] <= (w["x0"] + w["x1"]) / 2 <= cell[2]
                    and cell[1] <= (w["top"] + w["bottom"]) / 2 <= cell[3]
                ]  # fmt: skip
                centre = (
                    (min(w["x0"] for w in inside) + max(w["x1"] for w in inside)) / 2
                    if inside
                    else (cell[0] + cell[2]) / 2
                )
            cells.append((cell, text(v), centre))
        rows.append(cells)
    return rows


def _title_row(row: list[Cell]) -> bool:
    filled = [t for _, t, _ in row if t]
    return len(filled) == 1 and not _is_data(filled[0])


def _header(rows: list[list[Cell]]) -> tuple[list[Column], int] | None:
    """Logical columns and number of header rows (at most 3, no data)."""
    n = 0
    while n < min(len(rows), 3):
        filled = [t for _, t, _ in rows[n] if t]
        if not filled or any(_is_data(t) for t in filled):
            break
        n += 1
    if n == 0:
        return None
    cells = [(c, t, x) for row in rows[:n] for c, t, x in row if c is not None and t]
    # Leaves: header cells that do not contain the centre of a narrower cell
    leaves = []
    for c, _, x in cells:
        inside = [
            d for d, _, _ in cells if d is not c and c[0] - 1 <= (d[0] + d[2]) / 2 <= c[2] + 1
        ]
        if not any(d[2] - d[0] < c[2] - c[0] for d in inside):
            leaves.append((c, x))
    columns: list[Column] = []
    for leaf, x in sorted(leaves, key=lambda v: v[0][0]):
        centre = (leaf[0] + leaf[2]) / 2
        if columns and columns[-1].x0 - 1 <= centre <= columns[-1].x1 + 1:
            continue  # same column, on another header row
        parts = [t for c, t, _ in cells if c[0] - 1 <= centre <= c[2] + 1]
        columns.append(Column(" ".join(parts), leaf[0], leaf[2], x))
    return columns, n


def _normalise(columns: list[Column], format: Format) -> list[Column]:
    """Known names; a column with a fragment ("I") is joined to the next one."""
    names = format.headers
    result: list[Column] = []
    for col in columns:
        joined = _compact(result[-1].name + col.name) if result else ""
        if result and _compact(col.name) not in names and joined in names:
            before = result[-1]
            result[-1] = Column(before.name + col.name, before.x0, col.x1, col.text_centre)
        else:
            result.append(Column(col.name, col.x0, col.x1, col.text_centre))
    for col in result:
        compact = _compact(col.name)
        if compact in names:
            col.name = names[compact]
    return result


def _assign(row: list[Cell], columns: list[Column]) -> list[str | None]:
    """Values of the row per logical column; None if the column is covered by a merged cell."""
    values: list[str | None] = [""] * len(columns)
    for cell, t, x in row:
        if cell is None or not t:
            continue
        candidates = [i for i, c in enumerate(columns) if c.x0 - 3 <= x <= c.x1 + 3]
        if not candidates:
            continue
        i = min(candidates, key=lambda i: abs(x - columns[i].text_centre))
        values[i] = f"{values[i]} {t}".strip()
    for i, col in enumerate(columns):
        centre = (col.x0 + col.x1) / 2
        if not any(c is not None and c[0] - 1 <= centre <= c[2] + 1 for c, _, _ in row):
            values[i] = None
    return values


COURSE_YEAR = re.compile(r"\b\d°\s*ANNO\b.*", re.I)
# "INDIRIZZO CLOUD AND SECURITY" (from 2024/25) or "Percorso CLOUD AND SECURITY" (2023/24): name in
# capitals, so that it does not match sentences of the text ("del percorso “Cloud and Security”")
TRACK = re.compile(r"^(?:INDIRIZZO|Percorso)\s+([A-Z][A-Z &]+)$")
TABLE_TITLE = re.compile(r"^Tabella\s+\w+\s*[–-].*", re.I)


def update_context(context: Context, t: str, free_title: bool = False) -> None:
    """Track, course year and title from the text; with `free_title` any other line is a title."""
    for line in (r.strip() for r in t.splitlines()):
        if m := TRACK.match(line):
            context.track = "indirizzo " + m.group(1).title()
            context.course_year = context.title = ""
        elif m := COURSE_YEAR.search(line):
            context.course_year, context.title = m.group(0), ""
        elif TABLE_TITLE.match(line):
            context.title, context.course_year = line, ""
        elif free_title and line:
            context.title = line


def _zone_text(page, top: float, bottom: float) -> str:
    return page.crop((0, top, page.width, bottom)).extract_text() or ""


def _build(
    number: int,
    context: Context,
    columns: list[Column],
    rows: list[list[Cell]],
    row_texts: list[str],
    pdf_text: str,
) -> Table:
    data: list[list[str]] = []
    for row, row_text in zip(rows, row_texts, strict=True):
        if _title_row(row):
            update_context(context, text(row_text), free_title=True)
            continue
        values = _assign(row, columns)
        if not any(values):
            continue
        # Leftover of a merged cell (a single cell, the rest covered from above): not a row
        if data and sum(v is None for v in values) > len(values) / 2:
            continue
        above = data[-1] if data else [""] * len(columns)
        data.append([above[i] if v is None else v for i, v in enumerate(values)])
    return Table(number, context.description(), [c.name for c in columns], data, pdf_text)


def read_tables(
    content: bytes, format: Format, end: tuple[int, float] | None = None
) -> list[Table]:
    """The tables of the format (with their continuations), with their context.

    `end` = (page, y): tables from there on are ignored (e.g. the regulation's annexes)."""
    result: list[Table] = []
    context = Context()
    previous: list[Column] | None = None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            above = 0.0
            words = page.extract_words()
            for table in page.find_tables():
                if end and (number, table.bbox[1]) >= end:
                    return result
                rows = _rows(table, words)
                t = 0  # title rows at the top: context, read from the page (the cell cuts it)
                while t < len(rows) - 1 and _title_row(rows[t]):
                    t += 1
                start = table.rows[t].bbox[1]
                update_context(context, _zone_text(page, above, start))
                above = table.bbox[3]
                rows, bands = rows[t:], [r.bbox for r in table.rows[t:]]
                found = _header(rows)
                columns = _normalise(found[0], format) if found else []
                if found and format.recognises([c.name for c in columns]):
                    n = found[1]
                elif previous:  # continues the previous table
                    columns, n = previous, 0
                else:
                    continue
                previous = columns
                if n == len(rows):  # only the header at the bottom of the page: data comes next
                    continue
                row_texts = [_zone_text(page, b[1], b[3]) for b in bands[n:]]
                pdf_text = _zone_text(page, start, table.bbox[3])
                result.append(_build(number, context, columns, rows[n:], row_texts, pdf_text))
    return result


def read_pairs(content: bytes, end: tuple[int, float] | None = None) -> list[Table]:
    """Two-column tables item → value (e.g. the course data at the top of the regulation)."""
    result = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            for table in page.find_tables():
                if end and (number, table.bbox[1]) >= end:
                    return result
                rows = [[text(v) for v in r if v is not None] for r in table.extract()]
                if not rows or any(len(r) != 2 for r in rows):
                    continue
                pairs = [r for r in rows if any(r)]
                pdf_text = page.crop(table.bbox).extract_text() or ""
                result.append(Table(number, "", ["Voce", "Valore"], pairs, pdf_text))
    return result


MEANINGFUL = re.compile(r"[A-Za-zÀ-ÿ]{4,}|\d+(?:[.,:/\-]\d+)*|[A-Z]{2,}[\w/\-]*")


def lost_values(t: Table) -> list[str]:
    """Words and numbers of the table's area in the PDF that do not appear in the result."""
    got = _compact(" ".join([t.context, *t.headers, *(c for r in t.rows for c in r)]))
    return sorted({v for v in MEANINGFUL.findall(t.pdf_text) if _compact(v) not in got})


def check(tables: list[Table], row_check: Callable[[dict[str, str]], str | None]) -> None:
    """Common check (lost values, rows present) plus the document's own, row by row."""
    for t in tables:
        t.problems = []
        if lost := lost_values(t):
            t.problems.append(f"PDF values lost: {len(lost)}")  # how many, never the PDF text
        if not t.rows:
            t.problems.append("no rows")
        for n, fields in enumerate(t.fields(), start=1):
            if error := row_check(fields):
                t.problems.append(f"row {n}: {error}")


def markdown(t: Table) -> str:
    lines = [f"**{t.context}**", ""] if t.context else []
    lines.append("| " + " | ".join(t.headers) + " |")
    lines.append("|" + "---|" * len(t.headers))
    lines += ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in t.rows]
    return "\n".join(lines)
