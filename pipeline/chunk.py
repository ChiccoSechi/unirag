"""Chunks from the extracted texts (`data/extracts.jsonl`) to `data/chunks.jsonl`: one record per
chunk, with a predictable ID and metadata always all present.

Rules:
- split where the text is already split: Markdown headings (pages, notices, parts of the sheets),
  articles of the regulations ("Art. 16 …");
- at most about `MAX` characters: a longer section is split at paragraphs, then lines, then
  sentences (never mid-sentence, if possible); split Markdown tables repeat their header;
- a section under `MIN` characters is joined to the next one of the same source;
- in front of each chunk the context: "site section › title (year) › section title";
- overlap only inside the same split section: the next piece repeats the last sentence (or the last
  short paragraph, up to `OVERLAP` characters) of the previous one;
- PDF tables: one sentence per row (ready made) and a summary per table, with the `table`
  metadata pointing to the whole table in D1;
- `valid_until`: for notices from their deadline (`pipeline/notices.py`), inherited by the attached
  PDFs; for everything else NEVER;
- documents with one version per cohort (`pipeline/cohorts.py`): identical sections and tables are
  only in the most recent document, with `copy_of` = the other one's code (`copies` table in D1) and
  "(testo identico nel …)" in the context; the older one keeps only the different parts.

Try it: uv run python -m pipeline.chunk   (results in data/chunks.jsonl and data/chunks.md)
"""

import hashlib
import json
import logging
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from core.config import NEVER, ROOT, TIMEZONE
from pipeline.cohorts import Merge, merges
from pipeline.notices import valid_until
from pipeline.sources import sources

log = logging.getLogger("pipeline.chunk")

DATA = ROOT / "data"
MAX = 1500
MIN = 300
OVERLAP = 250
SEPARATORS = [r"\n\s*\n", r"\n", r"(?<=[.;:!?])\s+", r" "]  # from the coarsest to the finest

MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.M)
ARTICLE = re.compile(r"^Art\.\s?\d+\b.*$", re.M)


@dataclass
class Chunk:
    id: str
    text: str
    url: str
    page: str  # short code of the source (filters: only the first 64 bytes are indexed)
    kind: str
    year: str  # "2026/2027" or "all"
    valid_until: int  # Unix timestamp; NEVER if it does not expire
    table: str  # ID of the table in D1, "" if the chunk does not come from a table
    # Code of the other cohort's document with the same text (`copies` table in D1), "" if none.
    # Not stored in Vectorize: the bot reads it from D1.
    copy_of: str = ""

    def metadata(self) -> dict:
        """The Vectorize metadata, always all present (a missing field escapes the filters)."""
        return {
            "text": self.text,
            "url": self.url,
            "page": self.page,
            "kind": self.kind,
            "year": self.year,
            "valid_until": self.valid_until,
            "table": self.table,
        }


# --- Short codes of the sources ---


def _short(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def code(extract: dict) -> str:
    """Short and stable code of the source, from the URL: the same URL always gives the same code.

    p21 = page crs_60_73_21, p14.2 = its page 2 of the list, a… = notice, d541442 = PDF,
    i23489.2026 = sheet of subject 23489 in the 2026 offer, e… = external page."""
    url, kind = extract["url"], extract["kind"]
    parts = urlparse(url)
    if kind == "page" and (m := re.search(r"/crs_60_73_?([a-z0-9_]*)\.page$", parts.path)):
        base = "p" + (m.group(1) or "0")
        page = parse_qs(parts.query).get("frame1_item")
        return f"{base}.{page[0]}" if page else base
    if kind == "pdf" and (m := re.search(r"/protected/(\d+)/", parts.path)):
        return "d" + m.group(1)
    if kind == "course" and (m := re.search(r"/insegnamenti/(\d+)/(\d+)/", parts.path)):
        return f"i{m.group(2)}.{m.group(1)}"
    prefixes = {"notice": "a", "external": "e"}
    return prefixes.get(kind, "x") + _short(url)


# --- Splitting the text ---


def _clean_title(t: str) -> str:
    return re.sub(r"[*_`]", "", t).strip()


EMPTY_LINE = re.compile(r"^\s*([.\-–—*_]+|-{3,})\s*$", re.M)  # ".", "---", "***" alone


def sections(extract: dict) -> list[tuple[str, str]]:
    """(section title, text) where the text is already split: articles or Markdown headings."""
    text = EMPTY_LINE.sub("", extract["text"])
    regulation = extract["kind"] == "pdf" and extract["title"].startswith("Regolamento")
    titles = list((ARTICLE if regulation else MARKDOWN_HEADING).finditer(text))
    result = []
    start, title = 0, ""
    for m in titles:
        result.append((title, text[start : m.start()].strip()))
        title = _clean_title(m.group(0) if regulation else m.group(1))
        start = m.end()
    result.append((title, text[start:].strip()))
    return [(t, s) for t, s in result if t or s]


def merge_small(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sections under MIN characters are joined to the next one (the last to the previous)."""
    result: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None
    for title, text in parts:
        if pending is not None:
            t0, s0 = pending
            text = "\n\n".join(x for x in (s0, f"**{title}**" if title and t0 else "", text) if x)
            title = " / ".join(x for x in (t0, title) if x)
            pending = None
        if len(text) < MIN:
            pending = (title, text)
        else:
            result.append((title, text))
    if pending is not None:
        if result:
            t0, s0 = result[-1]
            t1, s1 = pending
            text = "\n\n".join(x for x in (s0, f"**{t1}**" if t1 else "", s1) if x)
            result[-1] = (t0, text)
        else:
            result.append(pending)
    return result


def _markdown_table(text: str) -> bool:
    lines = text.strip().splitlines()
    return len(lines) > 2 and all(r.lstrip().startswith("|") for r in lines)


def split(text: str, level: int = 0) -> list[str]:
    """Pieces of at most MAX characters, cut at the coarsest possible separator."""
    if len(text) <= MAX:
        return [text]
    if _markdown_table(text):  # long Markdown table: rows, with the header repeated
        lines = text.strip().splitlines()
        header, body = "\n".join(lines[:2]), lines[2:]
        pieces = _pack(body, "\n", MAX - len(header) - 1)
        return [f"{header}\n{p}" for p in pieces]
    if level >= len(SEPARATORS):
        return [text[i : i + MAX] for i in range(0, len(text), MAX)]
    separator = SEPARATORS[level]
    joiner = "\n\n" if level == 0 else ("\n" if level == 1 else " ")
    pieces = []
    for part in re.split(separator, text):
        pieces += split(part, level + 1) if len(part) > MAX else [part]
    return _pack([p for p in pieces if p.strip()], joiner, MAX)


def _pack(parts: list[str], joiner: str, maximum: int) -> list[str]:
    """Puts consecutive parts together in pieces of similar length (not a big one and a small
    leftover), never over `maximum`."""
    total = sum(len(p) + len(joiner) for p in parts)
    target = total / max(1, math.ceil(total / maximum))
    pieces: list[str] = []
    for part in parts:
        new = len(pieces[-1]) + len(joiner) + len(part) if pieces else 0
        # added only if the piece gets closer to the target (and stays under the maximum)
        if pieces and new <= maximum and abs(new - target) < abs(len(pieces[-1]) - target):
            pieces[-1] += joiner + part
        else:
            pieces.append(part)
    return pieces


def tail(piece: str) -> str:
    """The end to repeat at the start of the next piece: last short paragraph, otherwise last
    sentence, otherwise nothing."""
    last = re.split(r"\n\s*\n", piece.strip())[-1]
    if len(last) <= OVERLAP:
        return last
    sentence = re.split(r"(?<=[.;:!?])\s+", last.strip())[-1]
    return sentence if len(sentence) <= OVERLAP else ""


def with_overlap(pieces: list[str]) -> list[str]:
    result = pieces[:1]
    for before, piece in zip(pieces, pieces[1:], strict=False):
        repeated = tail(before)
        result.append(
            f"{repeated}\n\n{piece}" if repeated and not _markdown_table(piece) else piece
        )
    return result


def heading(extract: dict, section_title: str, identical_in: str = "") -> str:
    """The context in front of the chunk: "Informatica › Didattica › Aule › Section title".
    `identical_in`: the title of the other cohort's document with the same text."""
    title = extract["title"]
    if extract.get("year") and extract["year"] not in title:
        title += f" ({extract['year']})"
    if identical_in:
        title += f" (testo identico nel {identical_in})"
    if extract["kind"] == "notice" and extract.get("date"):
        title = f"Avviso del {extract['date']}: {title}"
    parts = [extract["section"]] if extract["kind"] in ("page", "external") else []
    parts += [title, section_title]
    return " › ".join(p for p in parts if p)


# --- Tables ---


def _cfu(value: str) -> int | None:
    """ "9" → 9, "6 + 3" → 9; anything else → None (the total only if all cells are numbers)."""
    if not re.fullmatch(r"\d+(\s*\+\s*\d+)*", value.strip()):
        return None
    return sum(int(x) for x in re.findall(r"\d+", value))


def table_summary(table: dict, document: str) -> str | None:
    """One sentence per table with the subjects; the CFU total only for tables of a course year
    (the elective tables are not added up)."""
    names = table.get("headers") or []
    if "Insegnamento" not in names:
        return None
    fields = [dict(zip(names, r, strict=False)) for r in table["rows"]]
    listed = ", ".join(c["Insegnamento"] for c in fields if c.get("Insegnamento"))
    context = f", {table['context']}" if table.get("context") else ""
    sentence = f"{document}{context}: {len(fields)} insegnamenti o attività ({listed})"
    cfu = [_cfu(c.get("CFU", "")) for c in fields]
    if re.search(r"\d°\s*ANNO", table.get("context", ""), re.I) and cfu and None not in cfu:
        sentence += f", {sum(cfu)} CFU in tutto"
    return sentence + "."


YEAR_CONTEXT = re.compile(r"^indirizzo (.+?), (\d)° ANNO\b", re.I)


def study_plan_summary(extract: dict) -> str | None:
    """A plain-text sentence for a study plan: tracks and CFU of each course year.

    The tracks are written only in the table headings: without this sentence the question "which
    tracks are there?" finds nothing. It is added to the table sentences (same PDF).
    """
    cfu: dict[str, dict[str, int | None]] = {}
    for table in extract.get("tables") or []:
        m = YEAR_CONTEXT.match(table.get("context", ""))
        if not m or "CFU" not in (table.get("headers") or []):
            continue
        index = table["headers"].index("CFU")
        values = [_cfu(r[index]) if index < len(r) else None for r in table["rows"]]
        track = cfu.setdefault(m.group(1), {})
        before = track.get(m.group(2), 0)
        track[m.group(2)] = None if before is None or None in values else before + sum(values)
    if not cfu:
        return None
    sentences = [
        f"{extract['title']} della laurea magistrale in Informatica: {len(cfu)} indirizzi "
        f"(curriculum): {', '.join(cfu)}."
    ]
    for track, years in cfu.items():
        parts = [
            f"{y}° anno {c} CFU" if c is not None else f"{y}° anno"
            for y, c in sorted(years.items())
        ]
        total = sum(c for c in years.values() if c is not None)
        complete = None not in years.values() and len(years) > 1
        sentences.append(
            f"Indirizzo {track}: {', '.join(parts)}"
            + (f", {total} CFU in tutto." if complete else ".")
        )
    return " ".join(sentences)


# --- Lists: summary sentences for "which are all the…" questions ---
# The search finds the chunks most similar to the question: for "the electives of a teacher" or
# "courses in English" the right items are spread over many chunks and only one or two arrive.
# These sentences, made by the code, put the whole list in a single chunk.

LABEL = re.compile(r"^\*\*([^*\n]+?)(?::\*\*|\*\*\s*:?)\s*(.*)$")
CONTACT_PLACEHOLDER = re.compile(r"\s*[–-]?\s*\[(email|telefono) sul sito\]")
SPLIT_NAMES = re.compile(r",\s*|\s+e\s+|\s+and\s+")


def field_value(block: str, names: tuple[str, ...]) -> str:
    """The value of a "**Label:** value" field, also with the value on the next line."""
    lines = [r.strip() for r in block.split("\n")]
    for i, line in enumerate(lines):
        m = LABEL.match(line)
        if not m or m.group(1).strip().lower() not in names:
            continue
        value = m.group(2).strip()
        if not value:  # label alone: the value is the next non-empty line
            value = next((r for r in lines[i + 1 :] if r), "")
            if LABEL.match(value):
                return ""
        return CONTACT_PLACEHOLDER.sub("", value).strip(" .;")
    return ""


def elective_activities(text: str) -> list[tuple[str, list[str], str]]:
    """(title, teachers, CFU) for each "#### Title" block that has a teacher field."""
    result = []
    for block in re.split(r"^#### ", text, flags=re.M)[1:]:
        title = block.split("\n", 1)[0].strip()
        teachers = field_value(block, ("instructor", "instructors", "docente", "docenti"))
        if title and teachers:
            names = [n.strip() for n in SPLIT_NAMES.split(teachers) if n.strip()]
            result.append((title, names, field_value(block, ("cfu",))))
    return result


def _in_pieces(heading: str, items: list[str]) -> list[str]:
    """A long list split into sentences within the chunk limit, each with its heading."""
    pieces, current = [], []
    for item in items:
        if current and len(heading) + len("; ".join([*current, item])) + 2 > MAX:
            pieces.append(current)
            current = []
        current.append(item)
    if current:
        pieces.append(current)
    return [f"{heading}: {'; '.join(p)}." for p in pieces]


def activity_summaries(extract: dict) -> list[str]:
    """For a page of activities with a teacher (e.g. "Attività a scelta dello studente"): one
    sentence per teacher and the full list. Nothing if the page has fewer than 3 such activities."""
    text = extract.get("text", "")
    activities = elective_activities(text)
    if len(activities) < 3:
        return []
    page = extract["title"]
    # The title of the first block without a teacher tells how the page is called on the site (e.g.
    # "Attività a Scelta e Reading courses correntemente attivi"): the source's words go into the
    # sentences, so the LLM links "reading course" and "attività a scelta" without guessing.
    titles = [b.split("\n", 1)[0].strip() for b in re.split(r"^#### ", text, flags=re.M)[1:]]
    with_teacher = {a[0] for a in activities}
    intro = next((t for t in titles if t and t not in with_teacher), "")
    if intro and intro.lower() != page.lower():
        page = f"{page} ({intro})"
    by_teacher: dict[str, list[str]] = {}
    for title, teachers, cfu in activities:
        item = f"{title} ({cfu} CFU)" if cfu else title
        for t in teachers:
            by_teacher.setdefault(t, []).append(item)
    sentences = [
        f"{page}, attività tenute da {t} ({len(v)}): {', '.join(v)}."
        for t, v in sorted(by_teacher.items())
    ]
    items = [
        f"{title} ({', '.join(teachers)}{', ' + cfu + ' CFU' if cfu else ''})"
        for title, teachers, cfu in activities
    ]
    heading = f"{page}: elenco completo delle {len(activities)} attività"
    return sentences + _in_pieces(heading, items)


# Event pages (e.g. "Altre attività formative"): one block per event, opened by the title in bold
# alone on a line, with the CFU in the text. Generic questions like "which seminars are there?"
# found the study plans and not the page: the event chunks are long and full of details. The
# sentence puts titles and CFU of all events in a short chunk.
EVENT_TITLE = re.compile(r"^\*\*([^*\n]+)\*\*$", re.M)
EXPECTED_CFU = re.compile(r"CFU previsti:\s*([^,\n]+)", re.I)
CFU_IN_TEXT = re.compile(r"\b\d+\s*CFU\b")
LINKED = re.compile(r"^- [^:\n]*: \[(.+?)\]\(", re.M)


def events_with_cfu(text: str) -> list[tuple[str, str]]:
    """(title, CFU) for each "**Title**" block that mentions CFU: "CFU previsti: …" if present (up
    to the first comma), otherwise the first "N CFU" of the block."""
    parts = EVENT_TITLE.split(text)
    events = []
    for title, block in zip(parts[1::2], parts[2::2], strict=True):
        if m := EXPECTED_CFU.search(block):
            events.append((title.strip(), m.group(1).strip(" .;")))
        elif m := CFU_IN_TEXT.search(block):
            events.append((title.strip(), m.group(0)))
    return events


DATE_LINE = re.compile(r"^Data(?: evento)?:\s*(.+)$", re.M | re.I)
EVENT_LINK = re.compile(r"<(https?://[^>\s]+)>|\]\((https?://[^)\s]+)\)")
LINKED_ITEM = re.compile(r"^- ([^:\n]+): \[(.+?)\]\((https?://[^)\s]+)\)", re.M)


def events(extract: dict) -> list[dict]:
    """The events of an event page, in the page's order, for the /events command: the "**Title**"
    blocks with CFU, then the linked contents. Date as written on the site, "" if missing; link of
    the event, "" if missing. [] for pages that are not event pages (as `events_summaries`)."""
    text = extract.get("text", "").replace("\\_", "_")
    if len(events_with_cfu(text)) < 3:
        return []
    body, _, linked = text.partition("\nContenuti collegati:")
    parts = EVENT_TITLE.split(body)
    found = []
    for title, block in zip(parts[1::2], parts[2::2], strict=True):
        cfu = EXPECTED_CFU.search(block) or CFU_IN_TEXT.search(block)
        if not cfu:
            continue
        date = DATE_LINE.search(block)
        url = EVENT_LINK.search(block)
        found.append({
            "title": title.strip(),
            "date": date.group(1).strip() if date else "",
            "credits": (cfu.group(1) if cfu.re is EXPECTED_CFU else cfu.group(0)).strip(" .;"),
            "url": (url.group(1) or url.group(2)) if url else "",
        })  # fmt: skip
    for date, title, url in LINKED_ITEM.findall(linked):
        cfu = CFU_IN_TEXT.search(title)
        found.append({"title": title.strip(), "date": date.strip(),
                      "credits": cfu.group(0) if cfu else "", "url": url})  # fmt: skip
    return found


LEGEND_LINE = re.compile(r"^Legenda: (.+)$", re.M)  # written by `tables.timetable.markdown`
LEGEND_ENTRY = re.compile(r"^([A-Z][A-Z0-9]{1,6}) = (.+)$")
TEACHERS = re.compile(r"\s*\([^()]*\)$")


def acronyms(extract: dict) -> list[tuple[str, str]]:
    """Course codes of the lesson timetables ("RAL" → "Reinforcement and Adversarial Learning"),
    from the legend of the rebuilt grid, without the teachers; for the `acronyms` table of D1. The
    bot adds the name after a code written in the question, so that the search finds the course."""
    found: dict[str, str] = {}
    for t in extract.get("tables") or []:
        for line in LEGEND_LINE.findall(t.get("markdown", "")):
            for entry in line.split("; "):
                if (m := LEGEND_ENTRY.match(entry)) and (name := TEACHERS.sub("", m.group(2))):
                    found.setdefault(m.group(1), name)
    return sorted(found.items())


def events_summaries(extract: dict) -> list[str]:
    """For a page with at least 3 events with CFU: sentences with titles and CFU of all of them,
    plus the linked contents ("date: [title](link)", `_linked_contents` in extract.py). A long
    list is split into several sentences: none is dropped."""
    text = extract.get("text", "")
    events = events_with_cfu(text)
    if len(events) < 3:
        return []
    items = [f"{t} ({c})" if "cfu" in c.lower() else f"{t} ({c} CFU)" for t, c in events]
    items += LINKED.findall(text.replace("\\_", "_"))
    block = re.search(r"^#### (.+)$", text, re.M)
    heading = f"{extract['title']}: seminari, eventi e attività che danno CFU"
    if block:
        heading += f" ({block.group(1).strip()})"
    return _in_pieces(heading, items)


def _sheet_field(text: str, name: str) -> str:
    m = re.search(rf"^- \*\*{name}:\*\* (.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def course_summaries(sheets: list[dict]) -> list[str]:
    """From the catalogue sheets: subjects by teacher, by language and by period."""
    if not sheets:
        return []
    year = sheets[0].get("year") or ""
    by_teacher: dict[str, list[str]] = {}
    by_language: dict[str, list[str]] = {}
    by_period: dict[str, list[str]] = {}
    for s in sorted(sheets, key=lambda x: x["title"]):
        t, name = s["text"], s["title"]
        details = [x for x in (
            f"{_sheet_field(t, 'CFU')} CFU" if _sheet_field(t, "CFU") else "",
            _sheet_field(t, "Periodo").lower(),
            f"in {_sheet_field(t, 'Lingua').lower()}" if _sheet_field(t, "Lingua") else "",
        ) if x]  # fmt: skip
        item = f"{name} ({', '.join(details)})" if details else name
        for d in SPLIT_NAMES.split(_sheet_field(t, "Docenti")):
            if d := re.sub(r"\s*\(titolare\)", "", d).strip():
                by_teacher.setdefault(d, []).append(item)
        if language := _sheet_field(t, "Lingua").lower():
            by_language.setdefault(language, []).append(name)
        if period := _sheet_field(t, "Periodo").lower():
            by_period.setdefault(period, []).append(name)
    prefix = f"Insegnamenti erogati nel {year}"
    sentences = [f"{prefix} tenuti da {d}: {', '.join(v)}." for d, v in sorted(by_teacher.items())]
    sentences += [
        f"{prefix} in {k} ({len(v)}): {', '.join(v)}." for k, v in sorted(by_language.items())
    ]
    sentences += [
        f'{prefix} con periodo "{k}" ({len(v)}): {", ".join(v)}.'
        for k, v in sorted(by_period.items())
    ]
    return sentences


# --- Chunks of a source ---


def _groups(extract: dict, merge: Merge | None) -> list[tuple[bool, list[tuple[str, str]]]]:
    """The sections to split, in groups of nearby sections all shared or all not (True = shared
    with the other cohort): small sections are joined only inside the same group."""
    parts = sections(extract)
    if merge is None:
        return [(False, parts)]
    if not merge.recent:  # in the older document the shared parts are not stored
        return [(False, [p for i, p in enumerate(parts) if i not in merge.sections])]
    groups: list[tuple[bool, list[tuple[str, str]]]] = []
    for i, part in enumerate(parts):
        shared = i in merge.sections
        if groups and groups[-1][0] == shared:
            groups[-1][1].append(part)
        else:
            groups.append((shared, [part]))
    return groups


def chunks_of_source(
    extract: dict,
    expires: int = NEVER,
    added: list[tuple[str, str]] | None = None,
    merge: Merge | None = None,
) -> list[Chunk]:
    """The chunks of a source. `added`: (text, year) of summary sentences made from other sources
    but shown with this one (e.g. the sheets' summaries on the "Insegnamenti" page).
    `merge`: the parts identical in the other cohort's document (`pipeline/cohorts.py`)."""
    c = code(extract)
    base = {
        "url": extract["url"],
        "page": c,
        "kind": extract["kind"],
        "year": extract.get("year") or "all",
        "valid_until": expires,
    }
    recent = merge is not None and merge.recent
    other = code(merge.other) if recent else ""
    texts: list[tuple[str, str, str]] = []  # (text, table, copy_of)
    for shared, parts in _groups(extract, merge):
        identical_in = merge.other["title"] if shared and merge else ""
        for section_title, text in merge_small(parts):
            head = heading(extract, section_title, identical_in)
            for piece in with_overlap(split(text)):
                texts.append((f"{head}\n\n{piece}".strip(), "", other if shared else ""))
    for i, table in enumerate(extract.get("tables") or []):
        if merge is not None and not merge.recent and i in merge.tables:
            continue  # table identical in the most recent document: stored only there
        table_id = f"{c}-t{i}"
        copy = other if recent and i in merge.tables else ""
        texts += [(s, table_id, copy) for s in table["sentences"]]
        if sentence := table_summary(table, extract["title"]):
            texts.append((sentence, table_id, copy))
    if sentence := study_plan_summary(extract):
        texts.append((sentence, "", ""))  # plain text: the LLM gets the sentence, not a table
    texts += [(s, "", "") for s in activity_summaries(extract)]
    texts += [(s, "", "") for s in events_summaries(extract)]
    chunks = [
        Chunk(id=f"{c}-{n}", text=t, table=table_id, copy_of=copy, **base)
        for n, (t, table_id, copy) in enumerate(texts)
    ]
    for text, year in added or []:
        chunks.append(
            Chunk(id=f"{c}-{len(chunks)}", text=text, table="", **(base | {"year": year}))
        )
    return chunks


def deadlines(extracts: list[dict]) -> dict[str, int]:
    """URL → `valid_until` for the notices and for the PDFs attached to a notice."""
    of_notices = {e["url"]: valid_until(e)[0] for e in extracts if e["kind"] == "notice"}
    attached = {
        e["url"]: of_notices[e["linked_from"]]
        for e in extracts
        if e["kind"] == "pdf" and e.get("linked_from") in of_notices
    }
    return of_notices | attached


# Course page listing the subjects: it hosts the summaries of the catalogue sheets
COURSES_PAGE = "https://web.unica.it/unica/it/crs_60_73_21.page"


def chunks_of_all(extracts: list[dict]) -> list[Chunk]:
    expiry = deadlines(extracts)
    sheets = [e for e in extracts if e["kind"] == "course"]
    sheets_year = sheets[0].get("year") or "all" if sheets else "all"
    added = {COURSES_PAGE: [(s, sheets_year) for s in course_summaries(sheets)]}
    if sheets and not any(e["url"] == COURSES_PAGE for e in extracts):
        log.warning("Insegnamenti page missing: summaries of the sheets not written")
    by_cohort = merges(extracts, sources().data.get("merged_cohort_documents") or [], sections)
    result = [
        c
        for e in extracts
        for c in chunks_of_source(
            e, expiry.get(e["url"], NEVER), added.get(e["url"]), by_cohort.get(e["url"])
        )
    ]
    repeated = [i for i, n in Counter(c.id for c in result).items() if n > 1]
    if repeated:
        raise ValueError(f"repeated chunk IDs: {repeated[:5]}")
    return result


def _write(chunks: list[Chunk]) -> None:
    with (DATA / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps({"id": c.id, "metadata": c.metadata()}, ensure_ascii=False) + "\n")
    blocks = [
        f"## {c.id} · {c.kind} · {c.year}" + (f" · table {c.table}" if c.table else "")
        + f" · {len(c.text)} characters\n{c.url}\n\n{c.text}"
        for c in chunks
    ]  # fmt: skip
    (DATA / "chunks.md").write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    lines = (DATA / "extracts.jsonl").read_text(encoding="utf-8").splitlines()
    extracts = [json.loads(r) for r in lines]
    chunks = chunks_of_all(extracts)
    _write(chunks)
    for e in extracts:  # one line per notice: date found (or none) and validity, no text
        if e["kind"] == "notice":
            until, found = valid_until(e)
            log.info(
                "notice %s: %s → valid until %s · %s",
                code(e),
                f"deadline {found:%d/%m/%Y}" if found else "no date",
                f"{datetime.fromtimestamp(until, TIMEZONE):%d/%m/%Y}",
                e["url"],
            )
    for kind, n in Counter(c.kind for c in chunks).most_common():
        lengths = [len(c.text) for c in chunks if c.kind == kind]
        log.info(
            "%-12s %4d chunks · median %4d · max %4d characters",
            kind, n, statistics.median(lengths), max(lengths),
        )  # fmt: skip
    from_tables = sum(bool(c.table) for c in chunks)
    log.info("total %d chunks (%d from tables) · free limit about 4,880", len(chunks), from_tables)


if __name__ == "__main__":
    main()
