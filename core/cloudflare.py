"""Calls to Cloudflare's HTTP API (Workers AI, D1, Vectorize) from the pipeline and the local RAG.

The Worker does not use this module: there the services are reached through bindings. Errors hold
only the service, the HTTP status and Cloudflare's codes: never texts, SQL, parameters or tokens.
"""

import logging

import httpx

from core.config import cloudflare

API = "https://api.cloudflare.com/client/v4"

# httpx and httpcore log the request URLs: keep them quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class CloudflareError(RuntimeError):
    """Error of a Cloudflare service: only HTTP status and error codes."""

    def __init__(self, service: str, status: int, codes: list[int]) -> None:
        self.service = service
        self.status = status
        self.codes = codes
        super().__init__(f"{service}: HTTP {status}, codes {codes}")


def call(
    service: str,
    method: str,
    path: str,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
    **request,
):
    """The `result` field of the answer to `method` (GET or POST) on `path` of the account."""
    cf = cloudflare()
    url = f"{API}/accounts/{cf.account_id}{path}"
    headers = {"Authorization": f"Bearer {cf.api_token}"}
    if client is None:
        with httpx.Client(timeout=timeout) as new:
            response = new.request(method, url, headers=headers, **request)
    else:
        response = client.request(method, url, headers=headers, **request)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.is_success and body.get("success"):
        return body.get("result")
    codes = [e.get("code") for e in body.get("errors") or [] if isinstance(e, dict)]
    raise CloudflareError(service, response.status_code, codes)


# --- Workers AI ---

MODEL_TIMEOUT_SECONDS = 120.0


class WorkersAIError(CloudflareError):
    """Workers AI error: only the HTTP status and Cloudflare's codes, never the text sent or the
    token."""

    def __init__(self, status: int, codes: list[int]) -> None:
        super().__init__("Workers AI", status, codes)


def run_model(
    model: str,
    data: dict,
    client: httpx.Client | None = None,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> dict:
    """Runs a Workers AI `model` on `data` and returns the `result` field of the answer."""
    try:
        result = call("Workers AI", "POST", f"/ai/run/{model}", client, timeout, json=data)
    except CloudflareError as e:
        raise WorkersAIError(e.status, e.codes) from None
    if not isinstance(result, dict):
        raise WorkersAIError(200, [])
    return result
