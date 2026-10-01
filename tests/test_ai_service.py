"""Сервис анализа отзыва: разбор ответа модели, валидация enum и отображение ошибок SDK.

Клиент ``OpenAI`` подменён заглушкой на уровне модуля ``ai_service``: сети и реальных ключей нет
(дополнительно сеть закрыта автоматической фикстурой в ``conftest.py``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from prompts import SYSTEM_PROMPT
from services import ai_service
from services.ai_service import AnalysisError, StructuredReviewAnalysis, analyze_review

_REQUEST = httpx.Request("POST", "https://api.openai.test/v1/chat/completions")

_VALID_PAYLOAD = {
    "sentiment": "negative",
    "topic": "delivery",
    "summary": "Доставка задержалась.",
    "reply_draft": "Приносим извинения за задержку.",
}


def _completion(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _status_error(cls: type[Exception], status: int) -> Exception:
    return cls("boom", response=httpx.Response(status, request=_REQUEST), body=None)


@pytest.fixture
def fake_openai(monkeypatch):
    """Подменяет ``ai_service.OpenAI``; ``outcome`` — готовый ответ или исключение для ``create``."""
    state = SimpleNamespace(clients=[], requests=[], outcome=_completion(json.dumps(_VALID_PAYLOAD)))

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            state.clients.append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            state.requests.append(kwargs)
            if isinstance(state.outcome, BaseException):
                raise state.outcome
            return state.outcome

    monkeypatch.setattr(ai_service, "OpenAI", FakeOpenAI)
    return state


def _analyze(text: str = "Курьер опоздал.") -> StructuredReviewAnalysis:
    return analyze_review(api_key="test-key-not-real", model="test-model", review_text=text)


# --- успешный разбор ---------------------------------------------------------------------------


def test_valid_response_is_parsed_into_structured_analysis(fake_openai) -> None:
    fake_openai.outcome = _completion(
        json.dumps({**_VALID_PAYLOAD, "summary": "  Доставка задержалась.  ", "reply_draft": "\nОтвет.\n"})
    )

    result = _analyze()

    assert result == StructuredReviewAnalysis(
        sentiment="negative",
        topic="delivery",
        summary="Доставка задержалась.",
        reply_draft="Ответ.",
    )


def test_request_carries_strict_schema_prompt_and_review_text(fake_openai) -> None:
    analyze_review(
        api_key="  test-key-not-real  ", model=" test-model ", review_text="  Текст отзыва.  "
    )

    assert fake_openai.clients[0]["api_key"] == "test-key-not-real"
    (request,) = fake_openai.requests
    assert request["model"] == "test-model"

    system, user = request["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert user["role"] == "user"
    assert "Текст отзыва." in user["content"]

    fmt = request["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"sentiment", "topic", "summary", "reply_draft"}
    assert tuple(schema["properties"]["sentiment"]["enum"]) == ai_service.SENTIMENT_VALUES
    assert tuple(schema["properties"]["topic"]["enum"]) == ai_service.TOPIC_VALUES


def test_enum_tuples_match_literal_types() -> None:
    # Схема для модели строится из кортежей, типы — из Literal: расхождение пропустило бы лишнее значение.
    assert get_args(ai_service.Sentiment) == ai_service.SENTIMENT_VALUES
    assert get_args(ai_service.Topic) == ai_service.TOPIC_VALUES


# --- некорректный вывод модели -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("это не JSON", "некорректный JSON"),
        ("{", "некорректный JSON"),
        ("[]", "объектом"),
        ('"текст"', "объектом"),
        ("null", "объектом"),
        ("", "нет содержимого"),
        (None, "нет содержимого"),
    ],
)
def test_invalid_model_output_is_rejected(fake_openai, content, expected) -> None:
    fake_openai.outcome = _completion(content)

    with pytest.raises(AnalysisError, match=expected):
        _analyze()


@pytest.mark.parametrize("field", ["sentiment", "topic", "summary", "reply_draft"])
def test_missing_field_is_rejected(fake_openai, field) -> None:
    payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != field}
    fake_openai.outcome = _completion(json.dumps(payload))

    with pytest.raises(AnalysisError, match=field):
        _analyze()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sentiment", "furious"),
        ("sentiment", "Negative"),
        ("sentiment", " negative"),
        ("sentiment", 5),
        ("sentiment", None),
        ("topic", "shipping"),
        ("topic", "DELIVERY"),
        ("topic", ["delivery"]),
    ],
)
def test_value_outside_enum_is_rejected(fake_openai, field, value) -> None:
    fake_openai.outcome = _completion(json.dumps({**_VALID_PAYLOAD, field: value}))

    with pytest.raises(AnalysisError, match=f"Недопустимое значение {field}"):
        _analyze()


@pytest.mark.parametrize("field", ["summary", "reply_draft"])
@pytest.mark.parametrize("value", ["", "   ", None, 42, ["текст"]])
def test_blank_or_non_string_text_fields_are_rejected(fake_openai, field, value) -> None:
    fake_openai.outcome = _completion(json.dumps({**_VALID_PAYLOAD, field: value}))

    with pytest.raises(AnalysisError, match=field):
        _analyze()


@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
    ],
    ids=["no_choices", "no_message"],
)
def test_empty_completion_is_rejected(fake_openai, completion) -> None:
    fake_openai.outcome = completion

    with pytest.raises(AnalysisError, match="Пустой ответ"):
        _analyze()


# --- ошибки SDK ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status_error(RateLimitError, 429), "лимит запросов"),
        (APIConnectionError(request=_REQUEST), "подключиться"),
        (APITimeoutError(request=_REQUEST), "подключиться"),
        (_status_error(AuthenticationError, 401), "Ошибка API OpenAI"),
        (_status_error(BadRequestError, 400), "Ошибка API OpenAI"),
        (_status_error(InternalServerError, 500), "Ошибка API OpenAI"),
        (RuntimeError("неожиданный сбой"), "Сбой при анализе"),
    ],
    ids=[
        "rate_limit",
        "connection",
        "timeout",
        "auth",
        "bad_request",
        "server_error",
        "unexpected",
    ],
)
def test_sdk_errors_are_mapped_to_analysis_error(fake_openai, error, expected) -> None:
    fake_openai.outcome = error

    with pytest.raises(AnalysisError, match=expected) as excinfo:
        _analyze()

    assert excinfo.value.__cause__ is error


# --- предусловия: клиент не создаётся -----------------------------------------------------------


@pytest.mark.parametrize(
    ("api_key", "model", "text", "expected"),
    [
        (None, "test-model", "Текст.", "OPENAI_API_KEY"),
        ("   ", "test-model", "Текст.", "OPENAI_API_KEY"),
        ("test-key-not-real", "", "Текст.", "OPENAI_MODEL"),
        ("test-key-not-real", "   ", "Текст.", "OPENAI_MODEL"),
        ("test-key-not-real", "test-model", "", "Пустой текст"),
        ("test-key-not-real", "test-model", "  \n ", "Пустой текст"),
    ],
)
def test_missing_inputs_fail_before_any_client_is_created(
    fake_openai, api_key, model, text, expected
) -> None:
    with pytest.raises(AnalysisError, match=expected):
        analyze_review(api_key=api_key, model=model, review_text=text)

    assert fake_openai.clients == []
    assert fake_openai.requests == []
