"""Crawler of the Master's degree: downloads to `cache/` the menu pages, linked PDFs and notices.

Usage (from the root, NEVER from the university network):
  uv run python -m pipeline.crawl                 # reduced mode (CRAWLER_FULL=0)
  uv run python -m pipeline.crawl --cache-only    # no request: only the local copy
  uv run python -m pipeline.crawl --use-cache     # downloads only what the local copy lacks
  uv run python -m pipeline.crawl --notices-only  # daily run: new notices of the first page
                                                # (reads the indexed ones from D1)

The summary (`cache/summary.json`) keeps for each document the page it comes from and the link text
(it often holds the academic year), for each notice title, date and summary from the list.

Text extraction is a separate step and works on the copy in `cache/`.
The full run (`CRAWLER_FULL=1`) is meant for GitHub Actions; on the PC the reduced mode is enough.
"""

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from bs4 import BeautifulSoup

from core import db
from core.config import ROOT, crawler, crawler_headers
from pipeline.catalogue import Course, course_list, current_year, has_texts, read_json
from pipeline.download import Downloader, Response, StopCrawl
from pipeline.sources import Sources, keep_only_section, normalise, sources

log = logging.getLogger("pipeline.crawl")

CACHE_FOLDER = ROOT / "cache"
SUMMARY_FILE = CACHE_FOLDER / "summary.json"
# The notices run writes apart: the full run's summary stays untouched
NOTICES_SUMMARY_FILE = CACHE_FOLDER / "summary_notices.json"


@dataclass
class Document:
    url: str
    page: str  # first course page linking it
    link_text: str  # text of the link on that page
    year: int | None = None  # starting academic year (2025 = 2025/26), from the link text


# Academic year in the text of a link ("Regolamento 2025/2026", "Orario 26/27"), to decide what to
# download. Not the same job as `extract.ACADEMIC_YEAR` (rewrites years in texts) nor
# `cohorts.ACADEMIC_YEAR` (texts already rewritten).
ACADEMIC_YEAR = re.compile(r"\b(20\d{2})\s*[/_\-–]\s*(?:20)?\d{2}\b|\b(\d{2})/(\d{2})\b")


def year_from_text(text: str) -> int | None:
    """Starting year of the academic year mentioned: "2025/2026" → 2025, "A.A. 26/27" → 2026."""
    found = ACADEMIC_YEAR.search(text)
    if found is None:
        return None
    if found.group(1):
        return int(found.group(1))
    start, end = int(found.group(2)), int(found.group(3))
    return 2000 + start if end == start + 1 else None


@dataclass
class Notice:
    url: str
    title: str  # from the list of notices
    date: str  # as written in the list, e.g. "25 settembre 2026"
    summary: str


@dataclass
class Summary:
    pages: list[str] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    courses: list[Course] = field(default_factory=list)  # sheets of the Cineca catalogue
    external: list[str] = field(default_factory=list)  # pages outside the course chosen by hand
    skipped: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)  # documents of D1 that no longer exist
    stopped: str | None = None


def _soup(r: Response) -> BeautifulSoup:
    return BeautifulSoup(r.content, "html.parser")


def menu_pages(r: Response, s: Sources) -> list[str]:
    """The course pages in the menu (#menup), in order and without duplicates."""
    pages: list[str] = []
    for a in _soup(r).select("#menup a[href]"):
        url = normalise(a["href"], r.url)
        if (
            url.startswith(s["page_prefix"])
            and url != s["start_page"]  # the menu also links the page itself ("Ita")
            and url not in s["excluded_pages"]
            and url not in pages
            and s.allowed(url)
        ):
            pages.append(url)
    return pages


# Attachments that are not text to extract (images, Office): recognised from the link text
SKIPPED_ATTACHMENTS = re.compile(r"\.(png|jpe?g|gif|svg|webp|docx?|xlsx?|pptx?|zip|rar)$", re.I)


def subpages(r: Response, s: Sources) -> list[str]:
    """Course pages linked in the content and in the side menus (#main_container)."""
    pages: list[str] = []
    for a in _soup(r).select("#main_container a[href]"):
        url = normalise(a["href"], r.url)
        if (
            url.startswith(s["page_prefix"])
            and "?" not in url  # pagination is followed apart
            and url not in s["excluded_pages"]
            and url not in pages
            and s.allowed(url)
        ):
            pages.append(url)
    return pages


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def documents_of_page(r: Response, page: str, s: Sources) -> list[Document]:
    """The documents linked in the page's content (not in the menus), with the link text."""
    content = _soup(r).select_one("#main_container")
    if content is None:
        return []
    title = s.kept_section(page)
    if title and not keep_only_section(content, title, s.follows_linked(page)):
        return []  # section gone: the page's extraction reports it
    documents: list[Document] = []
    previous_year: int | None = None
    for a in content.select("a[href]"):
        url = normalise(a["href"], r.url)
        if s.is_document(url) and url not in [d.url for d in documents]:
            text = _text(a) or a.get("title", "")
            if SKIPPED_ATTACHMENTS.search(text):
                continue
            year = year_from_text(text) or previous_year
            previous_year = year
            if any(re.search(e, text, re.I) for e in s["excluded_documents"]):
                continue
            if year is not None and not s["min_year"] <= year <= s["max_year"]:
                continue  # not one of the two running cohorts
            documents.append(Document(url=url, page=page, link_text=text, year=year))
    return documents


def linked_contents(r: Response, page: str, s: Sources) -> list[str]:
    """The web.unica.it pages in the list of linked contents (`div.article-list`), only for the
    pages in `linked_contents` of sources.yaml."""
    if not s.follows_linked(page):
        return []
    linked: list[str] = []
    for a in _soup(r).select("#main_container div.article-list h4 a[href]"):
        url = normalise(a["href"], r.url)
        if (
            url.startswith("https://web.unica.it/")
            and not url.startswith(s["page_prefix"])  # course pages are followed anyway
            and not s.is_document(url)
            and url not in linked
            and s.allowed(url)
        ):
            linked.append(url)
    return linked


def _next(soup: BeautifulSoup, base: str) -> str | None:
    """The "Avanti" link of a list split into pages, if any."""
    for a in soup.select("#main_container ul.pagination a[href]"):
        if a.get_text(strip=True).lower() == "avanti":
            return normalise(a["href"], base)
    return None


def next_page(r: Response, s: Sources) -> str | None:
    """The "Avanti" link of a list split into pages, if any and allowed."""
    url = _next(_soup(r), r.url)
    return url if url is not None and s.allowed(url) else None


def notices_of_list(r: Response) -> tuple[list[Notice], str | None]:
    """The notices of a page of the list (newest first) and the next page, if any."""
    soup = _soup(r)
    notices: list[Notice] = []
    for article in soup.select("#main_container article"):
        a = article.select_one("h4 a[href]")
        if a is None:
            continue
        url = normalise(a["href"], r.url)
        if url not in [n.url for n in notices]:
            notices.append(
                Notice(
                    url=url,
                    title=_text(a),
                    date=_text(article.select_one(".data")),
                    summary=_text(article.select_one("p")),
                )
            )
    return notices, _next(soup, r.url)


def collect_notices(d: Downloader, s: Sources, how_many: int) -> list[Notice]:
    """The `how_many` newest notices, reading the pages of the list only as long as needed."""
    notices: list[Notice] = []
    url: str | None = s["notices"]
    while url is not None and len(notices) < how_many:
        page = d.get(url)
        if page is None or not page.is_html:
            break
        new, url = notices_of_list(page)
        notices += [n for n in new if n.url not in [v.url for v in notices]]
    return notices[:how_many]


def latest_years(documents: list[Document], rules: list[dict]) -> list[Document]:
    """For each rule (link text, number of years) keeps only the most recent years."""
    dropped: set[str] = set()
    for rule in rules:
        group = [d for d in documents if re.search(rule["link_text"], d.link_text, re.I)]
        years = sorted({d.year for d in group if d.year is not None}, reverse=True)
        kept = set(years[: rule["years"]])
        dropped |= {d.url for d in group if d.year is not None and d.year not in kept}
    return [d for d in documents if d.url not in dropped]


def _first[T](items: list[T], limit: int | None) -> list[T]:
    return items if limit is None else items[:limit]


def _catalogue(d: Downloader, s: Sources, limits: dict, summary: Summary) -> None:
    """Course sheets from the Cineca catalogue, starting from the "Insegnamenti" page."""
    page = s["catalogue"]["courses_page"]
    response = d.get(page)
    year = current_year(response.content, page, s) if response is not None else None
    if year is None:
        log.warning("catalogue: no link to the current year on the Insegnamenti page")
        return
    log.info("catalogue: subjects taught in %s", year.year)
    for course in _first(course_list(d, s, year), limits["courses"]):
        data = _sheet(d, course.url)
        if data is None:
            summary.skipped.append(course.url)
            continue
        # Sheet "being defined": the texts of the previous year, if any
        if has_texts(data) or not course.previous:
            course.previous = ""
        elif not has_texts(_sheet(d, course.previous)):
            course.previous = ""
        else:
            log.info("catalogue: previous year's texts for %s", course.name)
        summary.courses.append(course)


def _sheet(d: Downloader, url: str) -> dict | None:
    """The catalogue sheet, cleaned of personal data in the local copy too; None if missing or
    unreadable (the sheet ends up among the skipped ones, the run goes on)."""
    data = read_json(d, url)
    return data if isinstance(data, dict) else None


def _pages(
    d: Downloader,
    s: Sources,
    start: Response,
    limits: dict,
    summary: Summary,
    documents: list,
    linked: list[str],
) -> None:
    """The course pages: menu, extra pages and subpages found on the way, always inside the course
    prefix; for each also the next pages of the lists. In `linked` the pages outside the course of
    the linked contents (`linked_contents`)."""
    queue = [
        s["start_page"],
        *menu_pages(start, s),
        *[p for p in s["extra_pages"] if s.allowed(p)],
    ]
    seen: set[str] = set()
    while queue and (limits["pages"] is None or len(seen) < limits["pages"]):
        url: str | None = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        response = start if url == s["start_page"] else d.get(url)
        for _ in range(s["max_list_pages"] + 1):  # the page and its next ones
            if url is None:
                break
            if response is None or not response.is_html:
                summary.skipped.append(url)
                break
            summary.pages.append(url)
            known = [x.url for x in documents]
            documents += [x for x in documents_of_page(response, url, s) if x.url not in known]
            queue += [p for p in subpages(response, s) if p not in seen and p not in queue]
            linked += [c for c in linked_contents(response, url, s) if c not in linked]
            url = next_page(response, s)
            response = d.get(url) if url is not None else None


def run(d: Downloader, s: Sources, limits: dict) -> Summary:
    """One crawler run. On a stop it still returns what was downloaded until then."""
    summary = Summary()
    try:
        d.read_site_robots()
        start = d.get(s["start_page"])
        if start is None or not start.is_html:
            raise StopCrawl("start page not available")
        if not menu_pages(start, s):  # e.g. maintenance page with HTTP 200
            raise StopCrawl("course menu not found: site under maintenance or changed?")
        documents: list[Document] = []
        linked: list[str] = []
        _pages(d, s, start, limits, summary, documents, linked)
        notices = collect_notices(d, s, limits["notices"])
        _external_pages(d, external_to_download(s, linked, notices), summary)
        documents += [
            Document(x["url"], x["page"], x["link_text"], year_from_text(x["link_text"]))
            for x in s["external_documents"]
        ]
        _notices_with_attachments(d, s, notices, summary, documents)
        documents = latest_years(documents, s["latest_years_documents"])
        _documents(d, _first(documents, limits["documents"]), summary)
        _catalogue(d, s, limits, summary)
    except StopCrawl as e:
        summary.stopped = str(e)
    except KeyboardInterrupt:
        summary.stopped = "stopped by hand (Ctrl+C)"
    return summary


def _external_pages(d: Downloader, urls: list[str], summary: Summary) -> None:
    """Downloads the external pages. The address recorded is the one asked for, under which the
    local copy is saved, even when the site redirects elsewhere."""
    for url in urls:
        response = d.get(url)
        if response is not None and response.is_html:
            summary.external.append(url)
        else:
            summary.skipped.append(url)


def external_to_download(s: Sources, linked: list[str], notices: list[Notice]) -> list[str]:
    """The external pages chosen by hand and those of the linked contents, without duplicates. A
    linked page that is also among the run's notices stays a notice (one source, in /avvisi)."""
    of_notices = {n.url for n in notices}
    external = list(s["external_pages"])
    external += [c for c in linked if c not in external and c not in of_notices]
    return external


def _notices_with_attachments(
    d: Downloader, s: Sources, notices: list[Notice], summary: Summary, documents: list[Document]
) -> None:
    """Downloads the notices and adds their attachments to `documents` (with exceptions)."""
    for notice in notices:
        response = d.get(notice.url)
        if response is not None and response.is_html:
            summary.notices.append(notice)
            if any(re.search(e, notice.title, re.I) for e in s["notices_without_attachments"]):
                continue  # e.g. graduation calendars: graduates' names in the attachments
            known = [x.url for x in documents]
            documents += [
                x for x in documents_of_page(response, notice.url, s) if x.url not in known
            ]
        else:
            summary.skipped.append(notice.url)


def _documents(d: Downloader, documents: list[Document], summary: Summary) -> None:
    for document in documents:
        response = d.get(document.url)
        # A document answering with an HTML page usually asks for the login: skipped
        if response is not None and response.is_pdf:
            summary.documents.append(document)
        else:
            summary.skipped.append(document.url)


def run_notices(d: Downloader, s: Sources, known: set[str], how_many: int) -> Summary:
    """Daily run: only the first page of the notices list and only the notices not yet in D1,
    with their attachments. Usually 1–3 requests.

    `known`: the URLs of all the sources in D1. An attachment already in D1 is not downloaded again:
    if it is a course PDF (e.g. the exam calendar), here it would take the notice's deadline and
    disappear from the bot when the notice expires; the Sunday run reads it from the course page."""
    summary = Summary()
    try:
        d.read_site_robots()
        page = d.get(s["notices"])
        if page is None or not page.is_html:
            raise StopCrawl("list of notices not available")
        notices, _ = notices_of_list(page)
        new = [n for n in notices if n.url not in known][:how_many]
        documents: list[Document] = []
        _notices_with_attachments(d, s, new, summary, documents)
        documents = [x for x in documents if x.url not in known]
        _documents(d, latest_years(documents, s["latest_years_documents"]), summary)
    except StopCrawl as e:
        summary.stopped = str(e)
    except KeyboardInterrupt:
        summary.stopped = "stopped by hand (Ctrl+C)"
    return summary


def check_missing_documents(d: Downloader, known: list[str], summary: Summary) -> None:
    """The PDFs in D1 not found in this run (e.g. replaced by a new version, the old address now
    asks for the portal's login) are asked once more. A page instead of the PDF, 404/410 or a
    redirect to an excluded address: gone for sure (`gone`), the run deletes them at once instead of
    keeping them for 2 runs. A PDF still answering, network errors or 5xx: the usual absence."""
    found = {x.url for x in summary.documents}
    for url in known:
        if url not in found and document_state(d, url) == "gone":
            log.info("document gone: %s", url)
            summary.gone.append(url)


def document_state(d: Downloader, url: str) -> str:
    """`pdf` (still there), `gone` (a page instead of the PDF, e.g. the portal's login; 404/410; a
    redirect to an excluded address) or `error` (network error, 5xx: maybe only for today)."""
    response = None if url in d.gone else d.get(url)
    if response is not None:
        return "pdf" if response.is_pdf else "gone"
    return "gone" if url in d.gone else "error"


def check_documents(d: Downloader) -> int:
    """Read only (`--check-documents`): what each PDF of D1 answers today. Only URLs and states in
    the log; nothing written to D1 or Vectorize. 1 if some document is gone."""
    rows = db.d1("select url, absent_since from sources where kind = 'pdf' order by url")
    states = {"pdf": 0, "gone": 0, "error": 0}
    for row in rows:
        state = document_state(d, row["url"])
        states[state] += 1
        absent = " (absent in D1)" if row["absent_since"] is not None else ""
        log.info("%s%s: %s", state, absent, row["url"])
    log.info("PDFs in D1 %d · pdf %d · gone %d · error %d", len(rows), *states.values())
    return 1 if states["gone"] else 0


def _check_gone(d: Downloader, summary: Summary, full: bool) -> None:
    """`check_missing_documents` on D1's PDFs, only after a full run that did not stop. If D1 does
    not answer, or the site asks to stop, nothing is gone: the usual absence (2 runs) applies."""
    if not full or summary.stopped:
        return
    try:
        known = [row["url"] for row in db.d1("select url from sources where kind = 'pdf'")]
        check_missing_documents(d, known, summary)
    except StopCrawl as e:
        log.warning("check of the missing documents stopped: %s", e)
    except Exception as e:
        log.warning("check of the missing documents skipped: %s", type(e).__name__)


def _cleanup(d: Downloader, summary: Summary, full: bool) -> None:
    """At the end of the run, files of addresses no longer used leave the local copy. Only after a
    full run that did not stop: the reduced one uses few pages on purpose and would delete almost
    everything."""
    if not full or summary.stopped:
        log.info("local copy cleanup skipped (reduced or stopped run)")
        return
    try:
        removed = d.clean_cache()
    except StopCrawl as e:
        summary.stopped = str(e)
        return
    for f in removed:
        log.info("removed from the local copy: %s", f.name)
    log.info("local copy cleanup: %d files removed", len(removed))


def main() -> int:
    args = argparse.ArgumentParser(description="Crawler of the Master's degree")
    args.add_argument("--cache-only", action="store_true", help="no request: only the local copy")
    args.add_argument(
        "--use-cache", action="store_true", help="in full mode too: downloads only what is missing"
    )
    args.add_argument(
        "--check-documents",
        action="store_true",
        help="only reads: what each PDF in D1 answers today (pdf, gone, error)",
    )
    args.add_argument(
        "--notices-only",
        action="store_true",
        help="only the new notices of the first page of the list (daily run)",
    )
    options = args.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    c = crawler()
    s = sources()
    mode = "full" if c.full else "reduced"
    limits = s["limits"]["full" if c.full else "reduced"]
    d = Downloader(
        s,
        CACHE_FOLDER,
        headers=crawler_headers(),
        use_cache=not c.full or options.use_cache,
        max_requests=limits["max_requests"],
        errors_in_a_row=limits["errors_in_a_row"],
        cache_only=options.cache_only,
    )
    if options.check_documents:  # always the site, never the local copy
        d.use_cache, d.cache_only = False, False
        log.info("check of the PDFs in D1 · 1 request per second · no writes to D1 or Vectorize")
        try:
            d.read_site_robots()
            return check_documents(d)
        except StopCrawl as e:
            log.error("check stopped: %s", e)
            return 2
    if options.cache_only:
        mode += ", local copy only"
    if options.notices_only:
        mode += ", new notices only"
    log.info("mode %s · 1 request per second · limits %s", mode, limits)
    if options.notices_only:
        known = {row["url"] for row in db.d1("select url from sources")}
        log.info("sources already indexed in D1: %d", len(known))
        summary = run_notices(d, s, known, limits["notices"])
        summary_file = NOTICES_SUMMARY_FILE  # no cleanup: the run does not see the whole site
    else:
        summary = run(d, s, limits)
        _check_gone(d, summary, c.full)
        _cleanup(d, summary, c.full)
        summary_file = SUMMARY_FILE
    CACHE_FOLDER.mkdir(exist_ok=True)
    out = {"run_at": datetime.now(UTC).isoformat(timespec="seconds"), "mode": mode}
    out |= {"requests": d.requests, "from_cache": d.from_cache} | asdict(summary)
    summary_file.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info(
        "requests %d (from the local copy %d) · pages %d · documents %d · notices %d"
        " · courses %d · external %d · skipped %d · gone %d",
        d.requests, d.from_cache, len(summary.pages), len(summary.documents),
        len(summary.notices), len(summary.courses), len(summary.external), len(summary.skipped),
        len(summary.gone),
    )  # fmt: skip
    if summary.stopped:
        log.error("crawler stopped: %s", summary.stopped)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
