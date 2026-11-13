"""Deadline of the notices → `valid_until`, without an LLM (decided on 11 October 2026).

Rule:
- deadline = the furthest date mentioned in the title or text of the notice: it matches the real
  deadline when there is one ("Scadenza ore 12 del 23 Luglio 2026") and the last day of the event
  when it is an event ("16 e 17 ottobre 2026");
- `valid_until` = deadline + 7 days, at 23:59:59 Rome time;
- notice without dates: 31 December of the publication year (not of the run's year, otherwise it
  would come back every January); without even the publication date, only dates with a year and,
  if there are none, NEVER: never today's date, because the result must not change between runs;
- the PDFs attached to a notice inherit its `valid_until` (in `pipeline/chunk.py`).

Dates recognised: "23 Luglio 2026", "16 e 17 ottobre 2026", "30 giugno – 1 luglio 2026", "giovedì 1
Ottobre" (without a year: the publication year, or the next one if the date would fall well before
the publication), "22/9/2026". The publication date (at the top of every notice) and dates more
than two years after publication (more likely mentioned for something else) do not count.

Possible mistakes go the safe way: a far date mentioned for something else keeps the notice
visible for longer; a deadline without a date ("by the end of the month") means 31 December.
"""

import re
from datetime import date, datetime, time, timedelta

from core.config import NEVER, TIMEZONE

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}  # fmt: skip
DAYS_AFTER = 7
TOO_FAR = timedelta(days=730)  # dates further than this from the publication are ignored
TOO_EARLY = timedelta(days=60)  # a date without a year this long before publication is next year's

_MONTH = "|".join(MONTHS)
# "16 e 17 ottobre 2026", "1° Ottobre", "l'11 giugno": one or more days, the month, the year if any
TEXT_DATE = re.compile(
    rf"\b(?P<days>\d{{1,2}}°?(?:\s*(?:e|,|-|–)\s*\d{{1,2}}°?)*)\s+(?P<month>{_MONTH})"
    rf"(?:\s+(?P<year>\d{{4}}))?\b",
    re.I,
)
NUMERIC_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\d/])")


def italian_date(text: str) -> date | None:
    """ "28 settembre 2026" → date(2026, 9, 28)."""
    m = TEXT_DATE.search(text)
    if m is None or m.group("year") is None:
        return None
    return _date(int(m.group("year")), MONTHS[m.group("month").lower()], _days(m)[-1])


def _days(m: re.Match) -> list[int]:
    return [int(d) for d in re.findall(r"\d{1,2}", m.group("days"))]


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _numeric_dates(text: str) -> list[date]:
    found = []
    for d, month, year in NUMERIC_DATE.findall(text):
        year = int(year) + (2000 if len(year) == 2 else 0)
        if (x := _date(year, int(month), int(d))) is not None:
            found.append(x)
    return found


def dates_in_text(text: str, published: date) -> list[date]:
    """All dates of the text; without a year the publication year (or the next one)."""
    found: list[date] = []
    for m in TEXT_DATE.finditer(text):
        month = MONTHS[m.group("month").lower()]
        for day in _days(m):
            if m.group("year"):
                d = _date(int(m.group("year")), month, day)
            else:
                d = _date(published.year, month, day)
                if d is not None and d < published - TOO_EARLY:
                    d = _date(published.year + 1, month, day)
            if d is not None:
                found.append(d)
    found += _numeric_dates(text)
    return [d for d in found if d <= published + TOO_FAR]


def dates_with_year(text: str) -> list[date]:
    """Only dates written with the year: used when the publication date is missing, which is the
    reference for those without a year."""
    found = [
        d
        for m in TEXT_DATE.finditer(text)
        if m.group("year")
        for day in _days(m)
        if (d := _date(int(m.group("year")), MONTHS[m.group("month").lower()], day)) is not None
    ]
    return found + _numeric_dates(text)


def deadline(title: str, text: str, published: date) -> date | None:
    """The furthest date mentioned in the notice, or None if there is none. The publication date
    (at the top of every notice) does not count: it is not a deadline."""
    found = [d for d in dates_in_text(f"{title}\n{text}", published) if d != published]
    return max(found) if found else None


def end_of_day(d: date) -> int:
    """Unix timestamp of 23:59:59 of that day, Rome time."""
    return int(datetime.combine(d, time(23, 59, 59), tzinfo=TIMEZONE).timestamp())


def valid_until(notice: dict) -> tuple[int, date | None]:
    """(`valid_until` of the notice, deadline found or None). `notice` is an extract record.

    Always the same result for the same notice (never today's date): otherwise its hash would
    change at every run and it would be rewritten every night. Without a publication date only dates
    written with the year count; if there are none, the notice never expires (NEVER)."""
    published = italian_date(notice.get("date", ""))
    if published is None:
        explicit = dates_with_year(f"{notice['title']}\n{notice['text']}")
        if not explicit:
            return NEVER, None
        found = max(explicit)
        return end_of_day(found + timedelta(days=DAYS_AFTER)), found
    found = deadline(notice["title"], notice["text"], published)
    if found is None:
        return end_of_day(date(published.year, 12, 31)), None
    return end_of_day(found + timedelta(days=DAYS_AFTER)), found
