"""Tables of the PDFs with pdfplumber, without an LLM: one reader per document.

Each document keeps the same structure from one year to the next (only details change, and the
common engine absorbs them), so each reader knows the columns and rules of its document:

- `study_plan.py`: study plans of the three tracks;
- `regulation.py`: course data at the top of the regulation (the annexes are not read);
- `exam_calendar.py`: exam sessions and boards;
- `electives.py`: elective exams of other courses;
- `timetable.py`: grid of the lessons (separate reader, based on the days);
- `engine.py`: the common geometry (columns, merged cells, continuations, context, check).

Try it on all the PDFs of the local copy: uv run python -m pipeline.tables
"""

import re
from types import ModuleType

from pipeline.tables import electives, exam_calendar, regulation, study_plan

# Link text of the document → reader. Documents not listed have no tables to read.
READERS: list[tuple[re.Pattern[str], ModuleType]] = [
    (re.compile(r"^Percorso formativo \d", re.I), study_plan),
    (re.compile(r"^Regolamento didattico \d", re.I), regulation),
    (re.compile(r"^Calendario esami", re.I), exam_calendar),
    (re.compile(r"^ELENCO esami opzionali", re.I), electives),
]


def reader(link_text: str) -> ModuleType | None:
    return next((m for rule, m in READERS if rule.search(link_text)), None)
