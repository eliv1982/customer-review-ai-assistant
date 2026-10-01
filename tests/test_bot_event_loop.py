"""Синхронная работа OpenAI/SQLite в хендлерах не должна блокировать event loop.

Медленный «анализ» подменён функцией, которая ждёт ``threading.Event``. Если бы хендлер
вызывал его прямо в event loop, ожидание никто не смог бы прервать (тест бы упал по
таймауту ожидания, а не зависал). Сеть и реальные ключи не нужны.
"""

from __future__ import annotations

import asyncio
import threading

from bot import handlers as bot_handlers_module
from bot import messages as msg
from services import review_pipeline
from services.ai_service import StructuredReviewAnalysis

_WAIT_SECONDS = 5

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)


def test_slow_analysis_does_not_block_other_handlers(
    tmp_db, monkeypatch, make_bot_handlers, fake_message
) -> None:
    started = threading.Event()
    release = threading.Event()
    state = {"loop_blocked": False, "worker_thread": None}

    def slow_analyze(**_kwargs):
        state["worker_thread"] = threading.get_ident()
        started.set()
        if not release.wait(timeout=_WAIT_SECONDS):
            state["loop_blocked"] = True
        return _ANALYSIS

    monkeypatch.setattr(review_pipeline, "analyze_review", slow_analyze)
    handlers = make_bot_handlers(tmp_db)

    async def scenario() -> tuple[int, list[str], bool, list[str]]:
        loop_thread = threading.get_ident()
        review_msg = fake_message("Курьер опоздал на два часа.")
        help_msg = fake_message("/help", user_id=2)

        review_task = asyncio.create_task(handlers["on_review_text"](review_msg))
        while not started.is_set() and not review_task.done():
            await asyncio.sleep(0.01)

        # Анализ «завис» в рабочем потоке; несвязанный хендлер обязан отработать сейчас.
        await handlers["cmd_help"](help_msg)
        help_done_while_analysing = started.is_set() and not release.is_set()

        release.set()
        await review_task
        return loop_thread, help_msg.answers, help_done_while_analysing, review_msg.answers

    loop_thread, help_answers, help_done_while_analysing, review_answers = asyncio.run(
        asyncio.wait_for(scenario(), timeout=30)
    )

    assert not state["loop_blocked"], "анализ заблокировал event loop"
    assert help_done_while_analysing
    assert help_answers == [msg.HELP_TEXT]
    assert state["worker_thread"] != loop_thread
    assert review_answers and msg.ERROR_PIPELINE not in review_answers[0]


def test_reviews_from_different_users_are_analysed_concurrently(
    tmp_db, monkeypatch, make_bot_handlers, fake_message
) -> None:
    # Оба анализа должны быть «в полёте» одновременно, иначе барьер не сработает.
    barrier = threading.Barrier(2, timeout=_WAIT_SECONDS)
    state = {"serialized": False}

    def analyze_at_barrier(**_kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            state["serialized"] = True
        return _ANALYSIS

    monkeypatch.setattr(review_pipeline, "analyze_review", analyze_at_barrier)
    handlers = make_bot_handlers(tmp_db)
    first = fake_message("Первый отзыв про доставку.", user_id=10)
    second = fake_message("Второй отзыв про доставку.", user_id=20)

    async def scenario() -> None:
        await asyncio.wait_for(
            asyncio.gather(
                handlers["on_review_text"](first),
                handlers["on_review_text"](second),
            ),
            timeout=30,
        )

    asyncio.run(scenario())

    assert not state["serialized"], "анализы разных пользователей выполнились последовательно"
    assert first.answers and msg.ERROR_PIPELINE not in first.answers[0]
    assert second.answers and msg.ERROR_PIPELINE not in second.answers[0]


def test_same_user_duplicate_sent_while_analysing_is_served_from_cache(
    tmp_db, monkeypatch, make_bot_handlers, fake_message
) -> None:
    # Раньше блокирующий event loop сам сериализовал повторную отправку: второй апдейт
    # обрабатывался после первого и находил дубликат. Без event loop это нужно гарантировать явно.
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_analyze(**kwargs):
        calls.append(kwargs["review_text"])
        started.set()
        release.wait(timeout=_WAIT_SECONDS)
        return _ANALYSIS

    monkeypatch.setattr(review_pipeline, "analyze_review", slow_analyze)
    handlers = make_bot_handlers(tmp_db)
    first = fake_message("Один и тот же отзыв.", user_id=7)
    second = fake_message("Один и тот же отзыв.", user_id=7)

    async def scenario() -> None:
        first_task = asyncio.create_task(handlers["on_review_text"](first))
        while not started.is_set() and not first_task.done():
            await asyncio.sleep(0.01)
        second_task = asyncio.create_task(handlers["on_review_text"](second))
        await asyncio.sleep(0.2)  # дать второму хендлеру шанс (ошибочно) начать свой анализ
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=30)

    asyncio.run(scenario())

    assert len(calls) == 1
    assert not first.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)
    assert second.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)


def test_report_runs_off_event_loop(tmp_db, monkeypatch, make_bot_handlers, fake_message) -> None:
    seen_threads: list[int] = []
    real_snapshot = bot_handlers_module.build_analytics_snapshot

    def recording_snapshot(*args, **kwargs):
        seen_threads.append(threading.get_ident())
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(bot_handlers_module, "build_analytics_snapshot", recording_snapshot)
    handlers = make_bot_handlers(tmp_db)
    report_msg = fake_message("/report")

    async def scenario() -> int:
        await handlers["cmd_report"](report_msg)
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())

    assert seen_threads and seen_threads[0] != loop_thread
    assert report_msg.answers and report_msg.answers[0].startswith("Сводка по отзывам")
