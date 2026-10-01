"""Деплой-ограничение ``ALLOWED_TELEGRAM_IDS``: пусто — как раньше, иначе только перечисленные ID.

Отказ обязан наступить до обращений к OpenAI и БД, поэтому проверяются вызовы анализа,
пайплайна и отчёта, а также неизменность файла SQLite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Router

import config
from bot import handlers as bot_handlers_module
from bot import messages as msg
from bot.handlers import setup_handlers
from config import Settings, parse_allowed_telegram_ids
from services import review_pipeline
from services.ai_service import StructuredReviewAnalysis
from services.review_service import list_reviews

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)
_REVIEW = "Курьер опоздал на два часа."
_ALLOWED = 111
_STRANGER = 222


@pytest.fixture
def spies(monkeypatch):
    """Считает обращения к OpenAI-анализу, синхронной границе обработки и отчёту."""
    state = SimpleNamespace(analysis=0, pipeline=0, report=0)

    def analyze(**_kwargs):
        state.analysis += 1
        return _ANALYSIS

    real_pipeline = bot_handlers_module._load_or_process_review
    real_snapshot = bot_handlers_module.build_analytics_snapshot

    def pipeline(*args, **kwargs):
        state.pipeline += 1
        return real_pipeline(*args, **kwargs)

    def snapshot(*args, **kwargs):
        state.report += 1
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(review_pipeline, "analyze_review", analyze)
    monkeypatch.setattr(bot_handlers_module, "_load_or_process_review", pipeline)
    monkeypatch.setattr(bot_handlers_module, "build_analytics_snapshot", snapshot)
    return state


def _run(handler, message) -> None:
    asyncio.run(asyncio.wait_for(handler(message), timeout=30))


# --- разбор настройки ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t "])
def test_empty_value_means_no_restriction(value) -> None:
    assert parse_allowed_telegram_ids(value) == frozenset()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123", {123}),
        ("111,222", {111, 222}),
        (" 111 , 222 ,", {111, 222}),
        ("111 222\n333", {111, 222, 333}),
        ("111,111", {111}),
        ("007", {7}),
    ],
)
def test_ids_are_parsed_from_commas_and_whitespace(value, expected) -> None:
    assert parse_allowed_telegram_ids(value) == frozenset(expected)


@pytest.mark.parametrize(
    "value",
    [
        "abc", "12a", "-5", "0", "1.5", "111;222", "@username", "111,abc", "١٢٣", "٣",
        # Только разделители: значение непустое, но ни одного ID — не «ограничение выключено».
        ",", ", ,", " , , ", " , ,\n", ",,,", "\t,\n",
    ],
)
def test_malformed_value_fails_loudly_instead_of_unrestricting(value) -> None:
    # Опечатка не должна молча превратиться в пустой список, то есть в «бот открыт для всех».
    with pytest.raises(ValueError, match="ALLOWED_TELEGRAM_IDS"):
        parse_allowed_telegram_ids(value)


def test_settings_read_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("ALLOWED_TELEGRAM_IDS", raising=False)
    assert config.get_settings().allowed_telegram_ids == frozenset()

    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "111, 222")
    assert config.get_settings().allowed_telegram_ids == frozenset({111, 222})

    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "")
    assert config.get_settings().allowed_telegram_ids == frozenset()

    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "   ")
    assert config.get_settings().allowed_telegram_ids == frozenset()

    for bad in ("111, oops", ",", " , , "):
        monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", bad)
        with pytest.raises(ValueError, match="ALLOWED_TELEGRAM_IDS"):
            config.get_settings()


def test_settings_default_is_unrestricted() -> None:
    settings = Settings(
        telegram_bot_token=None,
        openai_api_key=None,
        database_path="x.db",
        openai_model="m",
        log_level="INFO",
    )

    assert settings.allowed_telegram_ids == frozenset()


# --- поведение хендлеров ------------------------------------------------------------------------


def test_unset_guard_keeps_bot_open_to_everyone(tmp_db, make_bot_handlers, fake_message, spies) -> None:
    handlers = make_bot_handlers(tmp_db)  # allowed_telegram_ids не задан
    review = fake_message(_REVIEW, user_id=_STRANGER)
    help_message = fake_message("/help", user_id=_STRANGER)
    no_sender_help = fake_message("/help", user_id=None)

    _run(handlers["cmd_help"], help_message)
    _run(handlers["cmd_help"], no_sender_help)
    _run(handlers["on_review_text"], review)
    _run(handlers["cmd_report"], fake_message("/report", user_id=_STRANGER))

    assert help_message.answers == [msg.HELP_TEXT]
    assert no_sender_help.answers == [msg.HELP_TEXT]
    assert review.answers and msg.ACCESS_DENIED not in review.answers[0]
    assert spies.analysis == 1 and spies.report == 1
    assert len(list_reviews(tmp_db)) == 1


def test_listed_user_is_served_when_guard_is_configured(
    tmp_db, make_bot_handlers, fake_message, spies
) -> None:
    handlers = make_bot_handlers(tmp_db, allowed_telegram_ids=frozenset({_ALLOWED}))
    review = fake_message(_REVIEW, user_id=_ALLOWED)
    report = fake_message("/report", user_id=_ALLOWED)

    _run(handlers["on_review_text"], review)
    _run(handlers["cmd_report"], report)

    assert review.answers and msg.ACCESS_DENIED not in review.answers[0]
    assert report.answers[0].startswith("Сводка по отзывам")
    assert spies.analysis == 1 and spies.report == 1
    assert [r["source"] for r in list_reviews(tmp_db)] == [f"telegram:{_ALLOWED}"]


@pytest.mark.parametrize("sender", [_STRANGER, None], ids=["not_listed", "no_sender"])
def test_denied_user_reaches_neither_openai_nor_database(
    tmp_db, make_bot_handlers, fake_message, spies, sender
) -> None:
    handlers = make_bot_handlers(tmp_db, allowed_telegram_ids=frozenset({_ALLOWED}))
    db_before = Path(tmp_db).read_bytes()
    answers: dict[str, list[str]] = {}

    # Все зарегистрированные хендлеры: новый хендлер без защиты сделает этот тест красным.
    assert {"cmd_start", "cmd_help", "cmd_new_review", "cmd_report", "on_review_text"} <= set(handlers)
    for name, handler in handlers.items():
        message = fake_message(_REVIEW, user_id=sender)
        _run(handler, message)
        answers[name] = message.answers

    assert all(a == [msg.ACCESS_DENIED] for a in answers.values()), answers
    assert (spies.analysis, spies.pipeline, spies.report) == (0, 0, 0)
    assert Path(tmp_db).read_bytes() == db_before
    assert list_reviews(tmp_db) == []


def test_denied_user_does_not_block_a_listed_user(
    tmp_db, make_bot_handlers, fake_message, spies
) -> None:
    handlers = make_bot_handlers(tmp_db, allowed_telegram_ids=frozenset({_ALLOWED}))
    denied = fake_message(_REVIEW, user_id=_STRANGER)
    allowed = fake_message(_REVIEW, user_id=_ALLOWED)

    _run(handlers["on_review_text"], denied)
    _run(handlers["on_review_text"], allowed)

    assert denied.answers == [msg.ACCESS_DENIED]
    assert allowed.answers and msg.ACCESS_DENIED not in allowed.answers[0]
    assert spies.analysis == 1


def test_guarded_handlers_are_still_invoked_through_aiogram(
    tmp_db, fake_message, spies
) -> None:
    # ``HandlerObject.call`` — собственный вызов aiogram: проверяет, что обёртка распознана как корутина.
    router = Router()
    setup_handlers(
        router,
        Settings(
            telegram_bot_token=None,
            openai_api_key="test-key-not-real",
            database_path=tmp_db,
            openai_model="test-model",
            log_level="INFO",
            allowed_telegram_ids=frozenset({_ALLOWED}),
        ),
    )
    help_handler = next(h for h in router.message.handlers if h.callback.__name__ == "cmd_help")
    allowed = fake_message("/help", user_id=_ALLOWED)
    denied = fake_message("/help", user_id=_STRANGER)

    asyncio.run(help_handler.call(allowed))
    asyncio.run(help_handler.call(denied))

    assert allowed.answers == [msg.HELP_TEXT]
    assert denied.answers == [msg.ACCESS_DENIED]
