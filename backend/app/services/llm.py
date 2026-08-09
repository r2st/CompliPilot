"""The OpenRouter client, and the structured-output contract built on it.

Every model call in the product goes through :func:`complete` or
:func:`complete_json`. Concentrating them here buys four things that are
tedious to get right once per call site:

**Structured output that is actually structured.** A compliance product cannot
act on prose. :func:`complete_json` asks for JSON, and then *validates* what
came back against the caller's expected keys — because a model that returns
plausible prose where JSON was requested is a normal Tuesday, and the failure
must surface as a parse error at the boundary rather than as a ``None`` field
three layers in.

**Failure that degrades rather than propagates.** The callers are document
parsing and regulatory analysis, both of which run in a worker against content
that has already been stored. A model outage must leave the document parsed as
``FAILED`` with a reason, not lose the upload. :class:`LLMUnavailable` is
raised for the cases a retry could fix; everything else is reported in the
result.

**A hard rule about advice.** The system prompt states that output is a draft
for a qualified professional to review, and every response carries a confidence
the caller records. Section 4.2 requires human review of AI-generated filings,
and the product is not permitted to imply otherwise.

**No key, no call.** With ``openrouter_api_key`` unset, :func:`is_configured`
is false and the callers record "not analysed" rather than crashing. A
development stack runs the whole pipeline without a key.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import UpstreamError

logger = logging.getLogger(__name__)

# The instruction every call carries. Written once, here, because a compliance
# product that lets one call site quietly drop "this is a draft for review" has
# a regulatory problem rather than a prompt-engineering one.
SYSTEM_PROMPT = (
    "You are CompliPilot, a compliance assistant for Indian regulatory filings "
    "(GST, Income Tax, RBI, SEBI, MCA, FEMA, labour law, and the DPDP Act 2023).\n"
    "\n"
    "Rules you must follow:\n"
    "1. Your output is a DRAFT for review by a qualified professional. Never "
    "state or imply that it is final, filed, or a substitute for professional "
    "judgement.\n"
    "2. Never invent figures, dates, section numbers, or form names. If the "
    "input does not contain something you need, say so explicitly in the "
    "designated field rather than guessing.\n"
    "3. All monetary amounts are in Indian rupees. Indian financial years run "
    "1 April to 31 March.\n"
    "4. When asked for JSON, return ONLY valid JSON — no prose, no markdown "
    "fences, no commentary before or after."
)

# Confidence below this is worth surfacing to the user as "check this
# carefully". The threshold is a product decision, kept next to the prompt that
# elicits the number so the two stay consistent.
LOW_CONFIDENCE = 60

# A ```json fenced block, which models emit despite being asked not to.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMUnavailable(UpstreamError):
    """The model could not be reached, or is not configured.

    A subclass of :class:`~app.core.errors.UpstreamError` so a route that hits
    it returns 502 rather than 500 — the distinction between "we broke" and
    "our upstream broke" is the first thing anyone triaging wants.
    """


class LLMResponseError(UpstreamError):
    """The model replied, but not with what was asked for."""


@dataclass
class LLMResult:
    """One completion, with the provenance a filing has to record."""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Populated by :func:`complete_json`.
    data: dict | None = None
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def is_configured() -> bool:
    """Whether a key is present. Callers degrade rather than fail when false."""
    return bool(settings.openrouter_api_key)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        # OpenRouter attributes usage to these; without them calls are
        # rate-limited more aggressively on the free tier.
        "HTTP-Referer": "https://complipilot.in",
        "X-Title": "CompliPilot",
    }


def _post(payload: dict) -> dict:
    """POST to OpenRouter with bounded retries.

    Retries only what a retry can fix: a timeout, a connection error, or a 429
    or 5xx. A 400 or 401 is a bug or a bad key, and retrying it twice only
    delays the error by a minute.
    """
    url = f"{settings.openrouter_base_url}/chat/completions"
    last_error: Exception | None = None

    for attempt in range(settings.openrouter_max_retries + 1):
        try:
            response = httpx.post(
                url,
                headers=_headers(),
                json=payload,
                timeout=settings.openrouter_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            logger.warning("OpenRouter transport error (attempt %s): %s", attempt + 1, exc)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            last_error = LLMUnavailable(
                f"OpenRouter returned {response.status_code}",
                details={"status": response.status_code},
            )
            logger.warning(
                "OpenRouter %s (attempt %s)", response.status_code, attempt + 1
            )
            continue

        if response.status_code >= 400:
            # Not retried. Surfaced with the body, because OpenRouter's 400s
            # name the actual problem (a model id that does not exist, a
            # context length exceeded) and swallowing that message would make
            # the error untriageable.
            raise LLMUnavailable(
                f"OpenRouter rejected the request ({response.status_code})",
                details={"status": response.status_code, "body": response.text[:1000]},
            )

        return response.json()

    raise LLMUnavailable(
        "OpenRouter is unreachable after retries",
        details={"attempts": settings.openrouter_max_retries + 1},
    ) from last_error


def complete(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2000,
    images: list[str] | None = None,
) -> LLMResult:
    """One completion.

    ``temperature`` defaults low: every use in this product is extraction or
    classification, where the desirable property is that the same circular
    analysed twice gives the same answer.

    ``images`` are data URLs, which routes the call to the vision model — the
    path a scanned notice with no text layer takes.
    """
    if not is_configured():
        raise LLMUnavailable("OpenRouter is not configured (no API key)")

    chosen = model or (
        settings.openrouter_vision_model if images else settings.openrouter_model
    )

    if images:
        content: Any = [
            {"type": "text", "text": prompt},
            *[{"type": "image_url", "image_url": {"url": url}} for url in images],
        ]
    else:
        content = prompt

    payload = {
        "model": chosen,
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    body = _post(payload)

    try:
        message = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMResponseError(
            "OpenRouter response had no message content",
            details={"body": str(body)[:1000]},
        ) from exc

    usage = body.get("usage") or {}
    return LLMResult(
        content=message or "",
        model=body.get("model", chosen),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        raw=body,
    )


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model's reply.

    Three fallbacks, in order of how much benefit of the doubt they give:
    the whole string, the contents of a markdown fence, and the outermost
    braced span. Models emit all three shapes despite the instruction not to,
    and a parse failure here would otherwise discard a perfectly usable answer.
    """
    candidates = [text.strip()]

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        # A model asked for an object sometimes returns a single-element array
        # containing it. Unwrapping is safe and saves a retry.
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]

    raise LLMResponseError(
        "Model did not return parseable JSON", details={"reply": text[:1000]}
    )


def complete_json(
    prompt: str,
    *,
    required_keys: set[str] | None = None,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 2000,
    images: list[str] | None = None,
) -> LLMResult:
    """A completion that must parse as a JSON object.

    ``required_keys`` is checked after parsing. A model that returns valid JSON
    with none of the fields asked for has failed just as completely as one that
    returned prose, and catching it here means the caller can trust the shape
    of ``result.data`` instead of defending against it at every access.
    """
    result = complete(
        prompt,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        images=images,
    )
    data = extract_json(result.content)

    if required_keys:
        missing = required_keys - set(data)
        if missing:
            raise LLMResponseError(
                "Model response was missing required fields",
                details={"missing": sorted(missing), "received": sorted(data)},
            )

    result.data = data
    return result


def confidence_of(data: dict, *, default: int = 50) -> int:
    """Read a model-reported confidence, clamped to 0-100.

    Models report confidence as 0-100, as 0-1, and occasionally as prose. All
    three are normalised rather than trusted, because this number is stored on
    the filing and shown to a person deciding how carefully to check it.
    """
    raw = data.get("confidence", default)

    if isinstance(raw, str):
        try:
            raw = float(raw.strip().rstrip("%"))
        except ValueError:
            return default

    if not isinstance(raw, (int, float)):
        return default

    # A model that answered 0.85 meant 85%, not 1%. The ambiguity at exactly
    # 1 is resolved as 1% — the pessimistic reading, which errs toward telling
    # a user to check the output.
    value = float(raw)
    if 0 < value < 1:
        value *= 100

    return max(0, min(100, int(round(value))))
