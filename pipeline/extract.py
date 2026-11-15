"""Text extraction from the local copy: one record per source, in Markdown, without menus.

Sources: HTML pages (of the course and external), notices, course sheets, PDFs.

The pages of web.unica.it all share the same structure inside `#main_container`:
- `.briciole`: the breadcrumb (UniCa › Informatica › Didattica), kept as section, not in the text;
- `.titolopagina`: the title (`h3`) and, in notices, the author;
- `section.contenuto`: the text, with the side menu `#menusx` next to it, which is dropped. The
  course home is the exception: presentation and contacts in `section.subsite_info` (the notices
  box is skipped, notices are extracted on their own).

Check: if the blocks (title, content) are missing the site changed structure and the run fails,
instead of sending empty pages to the bot. An empty page on the site (e.g. "Modulistica", only a
menu) is skipped; but if empty pages are too many (over 20%) the run fails too.
Personal data: emails and phone numbers are always removed (rule: of people only name and role);
in their place a pointer to the page, which the bot cites as source. Of students (e.g.
representatives, recognised by their @studenti.unica.it email) not even the name stays.

Course sheets (JSON of the Cineca catalogue, already cleaned when downloaded): text on a fixed
template, fields in Italian (in English only if the Italian is missing), HTML texts converted to
Markdown, identical module blocks kept once; link to the sheet's public page, not to the JSON.

PDFs: text with pdfplumber outside the table areas, which come from the readers of
`pipeline/tables/` as Markdown (for D1) and sentences (for the chunks); the regulation stops at
"Allegato 1". A table that fails the check, or a PDF without text (e.g. a scan), is a problem and
makes the run fail.

Academic years always written as "2026/2027" in the `year` field and in titles ("A.A. 26/27",
"2026-2027", "2026_2027" and "2026/27" are rewritten); the text of the sources stays as it is.

Identical texts at several addresses are kept once, with all the addresses.

Try it: uv run python -m pipeline.extract   (results in data/extracts.jsonl and data/extracts.md)
"""

import hashlib
import html
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pdfplumber
from bs4 import BeautifulSoup
from markdownify import markdownify

from core.config import ROOT
from pipeline.catalogue import SHEET_FIELDS
from pipeline.sources import keep_only_section, sources
from pipeline.tables import reader, regulation, timetable
from pipeline.tables.engine import markdown as table_markdown

log = logging.getLogger("pipeline.extract")

CACHE = ROOT / "cache"
OUTPUT = ROOT / "data"
MIN_CHARS = 40  # below this the page counts as empty
MAX_EMPTY = 0.2  # share of empty pages above which the site probably changed structure


@dataclass
class Extract:
    url: str
    kind: str  # "page", "external", "notice", "course", "pdf" (stored in D1)
    title: str
    section: str  # from the breadcrumb, e.g. "Informatica › Didattica"
    text: str  # Markdown
    date: str = ""  # notices only, as written on the site
    year: str = ""  # academic year, e.g. "2026/2027" (sheets, PDFs)
    linked_from: str = ""  # PDFs only: page or notice with the link (inherited deadline)
    tables: list[dict] = field(
        default_factory=list
    )  # PDFs only: page, context, markdown, sentences
    also: list[str] = field(default_factory=list)  # other addresses with the same content
    problems: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return len(_text(self.text)) < MIN_CHARS

    @property
    def hash(self) -> str:
        """Hash of text and tables: in PDFs the tables are outside `text`, and two PDFs with the
        same header (e.g. two calendars) have the same text but different tables."""
        tables = "".join("\n\n" + t["markdown"] for t in self.tables)
        return hashlib.sha256((self.text + tables).encode()).hexdigest()


# "2026/2027", "2026-27", "2026_2027"; two-digit forms ("26/27") only after "A.A.", otherwise
# "ore 15/16" would become an academic year
ACADEMIC_YEAR = re.compile(
    r"\b(?P<a>20\d{2})\s?[/_\-–]\s?(?P<b>20\d{2}|\d{2})\b"
    r"|(?P<aa>\ba\.?\s?a\.?\s*)(?P<c>\d{2})\s?[/_\-–]\s?(?P<d>\d{2})\b",
    re.I,
)


def academic_year(start: int) -> str:
    """2026 → "2026/2027": the only form of academic years in extracts and chunks."""
    return f"{start}/{start + 1}"


def normalise_years(text: str) -> str:
    """Rewrites academic years as "2026/2027" ("A.A. 26/27", "2026-27", "2026_2027"…). Only pairs
    of consecutive years: "12/02" or "2026/2030" stay as they are."""

    def replace(m: re.Match) -> str:
        if m.group("a"):
            start, end, before = int(m.group("a")), int(m.group("b")), ""
        else:
            start, end, before = 2000 + int(m.group("c")), int(m.group("d")), m.group("aa")
        end = end if end >= 2000 else 2000 + end
        return before + academic_year(start) if end == start + 1 else m.group(0)

    return ACADEMIC_YEAR.sub(replace, text)


# Special spaces of HTML and PDFs (non-breaking, thin, zero-width) → normal space:
# "Orario di\xa0ricevimento" must equal "Orario di ricevimento"
SPACES = str.maketrans({"\xa0": " ", " ": " ", " ": " ", "​": ""})


# Bold labels alone on a line ("**References:**", "**Objectives**"), as in "Attività a scelta dello
# studente"; references go from their label to the next one (or to the next heading).
LABEL = re.compile(r"^\*\*[^*\n]{2,60}\*\*\s*$|^#{1,6}\s")
BIBLIOGRAPHY = re.compile(
    r"^\*\*(References|Bibliograf|Riferimenti bibliografici)[^*\n]*\*\*\s*$", re.I
)


def without_bibliography(text: str) -> str:
    """Removes blocks of bibliographic references (links, DOIs, papers): they do not help students'
    questions and take places among the excerpts. The textbooks of the sheets ("Testi") stay."""
    lines, inside = [], False
    for line in text.split("\n"):
        if BIBLIOGRAPHY.match(line):
            inside = True
            continue
        if inside and LABEL.match(line):
            inside = False
        if not inside:
            lines.append(line)
    if len(lines) == text.count("\n") + 1:
        return text  # nothing to remove: identical text (same hash)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def normal_spaces(text: str) -> str:
    return text.translate(SPACES)


def cache_file(url: str) -> Path:
    return CACHE / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".bin")


def _text(x: str) -> str:
    return " ".join(x.split())


# Also the short form for several people: "{name1|name2}@unica.it"
EMAIL = re.compile(r"(\{[\w.%+|, -]+\}|[\w.%+-]+)@[\w-]+(\.[\w-]+)+")
# Landlines (070 123 4567, 070/123-4568, +39 070 1234569) and mobiles (333 000 0001)
# Not preceded by digits, letters, "/", "." or "-": so dates (12/02/2027) and DOIs stay intact
PHONE = re.compile(r"(?<![\w/.-])(\+39[\s/.-]?)?(0\d{1,3}|3\d{2})([\s/.-]?\d){6,8}\b")
# Times, dates, their ranges and ISBNs look like phone numbers ("09.00-11.00",
# "01/10/2026-31/01/2027", "ISBN 0262033844"): a number inside one of them is left as it is
NOT_A_PHONE = re.compile(
    r"(?<!\d)(?:\d{1,2}[.:]\d{2}(?:\s*[-–/]\s*\d{1,2}[.:]\d{2})+"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}(?:\s*[-–/]\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})*)(?!\d)"
    r"|\bISBN[\s:-]*[\dXx-]{10,17}",
    re.I,
)
EMAIL_PLACEHOLDER = "[email sul sito]"
PHONE_PLACEHOLDER = "[telefono sul sito]"


def without_contacts(text: str) -> str:
    """Removes emails and phone numbers (also in `mailto:` and `tel:` links)."""
    text = re.sub(r"\[([^\]]*)\]\((mailto|tel):[^)]*\)", r"\1", text)
    text = EMAIL.sub(EMAIL_PLACEHOLDER, text)
    return PHONE.sub(lambda m: m.group(0) if not_a_phone(text, m) else PHONE_PLACEHOLDER, text)


def not_a_phone(text: str, m: re.Match) -> bool:
    """True if the match falls inside a time, a date, a range of them or an ISBN."""
    return any(t.start() < m.end() and m.start() < t.end() for t in NOT_A_PHONE.finditer(text))


def _real_link(href: str) -> str:
    """Outlook's "safelinks" hide the real address (and the email of whoever copied it)."""
    if "safelinks.protection.outlook.com" in href:
        real = parse_qs(urlparse(href).query).get("url")
        return real[0] if real else ""
    return href


STUDENTS = "studenti.unica.it"
STUDENTS_PLACEHOLDER = "[nominativi degli studenti sul sito]"


def _without_students(content, soup) -> None:
    """Of students (e.g. representatives) the name is not kept (decided on 9 October 2026): the
    contacts block with a student's email becomes a pointer to the page."""
    for link in content.select(f'a[href^="mailto:"][href*="{STUDENTS}"]'):
        block = link.find_parent(class_=["contatti", "article-list"]) or link.find_parent("p")
        if block is not None and block.parent is not None:
            placeholder = soup.new_tag("p")
            placeholder.string = STUDENTS_PLACEHOLDER
            block.replace_with(placeholder)


PERSON_ADDRESS = re.compile(r"^\s*Indirizzo\s*:", re.I)


def _contacts_without_address(content) -> None:
    """In a person's contacts, drop the "Indirizzo: …" line (rule: of people only name and role).
    Person = paragraph of a `.contatti` block starting with the name in bold
    ("<strong>Vegvari Katalin</strong><br/>Indirizzo: …"). A place's address (page "Sede": same
    block, but the paragraph starts with "Indirizzo:") stays."""
    for paragraph in content.select(".contatti p"):
        first = next((x for x in paragraph.children if str(x).strip()), None)
        if getattr(first, "name", None) != "strong":
            continue
        for line in paragraph.find_all(string=PERSON_ADDRESS):
            line.replace_with("")


def _linked_contents(content, soup) -> None:
    """The list of linked contents (`.article-list` with date and linked title) becomes a bullet
    list "date: [title](link)" at the end of the block, before `_people_cards` takes it for people
    cards. Plain lines, not headings: they stay in the chunk with the text before."""
    items = []
    for entry in content.select(".article-list"):
        link = entry.select_one("h4 a[href]")
        if link is not None:
            item = soup.new_tag("li")
            date = entry.select_one(".data")
            if date is not None and _text(date.get_text()):
                item.append(_text(date.get_text()) + ": ")
            new = soup.new_tag("a", href=link["href"])
            new.string = _text(link.get_text())
            item.append(new)
            items.append(item)
        entry.decompose()
    if items:
        intro = soup.new_tag("p")
        intro.string = "Contenuti collegati:"
        bullets = soup.new_tag("ul")
        for item in items:
            bullets.append(item)
        content.append(intro)
        content.append(bullets)


def _people_cards(content, soup) -> None:
    """People cards (`.article-list` with the name in `h4`): one line stays with name, role and
    department; photo, address, phone and email go."""
    for card in content.select(".article-list"):
        name = card.select_one("h4")
        if name is None:
            continue
        role = card.select_one(".tematiche")
        department = next(
            (a for a in card.select("a") if a.find_previous(string=re.compile("Struttura"))), None
        )
        parts = []
        if role is not None and _text(role.get_text()):
            parts.append(_text(role.get_text()))
        if department is not None and department.find_parent("h4") is None:
            parts.append(_text(department.get_text()))
        line = soup.new_tag("p")
        bold = soup.new_tag("strong")
        bold.string = _text(name.get_text())
        line.append(bold)
        if parts:
            line.append(", " + ", ".join(parts))
        card.replace_with(line)


def _markdown(content, base: str) -> str:
    """HTML → Markdown: absolute links, no images or scripts, blank lines compacted."""
    for tag in content.select("script, style, img, iframe, form, button, #menusx"):
        tag.decompose()
    for a in content.select("a[href]"):
        a["href"] = _real_link(urljoin(base, a["href"]))
        a.attrs.pop("title", None)  # it usually repeats the link text
    text = markdownify(str(content), heading_style="ATX", bullets="-", strip=["span"])
    text = without_contacts(text)
    text = re.sub(rf"({re.escape(STUDENTS_PLACEHOLDER)}\s*)+", STUDENTS_PLACEHOLDER + "\n\n", text)
    text = re.sub(
        r"^.{0,30}(Telefono|Tel\.?|Fax[\w ]*|E-?mail|Recapito telefonico)\s*:?\s*\n?\s*"
        r"(\[(telefono|email) sul sito\][\s,;/e-]*)+.*$",
        "",
        text,
        flags=re.M | re.I,
    )
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text)  # spaces of justified text
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_page(
    html: bytes, url: str, kind: str, section: str | None = None, with_list: bool = False
) -> Extract:
    """Text of the page; with `section` only the block opened by that title (and, with
    `with_list`, the list of linked contents)."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main_container")
    if main is None:
        return Extract(url, kind, "", "", "", problems=["block #main_container missing"])
    breadcrumb = [_text(x.get_text()) for x in main.select(".briciole a, .briciole span")]
    title = main.select_one(".titolopagina h3")
    content = main.select_one("section.contenuto") or main.select_one("section.subsite_info")
    section_missing = (
        section is not None
        and content is not None
        and not keep_only_section(content, section, with_list)
    )
    if content is not None and with_list and not section_missing:
        _linked_contents(content, soup)
    if content is not None:
        _without_students(content, soup)
        _contacts_without_address(content)
        _people_cards(content, soup)
    text = _markdown(content, url) if content is not None else ""
    if title is None and main.select_one("section.subsite_info") is not None:
        title = soup.select_one(".slide-titolo")  # course home: name of the course
    e = Extract(
        url=url,
        kind=kind,
        title=_text(title.get_text()) if title else "",
        section=" › ".join(b for b in breadcrumb[1:-1] if b),  # without "UniCa" and the page itself
        text=text,
    )
    if not e.title:
        e.problems.append("title missing")
    if content is None:
        e.problems.append("content block missing")
    if section_missing:
        e.problems.append(f"section «{section}» not found")
    return e


CATALOGUE = "https://unica.coursecatalogue.cineca.it"


def _value(data: dict, name: str) -> str:
    """The field in Italian, or in English if the Italian is missing; "none" counts as empty."""
    for language in ("_it", "_en"):
        v = str(data.get(name + language) or "").strip()
        if v and v.lower() != "none":
            return v
    return ""


def _sheet_text(v: str) -> str:
    """Text of a sheet field: HTML (even escaped, &lt;b&gt;) to Markdown."""
    v = html.unescape(v)
    if not re.search(r"<[a-z][^>]*>", v, re.I):
        return without_contacts(v.strip())
    return _markdown(BeautifulSoup(v, "html.parser"), CATALOGUE)


def _person(name: str) -> str:
    return " ".join(p.capitalize() for p in name.split())


def _sheet_blocks(data: dict, note: str = "") -> list[str]:
    """The texts of the sheet by block (modules often repeat the whole text: kept once). `note` is
    added to each heading (e.g. texts taken from the previous year)."""
    blocks: list[str] = []
    for block in data.get("testiTotali") or []:
        parts = []
        for name, heading in SHEET_FIELDS:
            if v := _value(block, name):
                parts.append(f"## {heading}{note}\n\n{_sheet_text(v)}")
        text = "\n\n".join(parts)
        if text and text not in blocks:
            blocks.append(text)
    return blocks


def extract_sheet(data: dict, api_url: str, previous: dict | None = None) -> Extract:
    """Sheet on a fixed template. If the sheet is "being defined" and there is the previous year's
    one (`previous`), the texts come from there, with the year written in every heading."""
    year = str(data.get("aa") or "")
    year = academic_year(int(year)) if year.isdigit() else year
    lines = []
    items = [
        ("Codice", data.get("adCod")),
        ("CFU", data.get("crediti")),
        ("SSD", data.get("ssd")),
        ("Tipo", ", ".join(x for x in (_value(data, "tipo"), _value(data, "tipo_ins_des")) if x)),
        ("Periodo", _value(data, "periodo_didattico")),
        ("Lingua", _value(data, "lingua_des").capitalize()),
        ("Frequenza", _value(data, "frequenza")),
        ("Esame", ", ".join(
            x for x in (_value(data, "tipoEsaDes"), _value(data, "valutazione")) if x
        )),
        ("Ore", (data.get("durata") or {}).get("totale") or None),
    ]  # fmt: skip
    lines += [f"- **{k}:** {v}" for k, v in items if v not in (None, "", "None", "0")]
    holders = {d["des"] for d in data.get("titolari") or []}
    teachers = [
        _person(d["des"]) + (" (titolare)" if d["des"] in holders else "")
        for d in data.get("docenti") or []
    ]
    if teachers:
        lines.append("- **Docenti:** " + ", ".join(teachers))
    paths = [_value(data, "corso_percorso_des")] + [
        _value(p, "corso_percorso_des") for p in data.get("altriPercorsi") or []
    ]
    if any(paths):
        lines.append("- **Percorsi:** " + ", ".join(p for p in paths if p))
    blocks = _sheet_blocks(data)
    if not blocks and previous:
        before = str(previous.get("aa") or "")
        before = academic_year(int(before)) if before.isdigit() else "precedente"
        lines.append(
            f"- **Programma:** quello del {year} non è ancora pubblicato sul catalogo; "
            f"i testi qui sotto sono dell'anno accademico {before}"
        )
        blocks = _sheet_blocks(previous, f" (programma {before})")
    public = CATALOGUE + data["url"] if str(data.get("url", "")).startswith("/") else api_url
    e = Extract(
        url=public,
        kind="course",
        title=_person(_value(data, "des")),
        section="Catalogo degli insegnamenti",
        text="\n".join(lines) + ("\n\n" + "\n\n".join(blocks) if blocks else ""),
        year=year,
    )
    if not e.title or not data.get("crediti"):
        e.problems.append("sheet without name or CFU")
    return e


def _document_name(link_text: str) -> str:
    return normalise_years(re.sub(r"\s*(\.pdf|\(pdf\))\s*$", "", link_text, flags=re.I).strip())


def join_lines(text: str) -> str:
    """In PDFs lines wrap as on the page: a line that does not end the sentence and the next one
    starting in lower case are the same sentence ("…attività di" / "ricerca."). Hyphenated words
    ("informa-" / "tica") are joined."""
    result: list[str] = []
    for line in text.split("\n"):
        before = result[-1] if result else ""
        goes_on = (
            before.strip() and line[:1].islower() and not re.search(r"[.:;!?]$", before.strip())
        )
        if goes_on and before.endswith("-"):
            result[-1] = before[:-1] + line
        elif goes_on:
            result[-1] = before.rstrip() + " " + line.lstrip()
        else:
            result.append(line)
    return "\n".join(result)


def _pdf_text(content: bytes, without_tables: bool, end: tuple[int, float] | None) -> str:
    """Text of the pages, without the table areas (if a reader reads them) and without what comes
    after `end` (page, y)."""
    pages = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            if end and number > end[0]:
                break
            if end and number == end[0]:
                page = page.crop((0, 0, page.width, end[1] - 2))  # without the line itself
            if without_tables:
                for t in page.find_tables():
                    page = page.outside_bbox(t.bbox)
            lines = (page.extract_text() or "").splitlines()
            # no page numbers or index entries ("Art. 3 ……… 4")
            lines = [x for x in lines if not re.fullmatch(r"\s*\d{1,3}\s*|.*\.{5,}\s*\d*\s*", x)]
            pages.append("\n".join(lines))
    text = without_contacts(join_lines("\n\n".join(p.strip() for p in pages if p.strip())))
    return re.sub(r"[ \t]+", " ", text)


def _pdf_tables(content: bytes, link_text: str, name: str) -> tuple[list[dict], list[str]]:
    """Tables of the document from its reader, and problems (tables that fail the check)."""
    if link_text.lower().startswith("orario"):
        t = timetable.read_timetable(content)
        entry = {"pdf_page": 1, "context": t.title, "markdown": timetable.markdown(t)}
        return [entry | {"sentences": timetable.sentences(t)}], [
            f"timetable: {p}" for p in t.problems
        ]
    module = reader(link_text)
    if module is None:
        return [], []
    tables, problems = [], []
    for t in module.read(content):
        if not t.valid:
            problems.append(f"table on page {t.pdf_page}: {'; '.join(t.problems)}")
            continue
        tables.append({
            "pdf_page": t.pdf_page,
            "context": t.context,
            "markdown": table_markdown(t),
            "sentences": module.sentences([t], name),
            "headers": t.headers,
            "rows": t.rows,
        })  # fmt: skip
    return tables, problems


def extract_pdf(content: bytes, document: dict) -> Extract:
    link_text = document["link_text"]
    name = _document_name(link_text)
    with_reader = link_text.lower().startswith("orario") or reader(link_text) is not None
    end = regulation.end_of_text(content) if reader(link_text) is regulation else None
    text = _pdf_text(content, with_reader, end)
    if name.startswith("http"):  # link without text: title from the first line of the PDF
        name = next((r.strip() for r in text.splitlines() if r.strip()), name)[:120]
    tables, problems = _pdf_tables(content, link_text, name)
    year = document.get("year")
    e = Extract(
        url=document["url"],
        kind="pdf",
        title=name,
        section="PDF linkato da " + document["page"],
        text=text,
        year=academic_year(year) if isinstance(year, int) else "",
        tables=tables,
        problems=problems,
        linked_from=document["page"],
    )
    if e.empty and not tables:
        e.problems.append("PDF without text (a scan?)")
    return e


def without_duplicates(extracts: list[Extract]) -> list[Extract]:
    """One record per content (text and tables): identical addresses end up in `also`."""
    seen: dict[str, Extract] = {}
    for e in extracts:
        if e.text and e.hash in seen:
            seen[e.hash].also.append(e.url)
        else:
            seen[e.hash if e.text else e.url] = e
    return list(seen.values())


def extract_all(summary: dict) -> list[Extract]:
    extracts = []
    pages = [(u, "page") for u in summary["pages"]] + [(u, "external") for u in summary["external"]]
    rules = sources()
    for url, kind in pages:
        html = cache_file(url).read_bytes()
        section, with_list = rules.kept_section(url), rules.follows_linked(url)
        extracts.append(extract_page(html, url, kind, section, with_list))
    for notice in summary["notices"]:
        e = extract_page(cache_file(notice["url"]).read_bytes(), notice["url"], "notice")
        e.date = notice["date"]
        extracts.append(e)
    for course in summary["courses"]:
        data = json.loads(cache_file(course["url"]).read_bytes())
        before = course.get("previous")
        previous = json.loads(cache_file(before).read_bytes()) if before else None
        extracts.append(extract_sheet(data, course["url"], previous))
    for document in summary["documents"]:
        extracts.append(extract_pdf(cache_file(document["url"]).read_bytes(), document))
    empty = [
        e
        for e in extracts
        if e.empty and not e.problems and e.kind in ("page", "external", "notice")
    ]
    for e in empty:
        log.info("empty page on the site, skipped: %s", e.url)
    if len(empty) > MAX_EMPTY * len(extracts):
        for e in empty:
            e.problems.append("empty (too many empty pages: changed site structure?)")
    else:
        extracts = [e for e in extracts if e not in empty]
    for e in extracts:
        e.title = normal_spaces(normalise_years(e.title))
        e.text = normal_spaces(without_bibliography(e.text))
        for t in e.tables:
            t["markdown"] = normal_spaces(t["markdown"])
            t["sentences"] = [normal_spaces(s) for s in t["sentences"]]
    return without_duplicates(extracts)


def _write(extracts: list[Extract]) -> None:
    OUTPUT.mkdir(exist_ok=True)
    with (OUTPUT / "extracts.jsonl").open("w", encoding="utf-8") as f:
        for e in extracts:
            f.write(json.dumps(asdict(e) | {"hash": e.hash}, ensure_ascii=False) + "\n")
    blocks = []
    for e in extracts:
        header = [f"# {e.title or '(no title)'}", f"`{e.kind}` · {e.url}"]
        if e.section:
            header.append(f"Section: {e.section}")
        if e.date:
            header.append(f"Date: {e.date}")
        if e.year:
            header.append(f"Year: {e.year}")
        if e.also:
            header.append("Same content also at: " + ", ".join(e.also))
        if e.problems:
            header.append("**PROBLEMS: " + "; ".join(e.problems) + "**")
        tables = [
            f"### Table (page {t['pdf_page']})\n\n{t['markdown']}\n\nSentences:\n"
            + "\n".join(f"- {s}" for s in t["sentences"])
            for t in e.tables
        ]
        blocks.append("\n".join(header) + "\n\n" + e.text + "".join("\n\n" + t for t in tables))
    (OUTPUT / "extracts.md").write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = json.loads((CACHE / "summary.json").read_text(encoding="utf-8"))
    extracts = extract_all(summary)
    _write(extracts)
    for kind in ("page", "external", "notice", "course", "pdf"):
        group = [e for e in extracts if e.kind == kind]
        log.info(
            "%-12s %3d · characters %6d · with problems %d",
            kind, len(group), sum(len(e.text) for e in group), sum(bool(e.problems) for e in group),
        )  # fmt: skip
    for e in extracts:
        if e.problems:
            log.warning("problem: %s · %s", e.url, "; ".join(e.problems))
    if any(e.problems for e in extracts):
        raise SystemExit(1)  # the run fails: GitHub sends an email


if __name__ == "__main__":
    main()
