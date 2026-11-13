"""Lesson timetable from the PDF, without an LLM: the geometry of the cells tells each lesson's day.

In the PDF some days have several columns (parallel lessons) under a single header ("Lun"). The
vision models tried (Gemma 4, Mistral Small 3.1, Llama 4 Scout, Qwen3.8) find all the codes but
put them on the wrong day; pdfplumber instead knows where each cell is: each code goes to the day
whose header covers it.

Places: the colour of each cell tells where the lesson is, according to the legend "Localizzazione
delle lezioni" under the grid (coloured rectangles with the place's name). Colours and names are
read from the PDF every time. A cell whose colour is not in the legend gets a placeholder pointing
to the timetable (like "[email sul sito]"); coloured cells without a places legend = changed
structure, the run stops.

Try it (with the other readers): uv run python -m pipeline.tables
"""

import io
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pdfplumber

log = logging.getLogger("pipeline.tables.timetable")

DAYS = {
    "lun": "lunedì",
    "mar": "martedì",
    "mer": "mercoledì",
    "gio": "giovedì",
    "ven": "venerdì",
    "sab": "sabato",
}
TIME = re.compile(r"^\d{1,2}[:.]\d{2}$")


def _hhmm(time: str) -> str:
    """ "9.00" → "09:00": always two digits, so that times sort in the right order."""
    hours, minutes = re.split(r"[:.]", time)
    return f"{int(hours):02d}:{minutes}"


PLACES_LEGEND = "Localizzazione"  # first word of the title of the places legend
COLOUR_TOLERANCE = 0.03  # the same colour varies by ~0.004 between cells of the PDF
PLACE_TO_CHECK = "[luogo da verificare sull'orario in PDF o sull'Agenda web]"
ROOMS_WARNING = "Le aule possono variare: verificare sull'Agenda web di UniCa."
CODE = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")

Colour = tuple[float, float, float]


@dataclass(frozen=True)
class Lesson:
    day: str  # "lunedì"
    start: str  # "09:00"
    end: str  # "10:40"
    code: str  # "IR"
    place: str = ""  # from the colour legend; "" if the cell is not coloured
    colour: Colour | None = None  # colour of the cell (RGB)


def _centre(cell: tuple[float, float, float, float]) -> float:
    return (cell[0] + cell[2]) / 2


def _rgb(colour) -> Colour | None:
    """pdfplumber's fill colour as RGB: grey (1 value), RGB (3), CMYK (4)."""
    if not isinstance(colour, tuple | list):
        return None
    if len(colour) == 1:
        return (float(colour[0]),) * 3
    if len(colour) == 3:
        return tuple(float(c) for c in colour)
    if len(colour) == 4:
        c, m, y, k = (float(v) for v in colour)
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _distance(a: Colour, b: Colour) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def coloured_rects(page) -> list[tuple[dict, Colour]]:
    """Filled coloured rectangles: no black (borders drawn as rectangles) nor white."""
    result = []
    for r in page.rects:
        colour = _rgb(r.get("non_stroking_color")) if r.get("fill") else None
        if colour is not None and 0.05 < sum(colour) / 3 < 0.98:
            result.append((r, colour))
    return result


def colour_under(x: float, y: float, rects: list[tuple[dict, Colour]]) -> Colour | None:
    """Colour of the smallest rectangle containing the point (x, y)."""
    under = [(r, c) for r, c in rects if r["x0"] <= x <= r["x1"] and r["top"] <= y <= r["bottom"]]
    if not under:
        return None
    return min(
        under, key=lambda rc: (rc[0]["x1"] - rc[0]["x0"]) * (rc[0]["bottom"] - rc[0]["top"])
    )[1]


def legend_places(page, rects: list[tuple[dict, Colour]]) -> dict[Colour, str]:
    """Colour → place, from the coloured rectangles under the places legend title."""
    words = page.extract_words()
    title = next((w for w in words if w["text"] == PLACES_LEGEND), None)
    if title is None:
        return {}
    places: dict[Colour, str] = {}
    for r, colour in sorted(rects, key=lambda rc: rc[0]["top"]):
        if r["top"] < title["bottom"] - 1 or any(
            _distance(colour, c) <= COLOUR_TOLERANCE for c in places
        ):
            continue
        # words with their centre inside the rectangle (the text may stick out a little)
        inside = [
            w["text"]
            for w in words
            if r["x0"] <= (w["x0"] + w["x1"]) / 2 <= r["x1"]
            and r["top"] <= (w["top"] + w["bottom"]) / 2 <= r["bottom"]
        ]
        if inside:
            places[colour] = " ".join(inside)
    return places


def place(colour: Colour | None, places: dict[Colour, str]) -> str:
    """The place of the nearest legend colour, only if almost equal; otherwise the placeholder."""
    if colour is None or not places:
        return ""
    nearest = min(places, key=lambda c: _distance(c, colour))
    return places[nearest] if _distance(nearest, colour) <= COLOUR_TOLERANCE else PLACE_TO_CHECK


def lessons_of_table(table, places: dict[Colour, str] | None = None) -> list[Lesson]:
    """The lessons of a grid: each cell's day is the header covering it, its place the colour."""
    rects = coloured_rects(table.page)
    rows = table.rows
    texts = table.extract()
    days: list[tuple[float, float, str]] = []  # (start x, end x, day)
    for row, values in zip(rows, texts, strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            key = (value or "").strip().lower()[:3]
            if cell is not None and key in DAYS:
                days.append((cell[0], cell[2], DAYS[key]))
        if days:
            break
    if not days:
        return []
    lessons = []
    for row, values in zip(rows, texts, strict=True):
        times = [(v or "").strip() for v in values if TIME.match((v or "").strip())]
        if len(times) < 2:
            continue
        for cell, value in zip(row.cells, values, strict=True):
            code = (value or "").strip()
            if cell is None or not CODE.match(code):
                continue
            x = _centre(cell)
            day = next((d for start, end, d in days if start - 1 <= x <= end + 1), None)
            if day:
                colour = colour_under(x, (cell[1] + cell[3]) / 2, rects)
                start, end = _hhmm(times[0]), _hhmm(times[1])
                lessons.append(Lesson(day, start, end, code, place(colour, places or {}), colour))
    return lessons


def legend(tables: list[list[list[str | None]]]) -> dict[str, str]:
    """Code → subject (with the teacher), from the legend tables: one row per code.

    Plain text cannot do it: the two columns of the legend end up on the same line."""
    entries: dict[str, str] = {}
    for table in tables:
        for row in table:
            cells = [" ".join((c or "").split()) for c in row if c and c.strip()]
            if len(cells) >= 2 and CODE.match(cells[0]) and not CODE.match(cells[1]):
                entries[cells[0]] = " ".join(cells[1:])
    return entries


@dataclass
class Timetable:
    title: str
    lessons: list[Lesson]
    legend: dict[str, str]
    problems: list[str]
    places: list[str] = field(default_factory=list)  # places of the legend, in order


def read_timetable(content: bytes) -> Timetable:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        tables, lessons, places = [], [], {}
        for page in pdf.pages:
            of_page = legend_places(page, coloured_rects(page))
            places |= of_page
            for t in page.find_tables():
                tables.append(t)
                lessons += lessons_of_table(t, of_page)
        entries = legend([t.extract() for t in tables])
    title = next((r.strip() for r in text.splitlines() if "orario" in r.lower()), "Orario")
    problems = sorted(
        {f"code without legend: {le.code}" for le in lessons if le.code not in entries}
    )
    if not lessons:
        problems.append("no lesson found")
    colours = {le.colour for le in lessons if le.colour is not None}
    if not places and len(colours) >= 2:
        problems.append("places: coloured cells but no places legend found")
    for le in lessons:
        if le.place == PLACE_TO_CHECK:
            log.warning("unknown place: %s %s %s", le.code, le.day, le.start)
    return Timetable(title, lessons, entries, problems, list(places.values()))


ORDER = list(DAYS.values())


def _hours(le: Lesson) -> str:
    return f"{le.start}–{le.end}"


def sentences(timetable: Timetable) -> list[str]:
    """One sentence per subject (when it has lessons) and one per day (what is on)."""
    by_subject: dict[str, list[Lesson]] = defaultdict(list)
    by_day: dict[str, list[Lesson]] = defaultdict(list)
    for le in timetable.lessons:
        by_subject[le.code].append(le)
        by_day[le.day].append(le)
    result = []
    for code, lessons in sorted(by_subject.items()):
        when = ", ".join(
            f"{le.day} {_hours(le)}" + (f" ({le.place})" if le.place else "")
            for le in sorted(lessons, key=lambda x: (ORDER.index(x.day), x.start))
        )
        name = timetable.legend.get(code, code)
        result.append(f"{timetable.title} — {name} [{code}]: lezione {when}.")
    for day in ORDER:
        if day not in by_day:
            continue
        what = "; ".join(
            f"{_hours(le)} {timetable.legend.get(le.code, le.code)}"
            + (f" ({le.place})" if le.place else "")
            for le in sorted(by_day[day], key=lambda x: x.start)
        )
        result.append(f"{timetable.title} — {day}: {what}.")
    if timetable.places:
        result.append(
            f"{timetable.title} — luoghi delle lezioni: {'; '.join(timetable.places)}. "
            f"{ROOMS_WARNING}"
        )
    return result


def markdown(timetable: Timetable) -> str:
    """The rebuilt grid: one row per time slot, one column per day."""
    days = [d for d in ORDER if any(le.day == d for le in timetable.lessons)]
    slots = sorted({(le.start, le.end) for le in timetable.lessons})
    lines = [f"**{timetable.title}**", "", "| Ora | " + " | ".join(days) + " |"]
    lines.append("|" + "---|" * (len(days) + 1))
    for start, end in slots:
        cells = [
            " / ".join(
                le.code
                for le in timetable.lessons
                if le.day == d and (le.start, le.end) == (start, end)
            )
            for d in days
        ]
        lines.append(f"| {start}–{end} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Legenda: " + "; ".join(f"{s} = {n}" for s, n in sorted(timetable.legend.items())),
    ]
    if any(le.place for le in timetable.lessons):
        lines += ["", "Luoghi delle lezioni:"]
        for d in days:
            of_day = sorted((le for le in timetable.lessons if le.day == d), key=lambda x: x.start)
            entries = "; ".join(f"{_hours(le)} {le.code}: {le.place or '-'}" for le in of_day)
            lines.append(f"- {d}: {entries}")
        lines.append(ROOMS_WARNING)
    return "\n".join(lines)
