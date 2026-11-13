"""The crawler's sources (`sources.yaml`) and the rule on what may be downloaded."""

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from bs4 import Tag
from protego import Protego

from core.config import TIMEZONE
from core.years import current_year

SOURCES_FILE = Path(__file__).with_name("sources.yaml")


def normalise(href: str, base: str) -> str:
    """Absolute URL, without fragment (`#...`)."""
    parts = urlsplit(urljoin(base, href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


@dataclass(frozen=True)
class Sources:
    data: dict[str, Any]  # `sources.yaml`
    local_robots: Protego
    site_robots: Protego | None = None  # robots.txt of web.unica.it, if it ever appears
    external: frozenset[str] = frozenset()  # external pages allowed (and their redirects)
    external_robots: dict[str, Protego] = field(default_factory=dict)  # robots.txt per host
    # Name used to read the sites' robots.txt: rules written for our crawler count too
    # (`User-agent: UniRAG-inf`), not only those for everyone. Set by Downloader.
    agent: str = "*"

    @property
    def external_hosts(self) -> set[str]:
        return {urlsplit(u).hostname or "" for u in self.external}

    def with_external(self, url: str) -> Sources:
        """Adds an external address: only the redirect of an external page already allowed."""
        return replace(self, external=self.external | {url})

    def with_agent(self, agent: str) -> Sources:
        return replace(self, agent=agent)

    def with_external_robots(self, host: str, text: str) -> Sources:
        return replace(self, external_robots=self.external_robots | {host: Protego.parse(text)})

    @property
    def catalogue_host(self) -> str:
        return self["catalogue"]["host"]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def with_site_robots(self, text: str | None) -> Sources:
        """The same sources with the site's robots.txt too (the strictest wins)."""
        return replace(self, site_robots=Protego.parse(text) if text else None)

    def exclusion(self, url: str) -> str | None:
        """None if the URL may be downloaded, otherwise why it is excluded."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if url in self.external:
            if parts.scheme != "https":
                return "not https"
            robots = self.external_robots.get(host)
            if robots is not None and not robots.can_fetch(url, self.agent):
                return f"robots.txt of {host}"
            return None
        if host == self.catalogue_host:
            robots = self.external_robots.get(host)
            if robots is not None and not robots.can_fetch(url, self.agent):
                return f"robots.txt of {host}"
            return self._catalogue_exclusion(parts.scheme, parts.path)
        if host != self["host"]:
            # Our host first: "unica.it" among the excluded sites must not hit web.unica.it
            if any(host == s or host.endswith("." + s) for s in self["excluded_sites"]):
                return "excluded site"
            return "outside the course website"
        if parts.scheme != "https":
            return "not https"
        if parts.query and not any(re.match(q, url) for q in self["allowed_queries"]):
            return "URL with parameters"
        if any(word in parts.path.lower() for word in self["forbidden_words"]):
            return "forbidden word in the path"
        if not self.local_robots.can_fetch(url, "*"):
            return "local robots.txt"
        if self.site_robots is not None and not self.site_robots.can_fetch(url, self.agent):
            return "site robots.txt"
        return None

    def _catalogue_exclusion(self, scheme: str, path: str) -> str | None:
        """On the Cineca catalogue only the three addresses of the JSON service (allowed params)."""
        if scheme != "https":
            return "not https"
        if not any(path.startswith(p) for p in self["catalogue"]["allowed_paths"]):
            return "catalogue: address not allowed"
        return None

    def allowed(self, url: str) -> bool:
        return self.exclusion(url) is None

    def kept_section(self, url: str) -> str | None:
        """The title of the only block to keep of this page (`kept_sections`), if any."""
        return (self.data.get("kept_sections") or {}).get(url)

    def follows_linked(self, url: str) -> bool:
        """True if this page's list of linked contents is kept and followed."""
        return url in (self.data.get("linked_contents") or [])

    def is_document(self, url: str) -> bool:
        """True if the URL is a document to download (PDF or documents path)."""
        path = urlsplit(url).path.lower()
        documents = self["documents"]
        return any(path.endswith(e) for e in documents["extensions"]) or any(
            p in path for p in documents["paths"]
        )


def keep_only_section(container: Tag, title: str, with_list: bool = False) -> bool:
    """Leaves in the `container` only the block opened by the title (`h1`–`h6`): the block is the
    title's `div.article-txt-item`, or its parent. With `with_list` it also keeps the list of linked
    contents (`div.article-list`). False, touching nothing, if the title is not there."""
    for heading in container.select("h1, h2, h3, h4, h5, h6"):
        if " ".join(heading.get_text().split()) == title:
            block = heading.find_parent(class_="article-txt-item") or heading.parent
            kept = [block.extract()]
            if with_list:
                kept += [e.extract() for e in container.select("div.article-list")]
            container.clear()
            for k in kept:
                container.append(k)
            return True
    return False


def automatic_min_year(today: date) -> int:
    """First year of the documents to keep: the two running cohorts (October 2026 → 2025, i.e.
    2025/26 and 2026/27; from September 2027 → 2026)."""
    return max_year(today) - 1


def max_year(today: date) -> int:
    """Last year of the documents to keep: the current one. Next year's regulation published before
    September is downloaded from September: before that it is not a running cohort, the bot does
    not search it and it would push out the one of the second-year students."""
    return int(current_year(today)[:4])


@cache
def sources(path: Path = SOURCES_FILE, today: date | None = None) -> Sources:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    today = today or datetime.now(TIMEZONE).date()
    if data.get("min_year") in (None, "auto"):
        data["min_year"] = automatic_min_year(today)
    data["max_year"] = max_year(today)
    external_pages = data.get("external_pages") or []
    external_documents = [d["url"] for d in data.get("external_documents") or []]
    return Sources(
        data=data,
        local_robots=Protego.parse(data["local_robots"]),
        external=frozenset([*external_pages, *external_documents]),
    )
