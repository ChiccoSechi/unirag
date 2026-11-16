"""The pipeline run: from the downloaded copy (`cache/`, filled by the crawler) to Vectorize and D1.

1. Extraction and chunks (`pipeline/extract.py`, `pipeline/chunk.py`). If extraction reports
   problems (e.g. the site changed structure) the run stops before writing anything.
2. For each source, `hash` (fingerprint of the chunks as they go into Vectorize) and `text_hash`
   (fingerprint of the extracted text only) compared with the `sources` table of D1:
   - same → nothing;
   - new or changed → embedding (bge-m3) of its chunks; different `text_hash` = changed by UniCa;
   - in D1 but not found → at the first run `absent_since` is set; if still missing at the next run
     (2 runs in a row) its chunks are deleted and then the row. If it comes back, it is reset. A
     document that no longer exists (`gone` of the crawler: the portal's login, 404) is deleted at
     once. Until then the bot does not show the chunks of absent sources.
3. **First Vectorize, then D1**: inserts, then deletes (leftover chunks of shortened
   sources, sources gone for 2 runs); wait until Vectorize has processed them; only then D1 is
   written, one source per atomic batch (row in `sources` with its `notices`, `tables` and
   `copies`).
   If the run stops after Vectorize, D1 still has the old hash: at the next run the source looks
   changed and is rewritten, and everything becomes consistent again by itself.
4. Before writing, checks on the real data (`pipeline/checks.py`): personal data left in the
   texts stop the run; sources that lost most of their content only give a warning.
5. A row in `runs` with the counts (also if the run fails: outcome "error" and type).
6. For `/updates`: `added_at` (first appearance) and `changed_at` (text changed by UniCa). If the
   extraction code changed (`extraction_version`, fingerprint of the files producing the text), the
   different texts of that run are our doing and do not count as changed by UniCa.

UniCa's website unreachable or under maintenance: nothing is written and the run fails (GitHub
emails whoever runs it); the bot keeps answering with last week's data.
- crawler stopped (503, 429, network errors in a row, menu not found) → `summary.json` has
  `stopped` → the run does not start (`CrawlerStopped`);
- crawler not stopped but with many sources fewer (e.g. maintenance page with HTTP 200, Cineca
  catalogue down) → more than 30% of D1's sources missing, or more than half of a kind (with at
  least 5 sources) → the run does not start (`TooManyAbsent`). For a wanted exclusion:
  `--accept-absences`.

Daily notices run (`--additions-only`): reads `cache/summary_notices.json` (written by
`pipeline.crawl --notices-only`) and adds or updates the sources found, without marking absences or
deleting (the full Sunday run does that). In `runs` its outcome is "ok_notices": `/fonti`
shows the date of the last full run ("ok").

Usage: uv run python -m pipeline.run --dry-run          # what it would do: no writes, no embeddings
       uv run python -m pipeline.run                    # real run
       uv run python -m pipeline.run --accept-absences  # many sources removed on purpose
       uv run python -m pipeline.run --additions-only   # daily notices run
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as clock

from core import db
from core.config import ROOT, TIMEZONE, details
from core.embeddings import embedding
from core.years import current_year, filter_years
from pipeline.checks import PersonalDataFound, losses, personal_data
from pipeline.chunk import Chunk, acronyms, chunks_of_all, code, events
from pipeline.extract import extract_all
from pipeline.notices import end_of_day, italian_date, valid_until

log = logging.getLogger("pipeline.run")

EMBEDDING_BATCH = 50  # texts per call to Workers AI
NEURONS_PER_TOKEN = 1075 / 1_000_000  # bge-m3, Workers AI price list
CHARS_PER_TOKEN = 2  # measured on the first import: 1,099 chunks, 419 neurons
STATEMENTS_PER_BATCH = 80  # SQL statements per D1 batch (a source is never split)

Statement = tuple[str, Sequence]


def fingerprint(data) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# The files deciding the extracted text (`text_hash`): if they change, the version changes.
# sources.py and sources.yaml too: they choose the kept section and the linked contents of a page.
EXTRACTION_FILES = [
    "pipeline/extract.py",
    "pipeline/catalogue.py",
    "pipeline/tables/*.py",
    "pipeline/sources.py",
    "pipeline/sources.yaml",
]


def extraction_version() -> str:
    """Fingerprint of the extraction code (content of the files, in name order)."""
    files = sorted({f for pattern in EXTRACTION_FILES for f in ROOT.glob(pattern)})
    return fingerprint(
        [[f.relative_to(ROOT).as_posix(), f.read_text(encoding="utf-8")] for f in files]
    )


# --- Today's sources ---


@dataclass
class Source:
    code: str
    extract: dict
    chunks: list[Chunk]

    @property
    def hash(self) -> str:
        """Fingerprint of the chunks as they go into Vectorize, plus the link to the other cohort
        (`copies` in D1), the events (`events` in D1) and the course codes (`acronyms` in D1) when
        there are some: the other sources keep the same fingerprint."""
        chunks = [
            {"id": c.id, **c.metadata(), **({"copy_of": c.copy_of} if c.copy_of else {})}
            for c in self.chunks
        ]
        page_events = events(self.extract)
        codes = acronyms(self.extract)
        return fingerprint(
            chunks
            + ([{"events": page_events}] if page_events else [])
            + ([{"acronyms": codes}] if codes else [])
        )

    @property
    def text_hash(self) -> str:
        return fingerprint([self.extract["text"], [t["markdown"] for t in self.extract["tables"]]])


def todays_sources(extracts: list[dict], chunks: list[Chunk]) -> dict[str, Source]:
    by_code: dict[str, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_code[c.page].append(c)
    return {(k := code(e)): Source(k, e, by_code[k]) for e in extracts}


# --- The plan: comparison with D1 ---


@dataclass
class Plan:
    new: list[Source] = field(default_factory=list)
    changed: list[Source] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)  # codes with a different text_hash
    returned: list[Source] = field(default_factory=list)  # were absent, came back the same
    to_mark: list[str] = field(default_factory=list)  # absent for the first time
    to_delete: dict[str, int] = field(default_factory=dict)  # code → chunk_count in Vectorize
    # Same URL under another code today (e.g. a notice that became an external page): the old
    # source goes at once, before the new one is written (URLs are unique in D1).
    renamed: dict[str, str] = field(default_factory=dict)  # old code → new code
    # Documents that no longer exist (`gone` of the crawler: login page, 404): deleted at once
    gone: list[str] = field(default_factory=list)
    # Text changed but same passages: nothing to write in Vectorize, only the new text_hash in D1
    text_only: list[Source] = field(default_factory=list)
    leftovers: list[str] = field(default_factory=list)  # IDs of leftover chunks (shorter sources)
    registry: dict[str, dict] = field(default_factory=dict)  # rows of `sources` before the run

    @property
    def to_write(self) -> list[Source]:
        return self.new + self.changed

    def chunks_to_write(self) -> list[Chunk]:
        return [c for s in self.to_write for c in s.chunks]

    def ids_to_delete(self) -> list[str]:
        gone = [f"{k}-{n}" for k, total in self.to_delete.items() for n in range(total)]
        gone += [
            f"{k}-{n}" for k in self.renamed for n in range(int(self.registry[k]["chunk_count"]))
        ]
        return self.leftovers + gone


MAX_ABSENT = 0.3  # share of D1's sources that may be missing in a run
MAX_ABSENT_PER_KIND = 0.5  # share of a kind (pages, sheets, PDFs…) that may be missing
MIN_PER_KIND = 5  # below this number of sources the per-kind check is not done


class CrawlerStopped(RuntimeError):
    """The crawler stopped (site unreachable, 503, 429…): the copy is incomplete."""


class TooManyAbsent(RuntimeError):
    """Too many sources missing compared with D1: a site down or changed is more likely than sources
    removed. The message holds only numbers and kinds."""


def check_absences(today: dict[str, Source], registry: dict[str, dict]) -> None:
    """Before marking sources absent (and deleting them at the next run): is it plausible?"""
    absent = [k for k in registry if k not in today]
    if registry and len(absent) > MAX_ABSENT * len(registry):
        raise TooManyAbsent(f"{len(absent)} sources absent of {len(registry)}")
    by_kind: dict[str, list[str]] = defaultdict(list)
    for k, row in registry.items():
        by_kind[row.get("kind", "")].append(k)
    for kind, codes in sorted(by_kind.items()):
        missing = sum(k not in today for k in codes)
        if len(codes) >= MIN_PER_KIND and missing > MAX_ABSENT_PER_KIND * len(codes):
            raise TooManyAbsent(f"{missing} sources of kind {kind} absent of {len(codes)}")


def make_plan(
    today: dict[str, Source], registry: dict[str, dict], gone: frozenset[str] = frozenset()
) -> Plan:
    """Today's sources against the registry in D1 (`sources`): what to write and what to delete.
    `gone`: URLs of documents that no longer exist (crawler): deleted without waiting."""
    p = Plan(registry=registry)
    by_url = {s.extract["url"]: k for k, s in today.items()}
    for k, row in registry.items():
        if k not in today and by_url.get(row.get("url")) not in (None, k):
            p.renamed[k] = by_url[row["url"]]
    for k, source in today.items():
        row = registry.get(k)
        if row is None:
            p.new.append(source)
            continue
        if row["hash"] != source.hash:
            p.changed.append(source)
            p.leftovers += [f"{k}-{n}" for n in range(len(source.chunks), int(row["chunk_count"]))]
        elif row["absent_since"] is not None:
            p.returned.append(source)
        if row["text_hash"] != source.text_hash:
            p.modified.append(k)
            if row["hash"] == source.hash:
                p.text_only.append(source)
    for k, row in registry.items():
        if k in today or k in p.renamed:
            continue
        if row.get("url") in gone:
            p.gone.append(k)
            p.to_delete[k] = int(row["chunk_count"])
        elif row["absent_since"] is None:
            p.to_mark.append(k)
        else:  # absent at the previous run too: 2 runs in a row
            p.to_delete[k] = int(row["chunk_count"])
    return p


# --- Writes to D1 ---


def _midnight(text: str) -> int | None:
    d = italian_date(text or "")
    return int(datetime.combine(d, clock(0, 0), tzinfo=TIMEZONE).timestamp()) if d else None


def statements_of_source(
    source: Source, now: int, before: dict | None = None, changed: bool = False
) -> list[Statement]:
    """Row in `sources` plus its `notices`, `tables`, `copies`, `events` and `acronyms`, in one
    atomic batch.

    `before`: the row in D1 before the run (None if the source is new), to keep the dates of
    `/updates`; `changed`: UniCa changed its text in this run."""
    e, k = source.extract, source.code
    year = e.get("year") or "all"
    added = before.get("added_at") if before else now
    changed_at = now if changed else (before.get("changed_at") if before else None)
    statements: list[Statement] = [
        ("delete from notices where code = ?", [k]),
        ("delete from tables where source = ?", [k]),
        ("delete from copies where source = ?", [k]),
        ("delete from events where source = ?", [k]),
        ("delete from acronyms where source = ?", [k]),
        (
            "insert or replace into sources (code, url, title, kind, year, hash, text_hash, "
            "chunk_count, updated_at, absent_since, added_at, changed_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?)",
            [k, e["url"], e["title"], e["kind"], year, source.hash, source.text_hash,
             len(source.chunks), now, added, changed_at],
        ),
    ]  # fmt: skip
    if e["kind"] == "notice":
        until, deadline = valid_until(e)
        statements.append(
            (
                "insert into notices (code, title, url, published_at, deadline, valid_until) "
                "values (?, ?, ?, ?, ?, ?)",
                [
                    k,
                    e["title"],
                    e["url"],
                    _midnight(e.get("date", "")),
                    end_of_day(deadline) if deadline else None,
                    until,
                ],
            )  # fmt: skip
        )
    statements += [
        ("insert into copies (chunk, source, also_for) values (?, ?, ?)", [c.id, k, c.copy_of])
        for c in source.chunks
        if c.copy_of
    ]
    statements += [
        (
            "insert into events (id, source, position, title, date_text, credits, url) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            [f"{k}-v{i}", k, i, v["title"], v["date"], v["credits"], v["url"]],
        )
        for i, v in enumerate(events(e))
    ]
    statements += [
        ("insert into acronyms (source, code, name) values (?, ?, ?)", [k, a, name])
        for a, name in acronyms(e)
    ]
    for i, t in enumerate(e.get("tables") or []):
        statements.append(
            (
                "insert into tables (id, source, pdf_page, context, year, markdown, hash) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                [
                    f"{k}-t{i}",
                    k,
                    t.get("pdf_page"),
                    t.get("context", ""),
                    year,
                    t["markdown"],
                    fingerprint(t["markdown"]),
                ],
            )  # fmt: skip
        )
    return statements


def d1_statements(plan: Plan, now: int) -> list[list[Statement]]:
    """Groups of statements: each group is atomic and a source is never split between two groups."""
    old_code = {new: old for old, new in plan.renamed.items()}
    per_source = [[  # first: the old code of a URL that changed code, before its new row
        ("delete from notices where code = ?", [k]), ("delete from tables where source = ?", [k]),
        ("delete from copies where source = ?", [k]), ("delete from events where source = ?", [k]),
        ("delete from acronyms where source = ?", [k]),
        ("delete from sources where code = ?", [k])] for k in plan.renamed
    ]  # fmt: skip
    per_source += [
        statements_of_source(
            s,
            now,
            plan.registry.get(s.code) or plan.registry.get(old_code.get(s.code, "")),
            s.code in plan.modified,
        )
        for s in plan.to_write
    ]
    per_source += [
        [("update sources set absent_since = null where code = ?", [s.code])] for s in plan.returned
    ]
    per_source += [
        [("update sources set text_hash = ? where code = ?", [s.text_hash, s.code])]
        for s in plan.text_only
    ]
    per_source += [
        [("update sources set absent_since = ? where code = ?", [now, k])] for k in plan.to_mark
    ]
    per_source += [
        [("delete from notices where code = ?", [k]), ("delete from tables where source = ?", [k]),
         ("delete from copies where source = ?", [k]), ("delete from events where source = ?", [k]),
         ("delete from acronyms where source = ?", [k]),
         ("delete from sources where code = ?", [k])]
        for k in plan.to_delete
    ]  # fmt: skip
    groups: list[list[Statement]] = []
    for block in per_source:
        if groups and len(groups[-1]) + len(block) <= STATEMENTS_PER_BATCH:
            groups[-1] += block
        else:
            groups.append(list(block))
    return groups


# --- The services (real or, in the tests, fake) ---


class Services:
    """Real Cloudflare: D1, Vectorize and Workers AI through the HTTP API (`core/`)."""

    def registry(self) -> dict[str, dict]:
        rows = db.d1(
            "select code, url, kind, hash, text_hash, chunk_count, absent_since, added_at, "
            "changed_at from sources"
        )
        return {r["code"]: r for r in rows}

    def last_version(self) -> str | None:
        """Extraction version of the last successful run (None if there is none)."""
        rows = db.d1(
            "select extraction_version from runs where outcome = 'ok' order by id desc limit 1"
        )
        return rows[0]["extraction_version"] if rows else None

    def embedding(self, texts: list[str]) -> tuple[list[list[float]], float]:
        result = embedding(texts)
        return result.vectors, result.neurons

    def insert(self, vectors: list[db.Vector]) -> list[str]:
        return db.insert(vectors)

    def delete(self, ids: list[str]) -> list[str]:
        return db.delete(ids)

    def wait(self, mutation: str) -> None:
        db.wait(mutation)

    def d1_batch(self, statements: list[Statement]) -> None:
        db.d1_batch(statements)

    def table_counts(self) -> dict[str, int]:
        """Source code → number of its tables in D1."""
        rows = db.d1("select source, count(*) as n from tables group by source")
        return {r["source"]: int(r["n"]) for r in rows}


# --- The run ---


@dataclass
class Outcome:
    plan: Plan
    chunks_written: int = 0
    chunks_deleted: int = 0
    neurons: float = 0.0


def apply(plan: Plan, services: Services, now: int) -> Outcome:
    """First Vectorize (inserts, then deletes, then wait), then D1."""
    outcome = Outcome(plan)
    to_write = plan.chunks_to_write()
    vectors: list[db.Vector] = []
    for i in range(0, len(to_write), EMBEDDING_BATCH):
        batch = to_write[i : i + EMBEDDING_BATCH]
        values, neurons = services.embedding([c.text for c in batch])
        outcome.neurons += neurons
        vectors += [db.Vector(c.id, v, c.metadata()) for c, v in zip(batch, values, strict=True)]
    mutations = services.insert(vectors) if vectors else []
    ids = plan.ids_to_delete()
    mutations += services.delete(ids) if ids else []
    if mutations:
        services.wait(mutations[-1])
    outcome.chunks_written, outcome.chunks_deleted = len(vectors), len(ids)
    for group in d1_statements(plan, now):
        services.d1_batch(group)
    return outcome


def record_run(
    services: Services,
    start: int,
    outcome: Outcome | None,
    error: str | None,
    version: str | None,
    success: str = "ok",
) -> None:
    p = outcome.plan if outcome else Plan()
    services.d1_batch([(
        "insert into runs (started_at, ended_at, new_sources, changed_sources, modified_sources, "
        "absent_sources, deleted_sources, chunks_written, chunks_deleted, neurons, outcome, error, "
        "extraction_version) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [start, int(time.time()), len(p.new), len(p.changed), len(p.modified),
         len(p.to_mark), len(p.to_delete), outcome.chunks_written if outcome else 0,
         outcome.chunks_deleted if outcome else 0, round(outcome.neurons, 3) if outcome else 0,
         "error" if error else success, error, version],
    )])  # fmt: skip


def update(
    extracts: list[dict],
    services: Services,
    dry_run: bool = False,
    accept_absences: bool = False,
    additions_only: bool = False,
    gone: frozenset[str] = frozenset(),
) -> Outcome:
    """The run. `additions_only`: the daily notices run, which sees only the new sources and so
    marks no absence and deletes nothing."""
    start = int(time.time())
    today = todays_sources(extracts, chunks_of_all(extracts))
    registry = services.registry()
    plan = make_plan(today, registry, gone)
    if additions_only:
        plan.to_mark, plan.to_delete = [], {}
    version = extraction_version()
    if plan.modified and services.last_version() != version:
        log.info(
            "extraction code changed: %d different texts do not count as changes by UniCa",
            len(plan.modified),
        )
        plan.modified = []
    describe(plan)
    for message in losses(today, registry, services.table_counts()):
        warn(f"source with much less content than last run: {message}")
    found = personal_data(today.values())
    if dry_run:
        if found:
            raise PersonalDataFound("personal data in " + ", ".join(found))
        return Outcome(plan)
    try:
        if found:  # stop before anything reaches Vectorize and D1
            raise PersonalDataFound("personal data in " + ", ".join(found))
        if not accept_absences and not additions_only:
            check_absences(today, registry)
        outcome = apply(plan, services, start)
    except Exception as e:
        try:  # if D1 does not answer either, the error to show stays the original one
            record_run(services, start, None, type(e).__name__, version)
        except Exception as other:
            log.error("run not recorded in D1: %s", type(other).__name__)
        raise
    record_run(services, start, outcome, None, version, "ok_notices" if additions_only else "ok")
    return outcome


def describe(p: Plan) -> None:
    """Summary in the log: counts, codes and URLs, never texts."""
    to_write = p.chunks_to_write()
    neurons = sum(len(c.text) for c in to_write) / CHARS_PER_TOKEN * NEURONS_PER_TOKEN
    log.info(
        "new sources %d · changed %d (modified by UniCa %d) · returned %d · absent %d · "
        "to delete %d · chunks to write %d (~%.0f neurons) · chunks to delete %d",
        len(p.new), len(p.changed), len(p.modified), len(p.returned), len(p.to_mark),
        len(p.to_delete), len(to_write), neurons, len(p.ids_to_delete()),
    )  # fmt: skip
    for s in p.new:
        log.info("new: %s %s", s.code, s.extract["url"])
    for k in p.modified:
        url = next((s.extract["url"] for s in p.changed if s.code == k), "")
        log.info("modified by UniCa: %s %s", k, url)
    for k in p.to_mark:
        log.info("absent (first run): %s", k)
    for k in p.to_delete:
        if k in p.gone:
            log.info("gone (login page or 404), to delete now: %s %s", k, p.registry[k]["url"])
        else:
            log.info("absent for 2 runs, to delete: %s", k)
    for old, new in p.renamed.items():
        log.info("same address under a new code, replaced: %s → %s", old, new)


# --- Cohorts ---

# Documents depending on the enrolment year: each running cohort must have them.
COHORT_DOCUMENTS = {
    "Regolamento didattico": re.compile(r"^Regolamento didattico\b", re.I),
    "Percorso formativo": re.compile(r"^Percorso formativo\b", re.I),
}


def incomplete_cohorts(extracts: list[dict], today: date) -> list[str]:
    """For the two cohorts of the bot's filter, the missing documents (e.g. in September, before
    UniCa publishes those of the new year)."""
    missing = []
    for year in filter_years(current_year(today)):
        titles = [e["title"] for e in extracts if e.get("year") == year]
        for name, pattern in COHORT_DOCUMENTS.items():
            if not any(pattern.match(t) for t in titles):
                missing.append(f"{year}: missing {name}")
    return missing


def cohorts_to_report(extracts: list[dict], today: date, additions_only: bool) -> list[str]:
    """Like `incomplete_cohorts`, but not in the daily notices run, which does not see the
    regulations and would report two "incomplete" cohorts every day."""
    return [] if additions_only else incomplete_cohorts(extracts, today)


def warn(message: str) -> None:
    """A warning that does not stop the run; on GitHub Actions it shows in the run's summary."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else ""
    log.warning("%s%s", prefix, message)


NOTHING_WRITTEN = "Nothing was written or deleted: the bot keeps the data of the last run."


def fail(message: str) -> None:
    """Why the run stopped, in plain words; on GitHub Actions it shows in the run's summary."""
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else ""
    log.error("%s%s", prefix, message)


def _record_error(kind: str) -> None:
    try:
        record_run(Services(), int(time.time()), None, kind, None)
    except Exception as other:
        log.error("run not recorded in D1: %s", type(other).__name__)


def main() -> int:
    args = argparse.ArgumentParser(description="Pipeline run to Vectorize and D1")
    args.add_argument("--dry-run", action="store_true", help="says what it would do, no writes")
    args.add_argument(
        "--accept-absences", action="store_true", help="many sources removed on purpose: no check"
    )
    args.add_argument(
        "--additions-only",
        action="store_true",
        help="notices run (cache/summary_notices.json): no absences or deletes",
    )
    options = args.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    name = "summary_notices.json" if options.additions_only else "summary.json"
    summary = json.loads((ROOT / "cache" / name).read_text(encoding="utf-8"))
    if summary.get("stopped"):
        fail(
            f"run stopped: the crawler stopped ({summary['stopped']}), so the copy of the site is "
            f"incomplete. {NOTHING_WRITTEN} Usually the site is down: run again later."
        )
        if not options.dry_run:
            _record_error(CrawlerStopped.__name__)
        return 1
    extracts = extract_all(summary)
    if bad := [e for e in extracts if e.problems]:
        hint = (
            "" if details() else " With PIPELINE_DETAILS=1 the log also shows the missing values."
        )
        fail(
            f"run stopped: {len(bad)} source(s) not read correctly. {NOTHING_WRITTEN} UniCa "
            "probably changed their format: adapt the reader (pipeline/tables/ for the tables of "
            f"the PDFs, pipeline/extract.py for pages and texts).{hint}"
        )
        for e in bad:
            fail(f"{e.title or 'untitled'} · {e.url} · {'; '.join(e.problems)}")
        return 1  # the run fails and GitHub sends an email
    records = [asdict(e) for e in extracts]
    for message in cohorts_to_report(
        records, datetime.now(TIMEZONE).date(), options.additions_only
    ):
        warn(message)
    try:
        outcome = update(
            records,
            Services(),
            options.dry_run,
            options.accept_absences,
            options.additions_only,
            frozenset(summary.get("gone", [])),
        )
    except TooManyAbsent as e:
        fail(
            f"run stopped: {e}, too many compared with D1 (site down, or changed?). "
            f"{NOTHING_WRITTEN} If they were removed on purpose, run with --accept-absences."
        )
        return 1
    except PersonalDataFound as e:
        fail(
            f"run stopped: {e} (email or phone number left in the texts). {NOTHING_WRITTEN} "
            "Fix the removal of contacts in pipeline/extract.py."
        )
        return 1
    except Exception as e:
        fail(
            f"run failed: {type(e).__name__}. Vectorize is written first and D1 after, and D1 says "
            "what is in Vectorize: the next run starts again from D1."
            + ("" if details() else " With PIPELINE_DETAILS=1 the log shows the full error.")
        )
        if details():
            raise  # the traceback says where and why
        return 1
    if not options.dry_run:
        log.info(
            "done: %d chunks written, %d deleted, %.1f neurons",
            outcome.chunks_written, outcome.chunks_deleted, outcome.neurons,
        )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
