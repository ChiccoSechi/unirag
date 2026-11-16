"""Answers from the Workers AI language model (default Gemma 4 26B), with reasoning turned off.

Gemma 4 answers in OpenAI's format (`choices[0].message.content`). Reasoning is turned off with
`chat_template_kwargs.enable_thinking = false`: otherwise Gemma 4 spends the tokens reasoning and
leaves the answer empty.
"""

from dataclasses import dataclass

import httpx

from core.cloudflare import run_model
from core.config import cloudflare

Messages = list[dict[str, str]]


@dataclass(frozen=True)
class Answer:
    text: str
    input_tokens: int
    output_tokens: int
    neurons: float
    truncated: bool  # True if the answer stopped at the token limit


def answer(
    messages: Messages,
    *,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    client: httpx.Client | None = None,
) -> Answer:
    """The model's answer (default: `CF_LLM_MODEL`) to `messages`."""
    data = {
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    result = run_model(model or cloudflare().llm_model, data, client)
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = result.get("usage") or {}
    return Answer(
        text=(message.get("content") or "").strip(),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        neurons=float(usage.get("neurons") or 0.0),
        truncated=choice.get("finish_reason") == "length",
    )
