"""The RAG from the PC: question → embedding → Vectorize → tables from D1 → LLM → answer, sources.

Same logic as the bot (`bot/src/rag.ts`): the prompt is in one file, `bot/src/prompt.json`,
read by both. No `print` and no logs: question and answer go back only to the caller.

Years: the search always covers the two running cohorts (the enrolment year is not asked): the
LLM answers once if the answer is the same for both, and tells them apart if it changes.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from core import db
from core.config import ROOT, TIMEZONE
from core.embeddings import embedding
from core.years import current_year, filter_years, previous_year
from rag_local.llm import answer

DEFAULT_EXCERPTS = 8  # 8 against 6: right source found in 35/35 questions against 34/35
MAX_ANSWER_TOKENS = 600
# Below this score of the first Vectorize result the question is off topic and gets
# `PROMPT["off_topic"]` without calling the LLM. 0 = off. Measured on 322 test
# questions: at 0.40 no question about the course stopped (the lowest, "aula G&N",
# 0.429) and 64 obvious off-topic ones out of 100 did. Same as `bot/src/rag.ts`.
RELEVANCE_THRESHOLD = 0.40
# Telegram's "Reply": the follow-up question is rewritten by the LLM so that it stands alone, then
# searched and answered like any other question (threshold included). Temperature 0.
MAX_REWRITE_TOKENS = 80
MAX_REWRITTEN = 500  # characters: a longer rewritten question is a model mistake

PROMPT = json.loads((ROOT / "bot" / "src" / "prompt.json").read_text(encoding="utf-8"))
SYSTEM: str = PROMPT["system"]

# "Fonti: [1] [3]" at the start of a line or after the end of a sentence (sometimes at the end);
# also "Fonte:" and "Fonti utilizzate:". Same as `bot/src/rag.ts`.
SOURCES_LINE = re.compile(
    r"(?:^|(?<=[.!?)]))[ \t*_]*(font[ei]|sources?)([ \t]+(utilizzate|usate|citate|used|cited))?"
    r"[*_]*[ \t]*:[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
CITATION = re.compile(r"\[(\d{1,2})\]")
# The refusal of rule 1 of the prompt ("I can only help with the course"): no sources, even if the
# model still writes the "Fonti:" line. Same as `bot/src/rag.ts`.
REFUSAL = re.compile(r"posso aiutarti solo|can only help", re.IGNORECASE)


def search_filter(years: list[str], now: int) -> dict:
    """Only chunks of these years or valid for all, and not expired (notices)."""
    return {"year": {"$in": [*years, "all"]}, "valid_until": {"$gte": now}}


@dataclass
class Excerpt:
    """A block for the LLM: the text of a chunk or, if it comes from a table, the whole table."""

    number: int
    page: str
    url: str
    year: str
    text: str
    title: str = ""
    chunks: list[str] = field(default_factory=list)  # IDs of the chunks it stands for
    score: float = 0.0
    # The same text holds for the other cohort (`copies` table in D1, e.g. identical articles in the
    # 2025/26 and 2026/27 regulations): its years in the heading and, if cited, its source.
    copies: list[Excerpt] = field(default_factory=list)

    @property
    def years(self) -> str:
        """The excerpt's year, or the years of all cohorts with the same text."""
        return " e ".join(sorted({self.year, *(c.year for c in self.copies)}))


def _of_one_cohort(year: str) -> bool:
    return year not in ("", "all")


def excerpts(
    found: list[dict],
    tables: dict[str, dict],
    titles: dict[str, str],
    copies: dict[str, dict] | None = None,
) -> list[Excerpt]:
    """From the chunks found to numbered excerpts, in order of similarity.

    A chunk from a table is replaced by the whole table (from D1), once: the other chunks of the
    same table join that excerpt. `copies`: chunk ID → the other document with the same text
    (code, title, url, year; `copies` table in D1).
    """
    copies = copies or {}
    result: list[Excerpt] = []
    by_table: dict[str, Excerpt] = {}
    for f in found:
        m = f.get("metadata") or {}
        table_id = m.get("table") or ""
        if table_id in by_table:
            by_table[table_id].chunks.append(f["id"])
            continue
        text = m.get("text", "")
        if table_id in tables:
            table = tables[table_id]
            text = table["markdown"].strip()
            if table["context"] and table["context"] not in text:
                text = f"{table['context']}\n\n{text}"
        e = Excerpt(
            number=len(result) + 1,
            page=m.get("page", ""),
            url=m.get("url", ""),
            year=m.get("year", ""),
            text=text,
            title=titles.get(m.get("page", ""), ""),
            chunks=[f["id"]],
            score=float(f.get("score") or 0.0),
        )
        if c := copies.get(f["id"]):
            e.copies.append(Excerpt(e.number, c["code"], c["url"], c["year"], "", c["title"]))
        result.append(e)
        if table_id in tables:
            by_table[table_id] = e
    return result


def years_context(current: str) -> str:
    """The sentence about years for the LLM (text in `prompt.json`)."""
    return PROMPT["years"].format(previous=previous_year(current), current=current)


def rewrite_messages(question: str, quoted: str) -> list[dict[str, str]]:
    """With "Reply": asks the LLM to rewrite the question so that it stands without the quoted
    message (completes "and when is it?", leaves an unrelated question as it is)."""
    user = PROMPT["rewrite_question"].format(quoted=quoted, question=question)
    return [{"role": "system", "content": PROMPT["rewrite"]}, {"role": "user", "content": user}]


def rewritten_question(text: str, question: str) -> str:
    """The first line of the rewrite, without quotes; the original question if it is empty or too
    long (the model answered instead of rewriting)."""
    lines = [r.strip().strip("\"'«»“”").strip() for r in text.splitlines()]
    line = next((r for r in lines if r), "")
    return line if 0 < len(line) <= MAX_REWRITTEN else question


def rewrite(question: str, quoted: str | None, model: str | None = None) -> tuple[str, float]:
    """The question to search and pass to the LLM, with the neurons spent: without "Reply" the
    student's own, with no calls."""
    if not quoted:
        return question, 0.0
    r = answer(
        rewrite_messages(question, quoted),
        model=model,
        max_tokens=MAX_REWRITE_TOKENS,
        temperature=0.0,
    )
    return rewritten_question(r.text, question), r.neurons


def messages(question: str, found: list[Excerpt], today: date) -> list[dict[str, str]]:
    """System prompt and message with date, years and numbered excerpts. With "Reply" the question
    is already the rewritten one (`rewrite`)."""
    blocks = []
    for e in found:
        heading = f"[{e.number}] {e.title or e.url}"
        if _of_one_cohort(e.year):
            heading += f" (anno {e.years})"
        blocks.append(f"{heading}\n{e.text}")
    user = (
        f"Oggi è il {today.strftime('%d/%m/%Y')}. {years_context(current_year(today))}\n\n"
        "Estratti:\n\n" + "\n\n".join(blocks) + f"\n\nDomanda: {question}\n\n" + PROMPT["closing"]
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def split_sources(text: str, found: list[Excerpt]) -> tuple[str, list[Excerpt]]:
    """Removes the "Fonti:" line and references like "[2]" from the text and returns the cited
    excerpts (valid numbers, each URL once).

    The sources are those of the "Fonti:" line; without it, the references in the text (Gemma
    sometimes cites only there). With neither (an "I don't know" answer) no source is shown, nor
    when the answer refuses an off-topic request (`REFUSAL`).
    """
    lines = [m.group(0) for m in SOURCES_LINE.finditer(text)]
    citations = " ".join(lines) if lines else text
    numbers = {int(n) for n in CITATION.findall(citations)}
    clean = SOURCES_LINE.sub("", text)
    clean = re.sub(r"[ \t]*(\[\d{1,2}\][ \t]*)+(?=[.,;:!?)]|$)", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"[ \t]*\[\d{1,2}\]", "", clean).strip()
    if REFUSAL.search(clean):
        return clean, []
    sources, seen = [], set()
    for e in found:  # one source even if several excerpts come from the same document
        if e.number not in numbers:
            continue
        for source in (e, *e.copies):  # the same text in two cohorts: both sources
            if source.url not in seen:
                sources.append(source)
                seen.add(source.url)
    return clean, sources


def off_topic(found: list[dict], threshold: float) -> bool:
    """True if no result reaches the threshold (never with the threshold off)."""
    best = max((float(f.get("score") or 0.0) for f in found), default=0.0)
    return threshold > 0 and best < threshold


@dataclass
class Result:
    answer: str
    years: list[str]
    sources: list[Excerpt]
    excerpts: list[Excerpt]
    neurons: float
    seconds: float
    truncated: bool
    question: str = ""  # the question searched and passed to the LLM (rewritten with "Reply")


@dataclass
class Search:
    question: str  # the question searched (rewritten with "Reply", course codes with their name)
    years: list[str]
    found: list[dict]
    neurons: float


# A word of 2–7 letters and digits, not inside a longer one (ASCII only, as in `bot/src/rag.ts`)
WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]{1,6}(?![A-Za-z0-9])")


def expand_acronyms(question: str, codes: dict[str, str]) -> str:
    """Course codes of the timetables written in the question get the course name after them
    ("quanti cfu vale RAL?" → "quanti cfu vale RAL (Reinforcement and Adversarial Learning)?"): a
    code alone means nothing to the embedding. Codes of 3 or more characters in any case, codes of
    2 only in upper case ("no", "ds" are words); not if the name is already in the question. As in
    `bot/src/rag.ts`."""
    lower = question.lower()

    def name_after(m: re.Match) -> str:
        word = m.group(0)
        name = codes.get(word.upper())
        if not name or (len(word) < 3 and word != word.upper()) or name.lower() in lower:
            return word
        return f"{word} ({name})"

    return WORD.sub(name_after, question)


def acronyms() -> dict[str, str]:
    """Course code → course name, from the `acronyms` table of D1 (as the bot): the same code in
    two timetables gets the first name in code order."""
    sql = (
        "select a.code, a.name from acronyms a join sources s on s.code = a.source "
        "where s.absent_since is null order by a.code, a.source"
    )
    codes: dict[str, str] = {}
    for r in db.d1_batch([(sql, [])])[0]:
        codes.setdefault(r["code"], r["name"])
    return codes


def search(
    question: str,
    now: datetime,
    *,
    top_k: int = DEFAULT_EXCERPTS,
    quoted: str | None = None,
    model: str | None = None,
) -> Search:
    """The first half of the round, as in the bot: rewrite with "Reply", course names after their
    codes, embedding and search in the two running cohorts, without expired chunks."""
    years = filter_years(current_year(now.date()))
    standalone, rewrite_neurons = rewrite(question, quoted, model)
    searched = expand_acronyms(standalone, acronyms())
    vector = embedding([searched])
    found = db.search(vector.vectors[0], top_k, search_filter(years, int(now.timestamp())))
    return Search(searched, years, found, rewrite_neurons + vector.neurons)


def ask(
    question: str,
    *,
    top_k: int = DEFAULT_EXCERPTS,
    model: str | None = None,
    now: datetime | None = None,
    quoted: str | None = None,
) -> Result:
    """The whole round for a question, on the real Vectorize and D1 data, in the running cohorts."""
    start = time.perf_counter()
    now = now or datetime.now(TIMEZONE)
    s = search(question, now, top_k=top_k, quoted=quoted, model=model)
    if off_topic(s.found, RELEVANCE_THRESHOLD):
        return Result(
            answer=PROMPT["off_topic"],
            years=s.years,
            sources=[],
            excerpts=[],
            neurons=s.neurons,
            seconds=time.perf_counter() - start,
            truncated=False,
            question=s.question,
        )
    tables, titles, copies, absent = _from_d1(s.found)
    # A source absent at the last run (e.g. a PDF replaced by a new version) stays in Vectorize
    # until it is deleted: its chunks are not shown, or the old and the new version are cited.
    present = [f for f in s.found if (f.get("metadata") or {}).get("page") not in absent]
    if not present:
        return Result(
            answer=PROMPT["off_topic"],
            years=s.years,
            sources=[],
            excerpts=[],
            neurons=s.neurons,
            seconds=time.perf_counter() - start,
            truncated=False,
            question=s.question,
        )
    found = excerpts(present, tables, titles, copies)
    reply = answer(
        messages(s.question, found, now.date()), model=model, max_tokens=MAX_ANSWER_TOKENS
    )
    text, sources = split_sources(reply.text, found)
    return Result(
        answer=text,
        years=s.years,
        sources=sources,
        excerpts=found,
        neurons=s.neurons + reply.neurons,
        seconds=time.perf_counter() - start,
        truncated=reply.truncated,
        question=s.question,
    )


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _from_d1(
    found: list[dict],
) -> tuple[dict[str, dict], dict[str, str], dict[str, dict], set[str]]:
    """From D1, in one request: the whole tables of the chunks that come from a table, the titles
    of the sources, for each chunk the other document with the same text (`copies`) and the codes
    of the sources absent at the last run."""
    meta = [f.get("metadata") or {} for f in found]
    tables = sorted({m.get("table") or "" for m in meta} - {""})
    codes = sorted({m.get("page") or "" for m in meta} - {""})
    chunks = [f["id"] for f in found]
    reads = {
        "tables": (f"select id, context, markdown from tables where id in "
                   f"({_placeholders(len(tables))})", tables),
        "titles": (f"select code, title, absent_since from sources where code in "
                   f"({_placeholders(len(codes))})", codes),
        "copies": ("select c.chunk, s.code, s.title, s.url, s.year from copies c join sources s "
                   f"on s.code = c.also_for where s.absent_since is null "
                   f"and c.chunk in ({_placeholders(len(chunks))})",
                   chunks),
    }  # fmt: skip
    reads = {k: v for k, v in reads.items() if v[1]}  # no `in ()`
    rows = dict(zip(reads, db.d1_batch(list(reads.values())), strict=True))
    return (
        {r["id"]: r for r in rows.get("tables", [])},
        {r["code"]: r["title"] for r in rows.get("titles", [])},
        {r["chunk"]: r for r in rows.get("copies", [])},
        {r["code"] for r in rows.get("titles", []) if r.get("absent_since") is not None},
    )
