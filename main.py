"""
Точка входа: SQLite, опциональные smoke test, Telegram-бот (long polling).

При наличии TELEGRAM_BOT_TOKEN после инициализации БД запускается aiogram polling.
Smoke test — только при RUN_SMOKE_TESTS=true (или 1, yes, on).
"""
from __future__ import annotations

import asyncio
import logging
import os
import pprint
import sys
from pathlib import Path

from bot.runner import run_bot_polling
from config import get_settings
from services.review_service import init_database


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def configure_logging(level_name: str) -> None:
    """Настраивает логирование процесса в стандартный вывод (stdout)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    level = getattr(logging, level_name.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _run_smoke_tests() -> None:
    """Локальные проверки CSV, отчёта, KB и pipeline (без Telegram)."""
    from services.ai_service import StructuredReviewAnalysis
    from services.csv_service import import_reviews_from_csv
    from services.knowledge_base_service import find_matches_for_analysis, load_knowledge_base
    from services.localization_service import (
        review_type_label_ru,
        sentiment_label_ru,
        topic_label_ru,
    )
    from services.report_service import build_analytics_snapshot, format_report_ru
    from services.review_pipeline import process_review

    settings = get_settings()
    db_path = settings.database_path
    sample_csv = Path(__file__).resolve().parent / "samples" / "sample_reviews.csv"
    process_ai = bool(settings.openai_api_key and str(settings.openai_api_key).strip())

    print("\n--- SMOKE: импорт CSV ---")
    print(f"Файл: {sample_csv}")
    print(f"process_with_ai: {process_ai}")
    stats = import_reviews_from_csv(
        db_path,
        sample_csv,
        process_with_ai=process_ai,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
    )
    pprint.pprint(stats)
    print("--- конец SMOKE CSV ---\n")

    print("--- SMOKE: аналитика ---")
    print(format_report_ru(build_analytics_snapshot(db_path)))
    print("--- конец SMOKE аналитики ---\n")

    root = Path(__file__).resolve().parent
    kb_csv = root / "data" / "knowledge_base.csv"
    print("--- SMOKE: knowledge base ---")
    kb_rows = load_knowledge_base(kb_csv)
    demo = StructuredReviewAnalysis(
        sentiment="negative",
        topic="delivery",
        summary="Демо.",
        reply_draft="(демо)",
    )
    for i, row in enumerate(find_matches_for_analysis(kb_rows, demo, limit=3), 1):
        print(
            f"  {i}. {sentiment_label_ru(row.sentiment)} | "
            f"{review_type_label_ru(row.review_type)} | {topic_label_ru(row.topic)}"
        )
    print("--- конец SMOKE KB ---\n")

    if process_ai:
        pr = process_review(
            db_path,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            source="smoke_kb",
            review_text="Заказ ехал долго, курьер не позвонил.",
            customer_name="Демо Клиент",
            product_name="Кофеварка Z (smoke)",
            rating=2,
            knowledge_base_path=kb_csv,
        )
        print("--- SMOKE: process_review ---")
        print(pr.status, pr.review_id, len(pr.kb_matches))
        print("--- конец SMOKE pipeline ---\n")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger(__name__)
    log.info("Сервис запущен: инициализация SQLite.")

    db_path = settings.database_path
    init_database(db_path)
    log.info("База данных готова: %s", db_path)

    if _env_truthy("RUN_SMOKE_TESTS"):
        log.info("RUN_SMOKE_TESTS включён — выполняю smoke test.")
        _run_smoke_tests()
    else:
        log.info("Smoke test пропущен (задайте RUN_SMOKE_TESTS=true для локальной проверки).")

    token = (settings.telegram_bot_token or "").strip()
    if not token:
        log.warning(
            "TELEGRAM_BOT_TOKEN не задан — бот не запускается. "
            "Добавьте токен в .env для long polling."
        )
        return

    log.info("Запуск Telegram-бота (aiogram, long polling).")
    asyncio.run(run_bot_polling(settings))


if __name__ == "__main__":
    main()
