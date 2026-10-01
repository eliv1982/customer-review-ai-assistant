"""
Хендлеры aiogram: команды и приём отзывов через process_review.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot import messages as msg
from config import Settings
from services.localization_service import format_user_review_analysis_message
from services.report_service import (
    TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES,
    build_analytics_snapshot,
    format_report_ru,
)
from services.review_pipeline import (
    ProcessedReviewResult,
    load_processed_result_from_database,
    process_review,
)
from services.review_service import find_recent_duplicate_review

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Лимит длины одного сообщения Telegram (запас ниже 4096).
_TELEGRAM_CHUNK = 3800


def _kb_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "knowledge_base.csv"


def _chunk_text(text: str, max_len: int = _TELEGRAM_CHUNK) -> list[str]:
    if not text:
        return [""]
    parts: list[str] = []
    rest = text
    while rest:
        parts.append(rest[:max_len])
        rest = rest[max_len:]
    return parts


# Синхронные границы сервисного слоя (sqlite3 + OpenAI). Хендлеры вызывают их только через
# ``asyncio.to_thread``, чтобы не блокировать event loop. Соединения SQLite открываются и
# закрываются внутри вызовов ``review_service`` / ``report_service`` — т.е. целиком в рабочем
# потоке; общих соединений между потоками нет.


async def _to_thread_until_done(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """
    ``asyncio.to_thread``, который при отмене ожидающей корутины дожидается рабочего потока.

    Отмена корутины, ждущей ``to_thread``, поток не останавливает: синхронный вызов
    (OpenAI + запись в SQLite) продолжает работать. Поэтому воркер держится отдельной задачей,
    а отмена хендлера лишь откладывается до фактического завершения воркера — пока вызывающий
    держит блокировку пользователя, второй запрос не сможет начать параллельную обработку.
    ``asyncio.wait`` не отменяет воркера при отмене ожидающего и не поднимает его исключение.
    """
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is None:
        return worker.result()
    if not worker.cancelled() and (worker_exc := worker.exception()) is not None:
        logger.warning(
            "Telegram: воркер завершился с ошибкой после отмены хендлера",
            exc_info=worker_exc,
        )
    raise cancelled


def _build_report_text(db_path: str) -> str:
    snapshot = build_analytics_snapshot(
        db_path,
        exclude_source_prefixes=TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES,
        omit_unnamed_products=True,
        product_limit=12,
        problem_topics_limit=4,
    )
    return format_report_ru(snapshot, compact=True)


def _load_or_process_review(
    db_path: str,
    kb_arg: str | None,
    *,
    api_key: str,
    model: str,
    source: str,
    review_text: str,
    customer_name: str | None,
    user_id: int,
) -> tuple[ProcessedReviewResult, bool]:
    """Дубликат за 24 ч. берётся из БД без OpenAI, иначе полный ``process_review``."""
    dup_id = find_recent_duplicate_review(
        db_path,
        source=source,
        review_text=review_text,
        hours=24,
    )
    if dup_id is not None:
        cached = load_processed_result_from_database(
            db_path,
            dup_id,
            knowledge_base_path=kb_arg,
        )
        if cached is not None and cached.analysis is not None:
            logger.info(
                "Telegram: дубликат отзыва за 24 ч., user_id=%s -> review_id=%s (без OpenAI)",
                user_id,
                dup_id,
            )
            return cached, True
    pr = process_review(
        db_path,
        api_key=api_key,
        model=model,
        source=source,
        review_text=review_text,
        customer_name=customer_name,
        knowledge_base_path=kb_arg,
    )
    return pr, False


def setup_handlers(router: Router, settings: Settings) -> None:
    """Регистрирует обработчики с доступом к настройкам (БД, ключи)."""
    db_path = settings.database_path
    kb_file = _kb_path()
    kb_arg: str | None = str(kb_file) if kb_file.is_file() else None
    # Отзывы одного пользователя обрабатываются по очереди: поиск дубликата и запись анализа
    # не атомарны, а без блокировки event loop повторная отправка того же текста, пока идёт
    # анализ, не нашла бы дубликат и создала бы вторую запись и второй вызов OpenAI.
    # Запись в словаре живёт, пока блокировку кто-то держит или ждёт.
    review_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        await message.answer(msg.START_TEXT)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(msg.HELP_TEXT)

    @router.message(Command("new_review"))
    async def cmd_new_review(message: Message) -> None:
        await message.answer(msg.NEW_REVIEW_PROMPT)

    @router.message(Command("report"))
    async def cmd_report(message: Message) -> None:
        try:
            report = await asyncio.to_thread(_build_report_text, db_path)
        except Exception:
            logger.exception("Telegram /report: ошибка report_service")
            await message.answer("Не удалось построить отчёт по базе.")
            return
        for part in _chunk_text(report):
            await message.answer(part)

    @router.message(F.text)
    async def on_review_text(message: Message) -> None:
        raw = (message.text or "").strip()
        if not raw:
            await message.answer(msg.ERROR_EMPTY_TEXT)
            return
        if raw.startswith("/"):
            await message.answer(msg.UNKNOWN_COMMAND)
            return

        api_key = settings.openai_api_key
        if not api_key or not str(api_key).strip():
            await message.answer(msg.ERROR_NO_OPENAI)
            return

        user = message.from_user
        uid = user.id if user else 0
        display = (user.full_name or "").strip() if user else ""
        if not display and user and user.username:
            display = f"@{user.username}"
        customer = display or None
        source = f"telegram:{uid}"

        logger.info(
            "Telegram: принят отзыв user_id=%s source=%s len=%s",
            uid,
            source,
            len(raw),
        )

        lock = review_locks.get(source)
        if lock is None:
            lock = asyncio.Lock()
            review_locks[source] = lock
        try:
            async with lock:
                pr, from_cache = await _to_thread_until_done(
                    _load_or_process_review,
                    db_path,
                    kb_arg,
                    api_key=api_key,
                    model=settings.openai_model,
                    source=source,
                    review_text=raw,
                    customer_name=customer,
                    user_id=uid,
                )
        except Exception:
            logger.exception("Telegram: сбой process_review для user_id=%s", uid)
            await message.answer(msg.ERROR_PIPELINE)
            return

        answer = format_user_review_analysis_message(
            pr,
            error_text_if_no_analysis=msg.ERROR_PIPELINE,
        )
        if from_cache:
            answer = f"{msg.DUPLICATE_TELEGRAM_REVIEW_CACHED}\n\n{answer}"
        for part in _chunk_text(answer):
            await message.answer(part)
