"""
Хендлеры aiogram: команды и приём отзывов через process_review.
"""

from __future__ import annotations

import logging
from pathlib import Path

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
from services.review_pipeline import load_processed_result_from_database, process_review
from services.review_service import find_recent_duplicate_review

logger = logging.getLogger(__name__)

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


def setup_handlers(router: Router, settings: Settings) -> None:
    """Регистрирует обработчики с доступом к настройкам (БД, ключи)."""
    db_path = settings.database_path
    kb_file = _kb_path()
    kb_arg: str | None = str(kb_file) if kb_file.is_file() else None

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
            snapshot = build_analytics_snapshot(
                db_path,
                exclude_source_prefixes=TELEGRAM_REPORT_EXCLUDE_SOURCE_PREFIXES,
                omit_unnamed_products=True,
                product_limit=12,
                problem_topics_limit=4,
            )
            report = format_report_ru(snapshot, compact=True)
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

        from_cache = False
        try:
            pr = None
            dup_id = find_recent_duplicate_review(
                db_path,
                source=source,
                review_text=raw,
                hours=24,
            )
            if dup_id is not None:
                cached = load_processed_result_from_database(
                    db_path,
                    dup_id,
                    knowledge_base_path=kb_arg,
                )
                if cached is not None and cached.analysis is not None:
                    pr = cached
                    from_cache = True
                    logger.info(
                        "Telegram: дубликат отзыва за 24 ч., user_id=%s -> review_id=%s (без OpenAI)",
                        uid,
                        dup_id,
                    )
            if pr is None:
                pr = process_review(
                    db_path,
                    api_key=api_key,
                    model=settings.openai_model,
                    source=source,
                    review_text=raw,
                    customer_name=customer,
                    knowledge_base_path=kb_arg,
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
