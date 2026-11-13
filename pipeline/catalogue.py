"""Course sheets from the Cineca course catalogue (`unica.coursecatalogue.cineca.it`).

The catalogue is a JavaScript app that reads its data from a JSON service. The crawler follows the
app's own steps, starting from the official links of the course's "Insegnamenti" page:
1. link to the latest year → `/api/v1/corso/{year}/{course}?codicione=…` (for the site);
2. `/api/v1/corso-offerta/{course}?codicione=…&annoOrdinamento={year}&sede={site}`: the list of
   subjects for each offer year (the current one and the previous are read);
3. for each subject `/api/v1/insegnamento?…`, with the parameters given by the list.
No parameter is guessed.

Only the subjects **taught in the current year**: one sheet per
active subject, no duplicates between years, no subjects no longer active. Like the catalogue does,
if the current year's sheet is "being defined" (no texts: objectives, programme, exam…) the texts
of the previous year's sheet of the same subject are used, if any; the fixed data (CFU, period,
teachers) stay those of the current year.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup

from pipeline.download import Downloader
from pipeline.sources import Sources, normalise

YEAR_LINK = re.compile(r"^/corsi/(\d{4})/(\d+)$")

# Of the teachers only name and role are kept: no staff numbers, codes, links
TEACHER_FIELDS = {"des", "titolareFlg", "respDidFlg", "lezioneFlg"}
PEOPLE_KEYS = {"titolari", "responsabili", "docenti", "docenti_ricerca"}


def clean_sheet(data: object) -> object:
    """The sheet without the teachers' personal data beyond name and role (even nested)."""
    if isinstance(data, list):
        return [clean_sheet(x) for x in data]
    if not isinstance(data, dict):
        return data
    clean = {}
    for k, value in data.items():
        if k in PEOPLE_KEYS and isinstance(value, list):
            clean[k] = [
                {f: v for f, v in p.items() if f in TEACHER_FIELDS} if isinstance(p, dict) else p
                for p in value
            ]
        else:
            clean[k] = clean_sheet(value)
    return clean


# Text fields of the sheet (in `testiTotali`, with suffix _it or _en) and their heading
SHEET_FIELDS = [
    ("obiettivi_formativi", "Obiettivi formativi"),
    ("prerequisiti", "Prerequisiti"),
    ("contenuti", "Contenuti"),
    ("metodi_didattici_est", "Metodi didattici"),
    ("verifica_apprendimento", "Verifica dell'apprendimento"),
    ("testi", "Testi di riferimento"),
    ("altri_testi_1", "Altri testi"),
    ("altri_testi_2", "Altri testi"),
    ("altri_testi_3", "Altri testi"),
    ("altro", "Altre informazioni"),
]


def has_texts(data: object) -> bool:
    """False for a sheet "being defined": no text in any block."""
    blocks = data.get("testiTotali") if isinstance(data, dict) else None
    for block in blocks or []:
        for name, _ in SHEET_FIELDS:
            for language in ("_it", "_en"):
                v = str((block or {}).get(name + language) or "").strip()
                if v and v.lower() != "none":
                    return True
    return False


@dataclass
class Course:
    url: str  # address of the sheet in the JSON service
    code: str  # e.g. "21875"
    name: str  # e.g. "DATA MINING"
    offer_year: str
    credits: float | None
    previous: str = ""  # previous year's sheet (fallback texts); "" if not used


@dataclass(frozen=True)
class CatalogueYear:
    year: str
    course: str
    codicione: str


def current_year(html: bytes, url: str, s: Sources) -> CatalogueYear | None:
    """The link of the "Insegnamenti" page to the latest enrolment year."""
    years = []
    for a in BeautifulSoup(html, "html.parser").select("#main_container a[href]"):
        target = urlsplit(normalise(a["href"], url))
        found = YEAR_LINK.match(target.path)
        codicione = parse_qs(target.query).get("codicione", [""])[0]
        if target.hostname == s.catalogue_host and found and codicione:
            years.append(CatalogueYear(found.group(1), found.group(2), codicione))
    return max(years, key=lambda x: x.year, default=None)


def _api(s: Sources, path: str, params: dict[str, str]) -> str:
    return f"https://{s.catalogue_host}{path}?{urlencode(params)}"


def read_json(d: Downloader, url: str) -> object | None:
    """A JSON answer of the catalogue, without the teachers' personal data (in the local copy
    too). None if missing or unreadable (e.g. truncated): removed from the local copy as well,
    because it cannot be cleaned."""
    response = d.get(url)
    if response is None or not response.is_json:
        return None
    try:
        data = clean_sheet(json.loads(response.content))
    except ValueError:
        d.replace_in_cache(url, b"")
        return None
    d.replace_in_cache(url, json.dumps(data, ensure_ascii=False).encode())
    return data


def course_list(d: Downloader, s: Sources, y: CatalogueYear) -> list[Course]:
    """The subjects taught in the current year, one per code (with the latest regulation), each with
    the address of the previous year's sheet if it exists."""
    course = read_json(d, _api(s, f"/api/v1/corso/{y.year}/{y.course}", {"codicione": y.codicione}))
    if not isinstance(course, list) or not course or "sede_cod" not in course[0]:
        return []
    params = {"codicione": y.codicione, "annoOrdinamento": y.year, "sede": course[0]["sede_cod"]}
    offer = read_json(d, _api(s, f"/api/v1/corso-offerta/{y.course}", params))
    if not isinstance(offer, dict):
        return []
    return with_previous(s, offer, y.year)


def with_previous(s: Sources, offer: dict, year: str) -> list[Course]:
    """The subjects of the year with, for each, the sheet of the same code the year before. Those
    offered only the year before (no longer active) are left out."""
    current = _courses_of_year(s, offer.get(year) or {})
    before = {c.code: c.url for c in _courses_of_year(s, offer.get(str(int(year) - 1)) or {})}
    for c in current:
        c.previous = before.get(c.code, "")
    return current


def _courses_of_year(s: Sources, offer_year: dict) -> list[Course]:
    chosen: dict[str, dict] = {}
    for period in offer_year.values():
        for activity in period.get("attivita", []):
            code = str(activity.get("cod", ""))
            if not code or not activity.get("percorsi"):
                continue
            seen = chosen.get(code)
            if seen is None or activity["ordinamento_aa"] > seen["ordinamento_aa"]:
                chosen[code] = activity
    courses = []
    for code, activity in chosen.items():
        path = activity["percorsi"][0]
        # Some entries have schema and path in the activity instead of the path
        schema = path.get("schemaId", activity.get("schemaId"))
        af_path = path.get("pdsId", activity.get("af_percorso_id"))
        if schema is None or af_path is None:
            continue
        params = {
            "anno": activity["aa"],
            "insegnamento": code,
            "ordinamento_aa": activity["ordinamento_aa"],
            "af_percorso": af_path,
            "corso_cod": activity["corso_cod"],
            "schema_id": schema,
        }
        courses.append(
            Course(
                url=_api(s, "/api/v1/insegnamento", params),
                code=code,
                name=activity.get("des_it", ""),
                offer_year=str(activity["aa"]),
                credits=activity.get("crediti"),
            )
        )
    return sorted(courses, key=lambda c: c.name)
