"""One place for talking to the LLM provider: clients, structured-JSON calls and readable errors."""
import json
import logging
import re
from functools import lru_cache

import openai
from openai import OpenAI

from .config import settings

log = logging.getLogger("llm")


class ProviderError(RuntimeError):
    """An AI call failed; the message is safe to show to the user."""


@lru_cache(maxsize=1)
def client() -> OpenAI:
    """Normal client: the SDK retries transient errors."""
    if not settings.api_key:
        raise ProviderError("No API key set. Copy .env.example to .env and add OPENROUTER_API_KEY or OPENAI_API_KEY.")
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


@lru_cache(maxsize=1)
def fast_client() -> OpenAI:
    """Audio path: no retries, short timeout, so we move straight to a fallback model instead of waiting."""
    return client().with_options(max_retries=0, timeout=25)


def friendly(e: Exception) -> str:
    """Turn provider errors into something the user can act on."""
    if isinstance(e, ProviderError):
        return str(e)
    status = getattr(e, "status_code", None)
    if status == 401 or isinstance(e, openai.AuthenticationError):
        return "The AI provider rejected your API key (expired, revoked or wrong). Put a valid key in .env and restart."
    if status == 402:
        return "Your AI provider account is out of credit, or the key reached its spending limit."
    if status == 403:
        return "The API key is not allowed to use this model. Check the model names in .env."
    if status == 429:
        return "Rate limited by the AI provider. Wait a few seconds and try again."
    if isinstance(e, openai.APIConnectionError):
        return "Could not reach the AI provider. Check your internet connection."
    return f"AI request failed: {e}"


def _reasoning_kwargs(model: str, effort: str) -> dict:
    """Reasoning models (gpt-5*) are much faster with low effort; other models ignore this."""
    if not effort or not re.search(r"gpt-5|/o\d|^o\d", model):
        return {}
    if settings.openrouter:
        return {"extra_body": {"reasoning": {"effort": effort}}}
    return {"reasoning_effort": effort}


def json_call(*, model: str, system: str, user: str, schema: dict, name: str,
              reasoning: str = "", max_tokens: int | None = None, fast: bool = False) -> dict:
    """Chat call that must return JSON matching `schema` (strict structured output).

    Falls back to pulling the first {...} block out of the text if a provider ignores the schema.
    """
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        **_reasoning_kwargs(model, reasoning),
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    r = (fast_client() if fast else client()).chat.completions.create(**kwargs)
    return parse_json(r.choices[0].message.content or "")


def parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise ProviderError("The AI returned an answer that was not JSON.")
        return json.loads(m.group(0))
