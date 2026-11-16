"""The academic calendar: which academic year it is and which two cohorts are running.

Used by the pipeline (which documents to download, September warning) and by the RAG (search in
both running cohorts).
"""

from datetime import date


def current_year(today: date) -> str:
    """Today's academic year: from September the one starting (October 2026 → 2026/2027)."""
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}/{start + 1}"


def previous_year(year: str) -> str:
    start = int(year[:4]) - 1
    return f"{start}/{start + 1}"


def filter_years(current: str) -> list[str]:
    """The two running cohorts: the previous year and the current one."""
    return [previous_year(current), current]
