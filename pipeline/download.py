"""Polite downloads: one request at a time, 1 s apart, stop rules, local copy in `cache/`.

Headers only from `core.config.crawler_headers()`. Redirects are followed by hand, checking every
target (a redirect to the login is skipped). Stop rules:
- 429 or 503: stop at once, without retrying;
- too many errors in a row (timeouts, network errors, 5xx): stop, the site has problems;
- maximum number of requests per run reached: stop.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from core.config import DELAY_SECONDS
from pipeline.sources import Sources, normalise

log = logging.getLogger("pipeline.download")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 30.0
# Crawler files in the local copy: hash of the URL + .bin (content), .json (metadata), .assente
CACHE_FILE = re.compile(r"[0-9a-f]{32}\.(bin|json|assente)")
MAX_CLEANUP = 0.3  # above this share of files to delete the cleanup is not done


class StopCrawl(Exception):
    """The crawler must stop: the message says why."""


@dataclass(frozen=True)
class Response:
    url: str  # final URL, after any redirect
    content_type: str
    content: bytes
    from_cache: bool

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()

    @property
    def is_pdf(self) -> bool:
        return "pdf" in self.content_type.lower() or self.content[:5] == b"%PDF-"


class Downloader:
    def __init__(
        self,
        sources: Sources,
        cache_folder: Path,
        *,
        headers: dict[str, str],
        delay: float = DELAY_SECONDS,
        use_cache: bool,
        max_requests: int,
        errors_in_a_row: int,
        cache_only: bool = False,
    ) -> None:
        if "From" not in headers or "User-Agent" not in headers:
            raise ValueError("User-Agent and From from core.config.crawler_headers() are needed")
        self.sources = sources
        self.cache = cache_folder
        self.headers = headers
        self.delay = delay
        self.use_cache = use_cache or cache_only
        self.cache_only = cache_only  # True: no request to the site, only the local copy
        self.max_requests = max_requests
        self.max_errors = errors_in_a_row
        self.client = httpx.Client(timeout=TIMEOUT_SECONDS)
        self.requests = 0
        self.from_cache = 0
        self._errors = 0
        self._last: float | None = None
        self._this_run: dict[str, Response] = {}  # already downloaded in this run: never twice
        self.used: set[str] = set()  # asked in this run (downloaded or from the local copy)
        # Answered 404/410 or redirected to an excluded address (e.g. the portal's login): the
        # content is gone, it is not a network problem
        self.gone: set[str] = set()

    # --- the sites' robots.txt ---

    def read_site_robots(self) -> None:
        """Reads web.unica.it's robots.txt again: if one day it exists, it applies together with
        ours. It is read with the crawler's name (first word of the User-Agent), so a rule written
        for us applies too."""
        if self.cache_only:
            return
        self.sources = self.sources.with_agent(self.headers["User-Agent"].split()[0])
        # The course catalogue too: today it has none, but a rule added one day applies at once
        for host in sorted(self.sources.external_hosts | {self.sources.catalogue_host}):
            r = self._request(f"https://{host}/robots.txt")
            kind = r.headers.get("content-type", "") if r is not None else ""
            if r is not None and r.status_code == 200 and "text/plain" in kind:
                log.info("robots.txt of %s read: it applies to that site", host)
                self.sources = self.sources.with_external_robots(host, r.text)
        r = self._request(f"https://{self.sources['host']}/robots.txt")
        kind = r.headers.get("content-type", "") if r is not None else ""
        if r is not None and r.status_code == 200 and "text/plain" in kind:
            log.info("site robots.txt found: it applies together with the local one")
            self.sources = self.sources.with_site_robots(r.text)
        else:
            status = r.status_code if r is not None else "network error"
            log.info("no site robots.txt (%s): the local one applies", status)

    # --- downloads ---

    def get(self, url: str) -> Response | None:
        """The URL's content, or None if excluded or not available."""
        if reason := self.sources.exclusion(url):
            log.info("excluded (%s): %s", reason, url)
            return None
        self.used.add(url)
        if url in self._this_run:
            return self._this_run[url]
        if self.use_cache and (copy := self._read_cache(url)) is not None:
            self.from_cache += 1
            return copy
        if self.use_cache and self._absent(url):
            return None  # already answered 404/410: not retried at every run
        if self.cache_only:
            log.info("not in the local copy: %s", url)
            return None
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            r = self._request(current)
            if r is None:
                return None
            if r.is_redirect:
                target = normalise(r.headers.get("location", ""), current)
                same_host = urlsplit(target).hostname == urlsplit(current).hostname
                if current in self.sources.external and same_host:
                    self.sources = self.sources.with_external(target)  # only the redirect
                if reason := self.sources.exclusion(target):
                    log.info("redirect excluded (%s): %s → %s", reason, current, target)
                    self.gone.add(url)
                    return None
                current = target
                continue
            if r.status_code != 200:
                log.info("HTTP %s: %s", r.status_code, current)
                if r.status_code in (404, 410):
                    self._mark_absent(url, r.status_code)
                    self.gone.add(url)
                return None
            response = Response(current, r.headers.get("content-type", ""), r.content, False)
            self._write_cache(url, response)
            self._this_run[url] = response
            return response
        log.info("too many redirects: %s", url)
        return None

    def _request(self, url: str) -> httpx.Response | None:
        """A GET with delay and stop rules. None if it fails without having to stop everything."""
        try:
            httpx.URL(url)
        except httpx.InvalidURL:  # a broken link on the site (e.g. control characters): skipped
            log.info("invalid address skipped: %r", url)
            return None
        if self.requests >= self.max_requests:
            raise StopCrawl(f"limit of {self.max_requests} requests reached")
        self._wait()
        self.requests += 1
        try:
            r = self.client.get(url, headers=self.headers, follow_redirects=False)
        except httpx.HTTPError as e:
            self._error(f"{type(e).__name__} on {url}")
            return None
        finally:
            self._last = time.monotonic()
        if r.status_code in (429, 503):
            raise StopCrawl(f"HTTP {r.status_code} on {url}: the site asks to stop")
        if r.status_code >= 500:
            self._error(f"HTTP {r.status_code} on {url}")
            return r
        self._errors = 0
        return r

    def _wait(self) -> None:
        if self._last is None:
            return
        remaining = self.delay - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)

    def _error(self, description: str) -> None:
        self._errors += 1
        log.warning("error %d in a row: %s", self._errors, description)
        if self._errors >= self.max_errors:
            raise StopCrawl(f"{self._errors} errors in a row: the site seems to have problems")

    # --- cleanup of the local copy ---

    def unused_files(self) -> list[Path]:
        """Files of the local copy of addresses not asked in this run (e.g. a regulation no longer
        among the latest years). Only the crawler's files: README and summary stay."""
        used = {self._file(u).name for u in self.used}
        return sorted(
            f for f in self.cache.glob("*") if CACHE_FILE.fullmatch(f.name) and f.stem not in used
        )

    def clean_cache(self) -> list[Path]:
        """Deletes the unused files, but only if they are few: many files to delete more likely mean
        a changed site (menu, links) than pages really gone.

        Only for the development copy (`cache/` on the PC). On GitHub
        Actions the local copy does not survive the night; in the bot, contents that disappeared
        are removed from Vectorize and D1 (after 2 weekly runs in a row), not from here."""
        old = self.unused_files()
        every = [f for f in self.cache.glob("*") if CACHE_FILE.fullmatch(f.name)]
        if len(old) > MAX_CLEANUP * len(every):
            raise StopCrawl(f"cleanup cancelled: {len(old)} files of {len(every)} to delete")
        for f in old:
            f.unlink()
        return old

    def _file(self, url: str) -> Path:
        return self.cache / hashlib.sha256(url.encode()).hexdigest()[:32]

    def _absent(self, url: str) -> bool:
        return self._file(url).with_suffix(".assente").exists()

    def _mark_absent(self, url: str, status: int) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        self._file(url).with_suffix(".assente").write_text(f"{status} {url}\n", encoding="utf-8")

    def replace_in_cache(self, url: str, content: bytes) -> None:
        """Rewrites the local copy of a URL (e.g. a sheet cleaned of personal data)."""
        file = self._file(url).with_suffix(".bin")
        if file.exists() and file.read_bytes() != content:
            file.write_bytes(content)

    def _read_cache(self, url: str) -> Response | None:
        base = self._file(url)
        meta, data = base.with_suffix(".json"), base.with_suffix(".bin")
        if not (meta.exists() and data.exists()):
            return None
        m = json.loads(meta.read_text(encoding="utf-8"))  # Italian keys: files already saved
        return Response(m["url"], m["content_type"], data.read_bytes(), True)

    def _write_cache(self, url: str, response: Response) -> None:
        self.cache.mkdir(parents=True, exist_ok=True)
        base = self._file(url)
        base.with_suffix(".bin").write_bytes(response.content)
        meta = {
            "url_richiesto": url,
            "url": response.url,
            "content_type": response.content_type,
            "byte": len(response.content),
            "scaricato_il": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        base.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
