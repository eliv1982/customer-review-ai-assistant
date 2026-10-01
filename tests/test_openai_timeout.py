"""OpenAI-клиент создаётся с явным конечным таймаутом (без сети и без реального ключа)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from openai import OpenAI

from services import ai_service


class _RecordingOpenAI(OpenAI):
    """Настоящий клиент SDK (проверяет имена kwargs), но без сетевых вызовов."""

    instances: list["_RecordingOpenAI"] = []

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)

    @property
    def chat(self):  # type: ignore[override]
        payload = json.dumps(
            {"sentiment": "neutral", "topic": "other", "summary": "Кратко.", "reply_draft": "Ответ."}
        )
        message = SimpleNamespace(content=payload)
        completion = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: completion)
        )


def test_analyze_review_creates_client_with_explicit_timeout(monkeypatch) -> None:
    _RecordingOpenAI.instances = []
    monkeypatch.setattr(ai_service, "OpenAI", _RecordingOpenAI)

    result = ai_service.analyze_review(
        api_key="test-key-not-real", model="test-model", review_text="Текст отзыва."
    )

    assert result.topic == "other"
    assert len(_RecordingOpenAI.instances) == 1
    client = _RecordingOpenAI.instances[0]
    assert client.timeout == ai_service.OPENAI_TIMEOUT_SECONDS
    assert client.max_retries == ai_service.OPENAI_MAX_RETRIES


def test_openai_timeout_is_finite_and_interactive() -> None:
    assert isinstance(ai_service.OPENAI_TIMEOUT_SECONDS, (int, float))
    assert 0 < ai_service.OPENAI_TIMEOUT_SECONDS <= 60
    assert ai_service.OPENAI_MAX_RETRIES >= 0
