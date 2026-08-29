"""Providers for the reasoning zone.

The reasoning zone holds no credentials that can move money, so which model
sits in it is a swappable detail — and demonstrating that is part of the point.
If the architecture only works with a frontier model, the kernel is not carrying
its weight. If it works with a 7B running on a laptop, the kernel is.

Three implementations, one interface:

* ``AnthropicProvider``  — the Anthropic SDK. Default when ANTHROPIC_API_KEY is set.
* ``OpenAICompatProvider`` — any /v1/chat/completions endpoint. Covers Groq,
  OpenRouter, Together, Cerebras and Gemini's compatibility endpoint, all of
  which have a free tier, plus Ollama's compat port.
* ``OllamaProvider`` — Ollama's native API, which does grammar-constrained
  decoding against a JSON schema. Slower to set up, much more reliable on small
  models, and costs nothing at all.

Selection is by environment, so switching providers never touches the kernel.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Protocol

from vasool.diagnosis.schema import Proposal

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"


class ProviderError(Exception):
    """The provider could not produce a usable proposal."""


class RateLimited(ProviderError):
    """The provider is throttling. Carries how long it asked us to wait."""

    def __init__(self, retry_after: float, detail: str = "") -> None:
        super().__init__(f"rate limited, retry in {retry_after:.1f}s: {detail}")
        self.retry_after = retry_after


_RETRY_HINT = re.compile(r"try again in ([0-9.]+)s")


def _retry_after(exc: "urllib.error.HTTPError", body: str) -> float:
    """How long to wait. Prefer the header, then the message, then a default."""
    header = exc.headers.get("retry-after") if exc.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = _RETRY_HINT.search(body)
    return float(match.group(1)) + 0.5 if match else 5.0


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[dict[str, Any]]) -> Optional[Proposal]:
        """Return a validated Proposal, or raise. None means unparseable."""
        ...


def _strict_schema() -> dict[str, Any]:
    """Pydantic's schema, tightened for providers that enforce strict mode."""
    schema = Proposal.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema


#: Several hosted providers sit behind a WAF that rejects the default
#: ``Python-urllib/3.x`` agent with a 403 before the request ever reaches the
#: API. Identifying the client properly is the whole fix.
USER_AGENT = "vasool/0.1.0 (payment-recovery benchmark)"


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: float, max_retries: int = 8) -> dict[str, Any]:
    """POST with backoff that honours the provider's own retry hint.

    Free tiers throttle by tokens per minute, and a 429 there is not a failure
    — it is the provider telling you exactly how long to wait. Treating it as
    an error would degrade cases to the rules path for no reason and quietly
    understate what the model can do.
    """
    for attempt in range(max_retries + 1):
        try:
            return _post_once(url, payload, headers, timeout)
        except RateLimited as limited:
            if attempt == max_retries:
                raise
            time.sleep(min(limited.retry_after * (1.3 ** attempt), 60.0))
    raise ProviderError("unreachable")


def _post_once(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT, **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:400]
        if exc.code == 429:
            raise RateLimited(_retry_after(exc, body), body) from exc
        raise ProviderError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"connection failed: {exc.reason}") from exc


# ---------------------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str = "low",
                 client: Any = None) -> None:
        self.model = model or os.environ.get("VASOOL_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.effort = effort
        self._client = client
        self._ready = client is not None

    def _get(self) -> Any:
        if not self._ready:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except Exception as exc:
                raise ProviderError(f"anthropic client unavailable: {exc}") from exc
            self._ready = True
        if self._client is None:
            raise ProviderError("no anthropic client")
        return self._client

    def complete(self, system: str, messages: list[dict[str, Any]]) -> Optional[Proposal]:
        client = self._get()
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=4096, system=system,
            messages=messages, output_format=Proposal,
        )
        try:
            response = client.messages.parse(
                **kwargs, thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except TypeError:
            response = client.messages.parse(**kwargs)
        return getattr(response, "parsed_output", None)


class OpenAICompatProvider:
    """Any OpenAI-shaped /chat/completions endpoint.

    Tries strict JSON-schema mode first and falls back to plain JSON mode with
    the schema in the prompt, because free-tier providers vary in what they
    accept and a hard failure on the first request would be a poor reason to
    lose a case.
    """

    name = "openai-compat"

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._schema_mode: str | None = None      # learned on first success

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def complete(self, system: str, messages: list[dict[str, Any]]) -> Optional[Proposal]:
        schema = _strict_schema()
        chat = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]

        modes = [self._schema_mode] if self._schema_mode else ["json_schema", "json_object"]
        last_error: Exception | None = None

        for mode in modes:
            payload: dict[str, Any] = {
                "model": self.model, "messages": list(chat),
                "temperature": 0, "max_tokens": 700,
            }
            if mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "proposal", "schema": schema,
                                    "strict": True},
                }
            else:
                payload["response_format"] = {"type": "json_object"}
                payload["messages"][0] = {
                    "role": "system",
                    "content": system + "\n\nReply with a single JSON object "
                    "matching this schema exactly, and nothing else:\n"
                    + json.dumps(schema),
                }
            try:
                body = _post_json(f"{self.base_url}/chat/completions", payload,
                                  self._headers(), self.timeout)
            except ProviderError as exc:
                last_error = exc
                continue

            self._schema_mode = mode
            return self._parse(body)

        raise ProviderError(str(last_error) if last_error else "no mode succeeded")

    @staticmethod
    def _parse(body: dict[str, Any]) -> Optional[Proposal]:
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return _validate(text)


class OllamaProvider:
    """Ollama's native API, with the JSON schema used for constrained decoding.

    Worth the extra implementation over the compat endpoint: constrained
    decoding means a 7B model produces schema-valid output nearly every time
    instead of nearly often.
    """

    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout: float = 180.0) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, system: str, messages: list[dict[str, Any]]) -> Optional[Proposal]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + [
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
            "stream": False,
            "format": _strict_schema(),
            "options": {"temperature": 0, "num_ctx": 8192},
        }
        body = _post_json(f"{self.base_url}/api/chat", payload, {}, self.timeout)
        return _validate(body.get("message", {}).get("content", ""))


def _validate(text: str) -> Optional[Proposal]:
    """Parse and validate. Returning None routes the case to the rules path."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1]
        candidate = candidate[4:] if candidate.startswith("json") else candidate
    try:
        return Proposal.model_validate_json(candidate.strip())
    except Exception:
        # Some models wrap the object in prose. Take the outermost braces.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return Proposal.model_validate_json(candidate[start:end + 1])
        except Exception:
            return None


# ---------------------------------------------------------------------------


def describe() -> str:
    """One line naming the provider that would be selected right now."""
    kind = os.environ.get("VASOOL_PROVIDER", "").lower()
    if not kind:
        kind = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "none"
    if kind == "ollama":
        return f"ollama · {os.environ.get('VASOOL_MODEL', 'qwen2.5:7b')} (local, free)"
    if kind in ("openai_compat", "openai-compat"):
        return (f"openai-compat · {os.environ.get('VASOOL_MODEL', '?')} @ "
                f"{os.environ.get('VASOOL_BASE_URL', '?')}")
    if kind == "anthropic":
        return f"anthropic · {os.environ.get('VASOOL_MODEL', DEFAULT_ANTHROPIC_MODEL)}"
    return "none — model arms will run from cache and degrade to the rules path"


def resolve() -> Optional[Provider]:
    """Pick a provider from the environment. Never raises."""
    kind = os.environ.get("VASOOL_PROVIDER", "").lower()
    if not kind:
        kind = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else ""

    if kind == "ollama":
        return OllamaProvider(
            model=os.environ.get("VASOOL_MODEL", "qwen2.5:7b"),
            base_url=os.environ.get("VASOOL_BASE_URL", "http://localhost:11434"),
        )
    if kind in ("openai_compat", "openai-compat"):
        base_url = os.environ.get("VASOOL_BASE_URL")
        model = os.environ.get("VASOOL_MODEL")
        if not (base_url and model):
            return None
        return OpenAICompatProvider(
            base_url=base_url, model=model,
            api_key=os.environ.get("VASOOL_API_KEY", ""),
        )
    if kind == "anthropic":
        return AnthropicProvider(effort=os.environ.get("VASOOL_EFFORT", "low"))
    return None
