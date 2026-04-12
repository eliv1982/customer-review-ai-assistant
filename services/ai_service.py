"""
Вызовы OpenAI API: структурированный анализ текста отзыва.

Используется Chat Completions с json_schema (строгая схема).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

Sentiment = Literal["positive", "neutral", "negative", "mixed"]
Topic = Literal[
    "delivery",
    "product_quality",
    "support",
    "price",
    "website",
    "returns",
    "packaging",
    "other",
]

SENTIMENT_VALUES: Final[tuple[str, ...]] = ("positive", "neutral", "negative", "mixed")
TOPIC_VALUES: Final[tuple[str, ...]] = (
    "delivery",
    "product_quality",
    "support",
    "price",
    "website",
    "returns",
    "packaging",
    "other",
)


@dataclass(frozen=True)
class StructuredReviewAnalysis:
    """Результат анализа отзыва: строго заданные поля."""

    sentiment: Sentiment
    topic: Topic
    summary: str
    reply_draft: str


class AnalysisError(Exception):
    """Ошибка конфигурации, разбора ответа или вызова API анализа отзыва."""


def _review_analysis_json_schema() -> dict[str, Any]:
    """Схема JSON для OpenAI strict structured outputs."""
    return {
        "type": "object",
        "properties": {
            "sentiment": {
                "type": "string",
                "enum": list(SENTIMENT_VALUES),
                "description": "Тональность отзыва (английский литерал).",
            },
            "topic": {
                "type": "string",
                "enum": list(TOPIC_VALUES),
                "description": "Основная тема отзыва (английский литерал).",
            },
            "summary": {
                "type": "string",
                "description": "Краткая сводка на русском.",
            },
            "reply_draft": {
                "type": "string",
                "description": "Черновик ответа клиенту на русском.",
            },
        },
        "required": ["sentiment", "topic", "summary", "reply_draft"],
        "additionalProperties": False,
    }


def _build_user_message(review_text: str) -> str:
    return (
        "Проанализируй отзыв клиента ниже. Верни JSON по схеме: sentiment, topic, "
        "summary (русский), reply_draft (русский).\n\n"
        f"Текст отзыва:\n---\n{review_text.strip()}\n---"
    )


def _parse_and_validate(payload: str) -> StructuredReviewAnalysis:
    try:
        data: Any = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.exception("Ответ модели не является корректным JSON")
        raise AnalysisError("Модель вернула некорректный JSON") from e

    if not isinstance(data, dict):
        raise AnalysisError("Корень JSON должен быть объектом")

    try:
        sentiment_raw = data["sentiment"]
        topic_raw = data["topic"]
        summary = data["summary"]
        reply_draft = data["reply_draft"]
    except KeyError as e:
        raise AnalysisError(f"В JSON не хватает поля: {e.args[0]}") from e

    if sentiment_raw not in SENTIMENT_VALUES:
        raise AnalysisError(f"Недопустимое значение sentiment: {sentiment_raw!r}")
    if topic_raw not in TOPIC_VALUES:
        raise AnalysisError(f"Недопустимое значение topic: {topic_raw!r}")
    if not isinstance(summary, str) or not summary.strip():
        raise AnalysisError("Поле summary должно быть непустой строкой")
    if not isinstance(reply_draft, str) or not reply_draft.strip():
        raise AnalysisError("Поле reply_draft должно быть непустой строкой")

    return StructuredReviewAnalysis(
        sentiment=cast(Sentiment, sentiment_raw),
        topic=cast(Topic, topic_raw),
        summary=summary.strip(),
        reply_draft=reply_draft.strip(),
    )


def analyze_review(
    *,
    api_key: str | None,
    model: str,
    review_text: str,
) -> StructuredReviewAnalysis:
    """
    Отправляет текст отзыва в OpenAI и возвращает структурированный анализ.

    ``api_key`` и ``model`` обычно берутся из ``get_settings()`` (переменные окружения).
    """
    if not api_key or not str(api_key).strip():
        raise AnalysisError("Не задан OPENAI_API_KEY")
    if not model or not str(model).strip():
        raise AnalysisError("Не задан OPENAI_MODEL")
    text = (review_text or "").strip()
    if not text:
        raise AnalysisError("Пустой текст отзыва")

    client = OpenAI(api_key=api_key.strip())
    schema = _review_analysis_json_schema()
    response_format: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": "review_analysis",
            "strict": True,
            "schema": schema,
        },
    }

    try:
        completion = client.chat.completions.create(
            model=model.strip(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(text)},
            ],
            response_format=response_format,  # type: ignore[arg-type]
        )
    except RateLimitError as e:
        logger.warning("Превышен лимит запросов OpenAI: %s", e)
        raise AnalysisError("Превышен лимит запросов к OpenAI, повторите позже") from e
    except APIConnectionError as e:
        logger.error("Нет соединения с OpenAI: %s", e)
        raise AnalysisError("Не удалось подключиться к OpenAI") from e
    except APIStatusError as e:
        logger.error(
            "Ошибка HTTP OpenAI: status=%s body=%s",
            getattr(e, "status_code", "?"),
            getattr(e, "response", None),
        )
        raise AnalysisError(f"Ошибка API OpenAI: {e}") from e
    except Exception as e:
        logger.exception("Неожиданная ошибка при вызове OpenAI")
        raise AnalysisError(f"Сбой при анализе отзыва: {e}") from e

    choice = completion.choices[0] if completion.choices else None
    if choice is None or choice.message is None:
        raise AnalysisError("Пустой ответ от OpenAI")

    raw_content = choice.message.content
    if not raw_content:
        raise AnalysisError("В ответе OpenAI нет содержимого")

    return _parse_and_validate(raw_content)
