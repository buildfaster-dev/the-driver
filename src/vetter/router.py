"""Provider routing layer: client instantiation, retries, and per-call logging.

reviewer.py delegates all model I/O here and never touches provider SDKs.
Errors from provider SDKs are translated into two router-level categories:

- TransientProviderError: retried with exponential backoff (rate limits,
  server errors, timeouts, connection failures).
- FatalProviderError: fails fast, no retry (bad credentials, bad request,
  unknown model) — retrying cannot fix these.

Every call attempt (success or failure) is appended as one JSON line to the
call log. The log is append-only and accumulates across runs (no rotation;
each record carries a timestamp). Default location: ~/.vetter/calls.jsonl,
overridable with the VETTER_LOG_DIR environment variable (a directory; the
file inside is always named calls.jsonl).
"""

import json
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import anthropic
import click


class TransientProviderError(Exception):
    """Retryable provider failure: rate limit, server error, timeout, connection."""


class FatalProviderError(Exception):
    """Non-retryable provider failure: credentials, permissions, bad request."""


# Model alias → per-provider model id. A raw model id (not an alias) is passed
# through to the provider unchanged.
MODEL_MAP = {
    "sonnet": {"anthropic": "claude-sonnet-4-6"},
    "opus": {"anthropic": "claude-opus-4-6"},
    "haiku": {"anthropic": "claude-haiku-4-5-20251001"},
}

# USD per million tokens (input, output). Checked against provider pricing
# docs on 2026-07-12. Unknown models log cost_usd as null rather than a guess.
PRICING = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

MAX_ATTEMPTS = 3
BASE_DELAY_S = 2.0


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int


@dataclass
class CallRecord:
    """One log line per call attempt — the receipt (who, how much, how long).

    Failed attempts carry null tokens/cost: the SDK returns no usage on errors,
    and an honest null beats an estimate.
    """

    timestamp: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    outcome: str  # "success" | "error:transient" | "error:fatal"
    error: str | None = None


class Provider(Protocol):
    name: str

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise FatalProviderError("ANTHROPIC_API_KEY environment variable is not set.")
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
    ) -> ProviderResponse:
        try:
            message = self._client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
        except anthropic.AuthenticationError:
            raise FatalProviderError("anthropic: invalid ANTHROPIC_API_KEY. Please check your API key.")
        except anthropic.PermissionDeniedError as e:
            raise FatalProviderError(f"anthropic: permission denied: {e}")
        except anthropic.NotFoundError as e:
            raise FatalProviderError(f"anthropic: model or endpoint not found: {e}")
        except anthropic.BadRequestError as e:
            raise FatalProviderError(f"anthropic: bad request: {e}")
        except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
            raise TransientProviderError(f"anthropic: {e}")
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientProviderError(f"anthropic: {e}")
            raise FatalProviderError(f"anthropic: {e}")
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            raise TransientProviderError(f"anthropic: {e}")
        return ProviderResponse(
            text=message.content[0].text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


def _log_path() -> Path:
    log_dir = os.environ.get("VETTER_LOG_DIR")
    base = Path(log_dir) if log_dir else Path.home() / ".vetter"
    return base / "calls.jsonl"


def _append_record(record: CallRecord) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record)) + "\n")


def _cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    rates = PRICING.get(model_id)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6)


def _record(
    provider: Provider,
    model_id: str,
    started: float,
    outcome: str,
    response: ProviderResponse | None = None,
    error: Exception | None = None,
) -> None:
    _append_record(
        CallRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider.name,
            model=model_id,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            cost_usd=_cost_usd(model_id, response.input_tokens, response.output_tokens)
            if response
            else None,
            latency_ms=int((time.monotonic() - started) * 1000),
            outcome=outcome,
            error=str(error) if error else None,
        )
    )


def _call_with_retry(
    provider: Provider, system: str, user_content: str, model_id: str, max_tokens: int, temperature: float
) -> ProviderResponse:
    for attempt in range(MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            response = provider.complete(system, user_content, model_id, max_tokens, temperature)
        except TransientProviderError as e:
            _record(provider, model_id, started, "error:transient", error=e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BASE_DELAY_S * (2**attempt) + random.uniform(0, 1))
                continue
            raise
        except FatalProviderError as e:
            _record(provider, model_id, started, "error:fatal", error=e)
            raise
        _record(provider, model_id, started, "success", response=response)
        return response
    raise AssertionError("unreachable")


def complete(
    system: str,
    user_content: str,
    model: str = "sonnet",
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> str:
    """Route a completion request to a provider and return the response text.

    Callers stay ignorant of which provider SDK answered; failures surface as
    click.ClickException with the provider named in the message.
    """
    try:
        provider = AnthropicProvider()
        model_id = MODEL_MAP.get(model, {}).get(provider.name, model)
        response = _call_with_retry(provider, system, user_content, model_id, max_tokens, temperature)
    except FatalProviderError as e:
        raise click.ClickException(str(e))
    except TransientProviderError as e:
        raise click.ClickException(
            f"anthropic unavailable after {MAX_ATTEMPTS} attempts: {e}"
        )
    return response.text
