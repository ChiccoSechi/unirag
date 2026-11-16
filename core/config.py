"""Configuration: project constants and environment variables.

Locally the variables come from `.env`; in production from GitHub Secrets and Variables.
Variables already in the environment take precedence over `.env`.
"""

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# "Never expires" in `valid_until` (31/12/9999 23:59:59 UTC): JSON has no infinity and an empty
# field would leave the vector out of the filters.
NEVER = 253402300799

# Time zone used to turn dates into Unix timestamps.
TIMEZONE = ZoneInfo("Europe/Rome")

EMBEDDING_MODEL = "@cf/baai/bge-m3"
LLM_MODEL = "@cf/google/gemma-4-26b-a4b-it"
VECTORIZE_INDEX = "unirag-inf"


class MissingConfiguration(RuntimeError):
    """A required variable is missing. The message holds only its name, never values."""

    def __init__(self, name: str) -> None:
        super().__init__(f"missing variable: {name} (see .env.example)")


@dataclass(frozen=True)
class Cloudflare:
    """Access to Cloudflare's HTTP API from the pipeline and the local RAG."""

    account_id: str = field(repr=False)
    api_token: str = field(repr=False)
    embedding_model: str
    llm_model: str
    vectorize_index: str = VECTORIZE_INDEX
    d1_database: str = ""  # D1 database ID; empty until it is created


def env_var(name: str, default: str | None = None) -> str:
    """Value of a variable; empty or missing counts as not set."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise MissingConfiguration(name)


def details() -> bool:
    """`PIPELINE_DETAILS=1`: errors in the log also show the values that explain them (e.g. the
    words a table lost), with long numbers masked. Off by default: a public repository's logs are
    public, and these values are texts from UniCa's website."""
    return os.environ.get("PIPELINE_DETAILS", "").strip() == "1"


def load_env() -> None:
    """Loads `.env` from the repository root, without overriding the environment."""
    load_dotenv(ROOT / ".env", override=False)


@cache
def cloudflare() -> Cloudflare:
    """Cloudflare configuration, read once."""
    load_env()
    return Cloudflare(
        account_id=env_var("CLOUDFLARE_ACCOUNT_ID"),
        api_token=env_var("CLOUDFLARE_API_TOKEN"),
        embedding_model=env_var("CF_EMBED_MODEL", EMBEDDING_MODEL),
        llm_model=env_var("CF_LLM_MODEL", LLM_MODEL),
        vectorize_index=env_var("VECTORIZE_INDEX", VECTORIZE_INDEX),
        d1_database=env_var("D1_DATABASE_ID", ""),
    )


# --- Crawler ---

DELAY_SECONDS = 1.0  # exactly 1 s between two requests


@dataclass(frozen=True)
class Crawler:
    """Identity of the crawler. The contact is always that of whoever runs it."""

    name: str
    contact: str
    info_url: str
    full: bool  # False: reduced mode with the local copy (development)


@cache
def crawler() -> Crawler:
    """Crawler configuration, read once. Without a valid contact it does not start."""
    load_env()
    contact = env_var("CRAWLER_CONTACT")
    if "@" not in contact or any(c.isspace() for c in contact):
        raise MissingConfiguration("CRAWLER_CONTACT (an email address is needed)")
    return Crawler(
        name=env_var("CRAWLER_NAME", "UniRAG-inf-copy"),
        contact=contact,
        info_url=os.environ.get("CRAWLER_INFO_URL", "").strip(),
        full=env_var("CRAWLER_FULL", "0") == "1",
    )


def crawler_headers() -> dict[str, str]:
    """User-Agent and `From`: the only place the crawler takes its identity from.

    The contact is also in the User-Agent because Apache's standard logs do not record `From`.
    """
    c = crawler()
    details = "; ".join(x for x in (f"+{c.info_url}" if c.info_url else "", c.contact) if x)
    return {"User-Agent": f"{c.name} ({details})", "From": c.contact}
