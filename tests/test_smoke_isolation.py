"""RUN_SMOKE_TESTS не должен менять обычную БД и, как следствие, отчёт ``/report``."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import main as app_main
from config import Settings
from services import review_pipeline
from services.ai_service import StructuredReviewAnalysis
from services.review_service import add_review, save_review_analysis

_ANALYSIS = StructuredReviewAnalysis(
    sentiment="negative",
    topic="delivery",
    summary="Доставка задержалась.",
    reply_draft="Приносим извинения.",
)


def _seed_normal_database(database_path: str) -> None:
    rid = add_review(
        database_path,
        source="telegram:42",
        review_text="Реальный отзыв пользователя.",
        customer_name="Анна",
        product_name="Чайник «Пар»",
        rating=4,
        status="processed",
    )
    save_review_analysis(
        database_path,
        review_id=rid,
        sentiment="negative",
        topic="packaging",
        summary="Кратко.",
        reply_draft="Черновик.",
    )


@pytest.mark.parametrize("with_ai", [False, True], ids=["no_openai_key", "with_openai_key"])
def test_smoke_run_does_not_alter_normal_database_or_report(
    tmp_db, tmp_path, monkeypatch, capsys, make_bot_handlers, fake_message, with_ai
) -> None:
    scratch = tmp_path / "scratch_tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    ai_calls: list[str] = []

    def fake_analyze(**kwargs):
        ai_calls.append(kwargs["review_text"])
        return _ANALYSIS

    monkeypatch.setattr(review_pipeline, "analyze_review", fake_analyze)
    settings = Settings(
        telegram_bot_token=None,
        openai_api_key="test-key-not-real" if with_ai else None,
        database_path=tmp_db,
        openai_model="test-model",
        log_level="INFO",
    )
    monkeypatch.setattr(app_main, "get_settings", lambda: settings)

    _seed_normal_database(tmp_db)
    handlers = make_bot_handlers(tmp_db)

    def report_text() -> str:
        message = fake_message("/report")
        asyncio.run(handlers["cmd_report"](message))
        return "\n".join(message.answers)

    report_before = report_text()
    db_bytes_before = Path(tmp_db).read_bytes()

    app_main._run_smoke_tests()

    out = capsys.readouterr().out
    assert "rows_imported=10" in out, "smoke должен реально выполнить импорт CSV"
    assert bool(ai_calls) is with_ai
    assert Path(tmp_db).read_bytes() == db_bytes_before
    assert report_text() == report_before
    assert list(scratch.iterdir()) == [], "временная БД smoke должна удаляться"
