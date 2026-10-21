"""OpenRouter Decisions API client that measures TTFB, latency, tokens and cost.

The Decisions API returns a single JSON document (no token streaming), so there is no
time-to-first-token. We measure time-to-first-byte (response headers received) and total latency.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict, dataclass

import httpx

from .config import ModelSpec, Settings

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 524, 529}


class OpenRouterError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class CallMetrics:
    ttfb_ms: float | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str | None = None  # "openrouter" | "env_pricing" | None
    provider: str | None = None
    model_version: str | None = None  # resolved snapshot, e.g. typesafe/jev-1.14-20261014
    generation_id: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CallResult:
    data: dict | None
    metrics: CallMetrics
    error: str | None = None


def _error_message(status: int, body: str) -> str:
    try:
        err = json.loads(body).get("error") or {}
        return f"HTTP {status}: {err.get('message') or body[:300]}"
    except (ValueError, AttributeError):
        return f"HTTP {status}: {body[:300]}"


async def _decide_once(client: httpx.AsyncClient, settings: Settings, model: ModelSpec, payload: dict) -> CallResult:
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "HTTP-Referer": settings.referer,
        "X-Title": settings.title,
        "Content-Type": "application/json",
    }
    metrics = CallMetrics()
    start = time.perf_counter()
    body = {"model": model.slug, **payload}
    if model.provider:
        body["provider"] = model.provider
    async with client.stream("POST", settings.decisions_url, json=body, headers=headers) as resp:
        metrics.ttfb_ms = round((time.perf_counter() - start) * 1000, 1)
        body = (await resp.aread()).decode(errors="replace")
    metrics.latency_ms = round((time.perf_counter() - start) * 1000, 1)

    if resp.status_code != 200:
        raise OpenRouterError(_error_message(resp.status_code, body), resp.status_code, resp.status_code in RETRYABLE_STATUS)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenRouterError(f"Invalid JSON from API: {exc}", retryable=True) from exc
    if "error" in data and "answers" not in data:
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise OpenRouterError(f"API error: {msg}", code, code in RETRYABLE_STATUS)

    usage = data.get("usage") or {}
    metrics.input_tokens = usage.get("input_tokens")
    metrics.output_tokens = usage.get("output_tokens")
    if metrics.input_tokens is not None or metrics.output_tokens is not None:
        metrics.total_tokens = (metrics.input_tokens or 0) + (metrics.output_tokens or 0)
    if usage.get("cost") is not None:
        metrics.cost_usd = float(usage["cost"])
        metrics.cost_source = "openrouter"
    elif model.price_in is not None and model.price_out is not None and metrics.total_tokens is not None:
        metrics.cost_usd = ((metrics.input_tokens or 0) * model.price_in + (metrics.output_tokens or 0) * model.price_out) / 1e6
        metrics.cost_source = "env_pricing"
    metrics.provider = data.get("provider")
    metrics.model_version = data.get("model")
    metrics.generation_id = data.get("id")
    return CallResult(data=data, metrics=metrics)


async def decide(client: httpx.AsyncClient, settings: Settings, model: ModelSpec, payload: dict) -> CallResult:
    """Call a decision model with retries (exponential backoff + jitter). Never raises; errors land in CallResult.error."""
    if not settings.api_key:
        return CallResult(data=None, metrics=CallMetrics(), error="OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")

    last_error = "unknown error"
    attempts = settings.max_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            result = await _decide_once(client, settings, model, payload)
            result.metrics.attempts = attempt
            return result
        except OpenRouterError as exc:
            last_error = str(exc)
            if not exc.retryable or attempt == attempts:
                break
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt == attempts:
                break
        await asyncio.sleep(min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5))

    return CallResult(data=None, metrics=CallMetrics(attempts=attempt), error=last_error)


def make_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_s, connect=10.0))
