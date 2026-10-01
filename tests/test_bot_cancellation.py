"""Отмена хендлера, пока его рабочий поток ещё выполняет обработку отзыва.

Отмена корутины, ждущей ``asyncio.to_thread``, поток не останавливает. Блокировка пользователя
обязана жить до фактического завершения потока, иначе второй запрос того же пользователя
запустит параллельную обработку (второй вызов OpenAI и вторая строка в БД).

Синхронизация — только ``threading.Event`` / ``asyncio.Event``; сон по времени не используется.
Сеть и реальные ключи не нужны: подменён только ``analyze_review``, пайплайн и SQLite настоящие.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import weakref
from types import SimpleNamespace

import pytest

from bot import handlers as bot_handlers_module
from bot import messages as msg
from services import review_pipeline
from services.ai_service import StructuredReviewAnalysis
from services.review_service import list_reviews

_WAIT_SECONDS = 30

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)


class _Probe:
    """Счётчики входов в синхронную границу и в ``analyze_review`` (вызываются из потоков)."""

    def __init__(self, blocking_text: str) -> None:
        self.blocking_text = blocking_text
        self.release = threading.Event()  # отпускает заблокированный анализ
        self.mutex = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.first_analysis_started = asyncio.Event()
        self.second_pipeline_entered = asyncio.Event()
        self.pipeline_entries = 0
        self.active_pipeline = 0
        self.max_active_pipeline = 0
        self.analysis_calls = 0
        self.active_analysis = 0
        self.max_active_analysis = 0

    def _signal(self, event: asyncio.Event) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(event.set)

    def analyze(self, **kwargs) -> StructuredReviewAnalysis:
        with self.mutex:
            self.analysis_calls += 1
            self.active_analysis += 1
            self.max_active_analysis = max(self.max_active_analysis, self.active_analysis)
            first_call = self.analysis_calls == 1
        if first_call:
            self._signal(self.first_analysis_started)
        try:
            if kwargs["review_text"] == self.blocking_text:
                assert self.release.wait(timeout=_WAIT_SECONDS), "анализ не был отпущен"
            return _ANALYSIS
        finally:
            with self.mutex:
                self.active_analysis -= 1

    def wrap_pipeline(self, real):
        def boundary(*args, **kwargs):
            with self.mutex:
                self.pipeline_entries += 1
                self.active_pipeline += 1
                self.max_active_pipeline = max(self.max_active_pipeline, self.active_pipeline)
                second_entry = self.pipeline_entries == 2
            if second_entry:
                self._signal(self.second_pipeline_entered)
            try:
                return real(*args, **kwargs)
            finally:
                with self.mutex:
                    self.active_pipeline -= 1

        return boundary


@pytest.fixture
def probe_factory(monkeypatch):
    """Ставит ``_Probe`` на ``analyze_review`` и на синхронную границу хендлера."""

    def factory(blocking_text: str) -> _Probe:
        probe = _Probe(blocking_text)
        monkeypatch.setattr(review_pipeline, "analyze_review", probe.analyze)
        monkeypatch.setattr(
            bot_handlers_module,
            "_load_or_process_review",
            probe.wrap_pipeline(bot_handlers_module._load_or_process_review),
        )
        return probe

    return factory


@pytest.fixture
def observed_locks(monkeypatch):
    """``asyncio.Lock`` с сигналом «кто-то начал ждать уже занятую блокировку».

    Даёт детерминированное положительное свидетельство, что второй хендлер упёрся в блокировку,
    вместо ожидания «ничего не произошло». Живые экземпляры отслеживаются для проверки утечек.
    """
    state = SimpleNamespace(contended=asyncio.Event(), instances=weakref.WeakSet())

    class ObservedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            state.instances.add(self)

        async def acquire(self) -> bool:
            if self.locked():
                state.contended.set()
            return await super().acquire()

    monkeypatch.setattr(asyncio, "Lock", ObservedLock)
    return state


async def _wait_first(*events: asyncio.Event) -> None:
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=_WAIT_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def test_cancelled_handler_keeps_user_lock_until_worker_finishes(
    tmp_db, make_bot_handlers, fake_message, probe_factory, observed_locks
) -> None:
    text = "Курьер опоздал на два часа."
    probe = probe_factory(blocking_text=text)
    handlers = make_bot_handlers(tmp_db)
    first = fake_message(text, user_id=7)
    second = fake_message(text, user_id=7)

    async def scenario() -> SimpleNamespace:
        probe.loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(handlers["on_review_text"](first))
        await asyncio.wait_for(probe.first_analysis_started.wait(), _WAIT_SECONDS)

        # Первый воркер застрял в анализе; хендлер отменяют, и сразу приходит второй запрос.
        first_task.cancel()
        second_task = asyncio.create_task(handlers["on_review_text"](second))

        # Второй либо упирается в занятую блокировку (корректно), либо доходит до синхронной
        # границы (баг) — ждём первое из двух событий, без сна по времени.
        await _wait_first(observed_locks.contended, probe.second_pipeline_entered)
        with probe.mutex:
            while_blocked = SimpleNamespace(
                pipeline_entries=probe.pipeline_entries,
                active_pipeline=probe.active_pipeline,
                analysis_calls=probe.analysis_calls,
                first_handler_done=first_task.done(),
                second_handler_done=second_task.done(),
                second_contended=observed_locks.contended.is_set(),
            )

        probe.release.set()
        done, pending = await asyncio.wait({first_task, second_task}, timeout=_WAIT_SECONDS)
        assert not pending
        return SimpleNamespace(
            while_blocked=while_blocked,
            first_cancelled=first_task.cancelled(),
            second_exception=second_task.exception(),
        )

    outcome = asyncio.run(scenario())

    blocked = outcome.while_blocked
    # Пока воркер первого запроса жив: второй не вошёл в синхронную границу и ждёт блокировку.
    assert blocked.second_contended, "второй запрос не упёрся в блокировку первого"
    assert blocked.pipeline_entries == 1, "второй запрос вошёл в обработку при живом воркере"
    assert blocked.active_pipeline == 1
    assert blocked.analysis_calls == 1
    assert not blocked.first_handler_done, "блокировка отпущена до завершения воркера"
    assert not blocked.second_handler_done

    # После завершения воркера: отмена дошла до вызывающего, второй запрос продолжил работу.
    assert outcome.first_cancelled
    assert outcome.second_exception is None
    assert first.answers == []
    assert second.answers and second.answers[0].startswith(msg.DUPLICATE_TELEGRAM_REVIEW_CACHED)

    assert probe.max_active_pipeline == 1
    assert probe.max_active_analysis == 1
    assert probe.analysis_calls == 1, "повторный анализ OpenAI"
    assert len(list_reviews(tmp_db)) == 1, "дублирующая строка в БД"

    gc.collect()
    assert len(observed_locks.instances) == 0, "блокировка пользователя не освобождена из памяти"


def test_cancelled_user_does_not_hold_up_other_users(
    tmp_db, make_bot_handlers, fake_message, probe_factory
) -> None:
    probe = probe_factory(blocking_text="Застрявший отзыв первого пользователя.")
    handlers = make_bot_handlers(tmp_db)
    stuck = fake_message("Застрявший отзыв первого пользователя.", user_id=1)
    other = fake_message("Отзыв другого пользователя.", user_id=2)

    async def scenario() -> tuple[bool, bool]:
        probe.loop = asyncio.get_running_loop()
        stuck_task = asyncio.create_task(handlers["on_review_text"](stuck))
        await asyncio.wait_for(probe.first_analysis_started.wait(), _WAIT_SECONDS)
        stuck_task.cancel()

        # Воркер пользователя 1 всё ещё заперт, но пользователь 2 обрабатывается до конца.
        await asyncio.wait_for(handlers["on_review_text"](other), _WAIT_SECONDS)
        other_done_while_stuck = not probe.release.is_set() and probe.active_analysis == 1

        probe.release.set()
        await asyncio.wait({stuck_task}, timeout=_WAIT_SECONDS)
        return other_done_while_stuck, stuck_task.cancelled()

    other_done_while_stuck, stuck_cancelled = asyncio.run(scenario())

    assert other_done_while_stuck
    assert stuck_cancelled
    assert other.answers and msg.ERROR_PIPELINE not in other.answers[0]


def test_worker_exception_propagates_and_releases_user_lock(
    tmp_db, monkeypatch, make_bot_handlers, fake_message
) -> None:
    real = bot_handlers_module._load_or_process_review
    calls: list[int] = []

    def fail_then_work(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой воркера")
        return real(*args, **kwargs)

    monkeypatch.setattr(review_pipeline, "analyze_review", lambda **_kw: _ANALYSIS)
    monkeypatch.setattr(bot_handlers_module, "_load_or_process_review", fail_then_work)
    handlers = make_bot_handlers(tmp_db)
    first = fake_message("Первая попытка.", user_id=3)
    retry = fake_message("Вторая попытка.", user_id=3)

    async def scenario() -> None:
        await asyncio.wait_for(handlers["on_review_text"](first), _WAIT_SECONDS)
        await asyncio.wait_for(handlers["on_review_text"](retry), _WAIT_SECONDS)

    asyncio.run(scenario())

    assert first.answers == [msg.ERROR_PIPELINE]
    assert retry.answers and msg.ERROR_PIPELINE not in retry.answers[0]


def test_worker_exception_after_cancellation_is_observed(
    tmp_db, monkeypatch, make_bot_handlers, fake_message, caplog
) -> None:
    # Воркер падает, когда хендлер уже отменён: исключение не должно остаться «неполученным».
    release = threading.Event()
    started = asyncio.Event()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    unobserved: list[dict] = []

    def failing_worker(*_args, **_kwargs):
        loop_holder["loop"].call_soon_threadsafe(started.set)
        assert release.wait(timeout=_WAIT_SECONDS)
        raise RuntimeError("сбой воркера после отмены")

    monkeypatch.setattr(bot_handlers_module, "_load_or_process_review", failing_worker)
    handlers = make_bot_handlers(tmp_db)
    message = fake_message("Отзыв, воркер которого упадёт.", user_id=4)

    async def scenario() -> bool:
        loop = loop_holder["loop"] = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unobserved.append(context))
        task = asyncio.create_task(handlers["on_review_text"](message))
        await asyncio.wait_for(started.wait(), _WAIT_SECONDS)
        task.cancel()
        release.set()
        await asyncio.wait({task}, timeout=_WAIT_SECONDS)
        return task.cancelled()

    with caplog.at_level(logging.WARNING, logger=bot_handlers_module.logger.name):
        cancelled = asyncio.run(scenario())
        gc.collect()

    assert cancelled
    assert message.answers == []
    assert unobserved == [], "исключение воркера осталось неполученным"
    assert any("после отмены хендлера" in record.getMessage() for record in caplog.records)
