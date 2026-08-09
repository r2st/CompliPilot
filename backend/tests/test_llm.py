"""The OpenRouter client and its structured-output contract.

No network. Every test substitutes ``httpx.post``, because what is being
checked is this module's handling of what a model returns — including the
several ways it returns something other than what was asked for — and a test
that needed a live free-tier model would be both slow and flaky about exactly
the cases that matter.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services import llm


def _reply(content: str, *, model: str = "openai/gpt-oss-20b:free") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def keyed(monkeypatch):
    """A configured API key, so calls are attempted rather than refused."""
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-test-key")


@pytest.fixture
def posts(monkeypatch):
    """Capture calls to ``httpx.post`` and script their replies."""

    calls: list[dict] = []
    scripted: list = []

    def _post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if not scripted:
            return _Response(_reply("{}"))
        outcome = scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "post", _post)
    return {"calls": calls, "scripted": scripted}


class TestConfiguration:
    def test_an_unkeyed_deployment_reports_itself_unconfigured(self, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_api_key", "")
        assert not llm.is_configured()

    def test_calling_without_a_key_raises_rather_than_posting(self, monkeypatch, posts):
        monkeypatch.setattr(settings, "openrouter_api_key", "")
        with pytest.raises(llm.LLMUnavailable):
            llm.complete("anything")
        assert posts["calls"] == []


class TestCompletion:
    def test_a_reply_is_returned_with_its_usage(self, keyed, posts):
        posts["scripted"].append(_Response(_reply("A draft response.")))

        result = llm.complete("Summarise this circular")

        assert result.content == "A draft response."
        assert result.total_tokens == 120

    def test_the_system_prompt_is_always_sent(self, keyed, posts):
        """The 'draft for review' instruction is not optional in this product."""
        posts["scripted"].append(_Response(_reply("ok")))

        llm.complete("anything")

        messages = posts["calls"][0]["json"]["messages"]
        assert messages[0]["role"] == "system"
        assert "DRAFT" in messages[0]["content"]

    def test_images_route_to_the_vision_model(self, keyed, posts):
        posts["scripted"].append(_Response(_reply("ok")))

        llm.complete("Read this notice", images=["data:image/png;base64,AAAA"])

        assert posts["calls"][0]["json"]["model"] == settings.openrouter_vision_model

    def test_text_routes_to_the_default_model(self, keyed, posts):
        posts["scripted"].append(_Response(_reply("ok")))

        llm.complete("Read this notice")

        assert posts["calls"][0]["json"]["model"] == settings.openrouter_model

    def test_a_reply_with_no_choices_is_an_error_not_an_empty_string(self, keyed, posts):
        posts["scripted"].append(_Response({"model": "m", "choices": []}))

        with pytest.raises(llm.LLMResponseError):
            llm.complete("anything")


class TestRetries:
    def test_a_timeout_is_retried(self, keyed, posts):
        posts["scripted"].extend(
            [httpx.ReadTimeout("slow"), _Response(_reply("recovered"))]
        )

        assert llm.complete("anything").content == "recovered"
        assert len(posts["calls"]) == 2

    def test_a_rate_limit_is_retried(self, keyed, posts):
        posts["scripted"].extend(
            [_Response({}, status_code=429), _Response(_reply("recovered"))]
        )

        assert llm.complete("anything").content == "recovered"

    def test_a_server_error_is_retried(self, keyed, posts):
        posts["scripted"].extend(
            [_Response({}, status_code=503), _Response(_reply("recovered"))]
        )

        assert llm.complete("anything").content == "recovered"

    def test_a_bad_request_is_not_retried(self, keyed, posts):
        """A 400 is a bug or a bad model id; retrying only delays the error."""
        posts["scripted"].append(
            _Response({}, status_code=400, text="no such model")
        )

        with pytest.raises(llm.LLMUnavailable) as excinfo:
            llm.complete("anything")

        assert len(posts["calls"]) == 1
        # The upstream message is preserved — OpenRouter's 400s name the actual
        # problem, and swallowing it makes the error untriageable.
        assert "no such model" in str(excinfo.value.details)

    def test_exhausted_retries_raise_unavailable(self, keyed, posts, monkeypatch):
        monkeypatch.setattr(settings, "openrouter_max_retries", 1)
        posts["scripted"].extend([httpx.ConnectError("down"), httpx.ConnectError("down")])

        with pytest.raises(llm.LLMUnavailable):
            llm.complete("anything")
        assert len(posts["calls"]) == 2


class TestJsonExtraction:
    def test_a_bare_object_parses(self):
        assert llm.extract_json('{"a": 1}') == {"a": 1}

    def test_a_markdown_fence_is_stripped(self):
        """Models emit these constantly despite being told not to."""
        assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_a_plain_fence_is_stripped(self):
        assert llm.extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_an_object_is_discarded(self):
        reply = 'Here is the analysis:\n{"a": 1}\nHope that helps!'
        assert llm.extract_json(reply) == {"a": 1}

    def test_a_single_element_array_is_unwrapped(self):
        assert llm.extract_json('[{"a": 1}]') == {"a": 1}

    def test_nested_braces_survive_the_outermost_span_fallback(self):
        reply = 'Result: {"a": {"b": 2}} done'
        assert llm.extract_json(reply) == {"a": {"b": 2}}

    def test_prose_with_no_json_raises(self):
        with pytest.raises(llm.LLMResponseError):
            llm.extract_json("I'm afraid I can't help with that.")

    def test_a_json_scalar_is_not_an_object(self):
        with pytest.raises(llm.LLMResponseError):
            llm.extract_json("42")


class TestCompleteJson:
    def test_required_keys_present_returns_the_data(self, keyed, posts):
        posts["scripted"].append(_Response(_reply('{"summary": "s", "confidence": 90}')))

        result = llm.complete_json("prompt", required_keys={"summary", "confidence"})

        assert result.data == {"summary": "s", "confidence": 90}

    def test_a_missing_required_key_is_an_error(self, keyed, posts):
        """Valid JSON with none of the asked-for fields has failed just as
        completely as prose, and the caller must not have to check."""
        posts["scripted"].append(_Response(_reply('{"something_else": 1}')))

        with pytest.raises(llm.LLMResponseError) as excinfo:
            llm.complete_json("prompt", required_keys={"summary"})

        assert "summary" in str(excinfo.value.details)

    def test_extra_keys_are_allowed(self, keyed, posts):
        posts["scripted"].append(_Response(_reply('{"summary": "s", "extra": 1}')))

        result = llm.complete_json("prompt", required_keys={"summary"})

        assert result.data is not None
        assert result.data["extra"] == 1


class TestConfidence:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (90, 90),
            (0, 0),
            (100, 100),
            # A model that answered 0.85 meant 85%.
            (0.85, 85),
            ("75", 75),
            ("75%", 75),
            # Out of range, clamped rather than trusted.
            (150, 100),
            (-10, 0),
        ],
    )
    def test_confidence_is_normalised(self, raw, expected):
        assert llm.confidence_of({"confidence": raw}) == expected

    def test_a_missing_confidence_takes_the_default(self):
        assert llm.confidence_of({}) == 50

    def test_unparseable_prose_takes_the_default(self):
        assert llm.confidence_of({"confidence": "quite sure"}) == 50

    def test_exactly_one_is_read_as_one_percent(self):
        """The pessimistic reading, which errs toward telling a user to check."""
        assert llm.confidence_of({"confidence": 1}) == 1
