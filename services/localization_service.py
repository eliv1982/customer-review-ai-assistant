"""
Локализация для пользовательского вывода (Telegram, текстовые отчёты).

В БД и в API OpenAI по-прежнему используются английские литералы enum.
"""
from __future__ import annotations

from typing import Any

from services.knowledge_base_service import KnowledgeBaseRow
from services.review_pipeline import ProcessedReviewResult
from services.review_service import REVIEW_DISPLAY_NAME_UNSPECIFIED

SENTIMENT_RU: dict[str, str] = {
    "positive": "позитивная",
    "neutral": "нейтральная",
    "negative": "негативная",
    "mixed": "смешанная",
}

TOPIC_RU: dict[str, str] = {
    "delivery": "доставка",
    "product_quality": "качество товара",
    "support": "поддержка",
    "price": "цена",
    "website": "сайт",
    "returns": "возвраты",
    "packaging": "упаковка",
    "other": "прочее",
}

REVIEW_TYPE_RU: dict[str, str] = {
    "complaint": "жалоба",
    "gratitude": "благодарность",
    "suggestion": "предложение",
    "mixed": "смешанный отзыв",
}

STATUS_RU: dict[str, str] = {
    "new": "новый",
    "processed": "обработан",
    "error": "ошибка",
}

# Для Telegram: не более стольких символов из шаблона KB.
_KB_TEMPLATE_MAX = 320


def _norm(code: str | None) -> str:
    return (code or "").strip()


def sentiment_label_ru(code: str | None) -> str:
    """Человекочитаемая тональность для интерфейса."""
    k = _norm(code)
    return SENTIMENT_RU.get(k, k or "—")


def topic_label_ru(code: str | None) -> str:
    """Человекочитаемая тема для интерфейса."""
    k = _norm(code)
    return TOPIC_RU.get(k, k or "—")


def review_type_label_ru(code: str | None) -> str:
    """Тип строки базы знаний / сценария для интерфейса."""
    k = _norm(code)
    return REVIEW_TYPE_RU.get(k, k or "—")


def status_label_ru(code: str | None) -> str:
    """Статус отзыва в интерфейсе."""
    k = _norm(code)
    return STATUS_RU.get(k, k or "—")


def product_display_ru(name: Any) -> str:
    """Название товара для пользователя без «сырого» NULL."""
    s = "" if name is None else str(name).strip()
    if not s:
        return REVIEW_DISPLAY_NAME_UNSPECIFIED
    return s


def rating_display_ru(rating: Any) -> str:
    """Оценка для пользователя и отчётов; при отсутствии в БД — «не указана»."""
    if rating is None:
        return "не указана"
    try:
        return str(int(rating))
    except (TypeError, ValueError):
        return "не указана"


def format_kb_match_caption(review_type: str, topic: str) -> str:
    """Подпись вида «[жалоба / доставка]» для шаблонов справочника."""
    return f"[{review_type_label_ru(review_type)} / {topic_label_ru(topic)}]"


def _polar_clash(analysis_sentiment: str, row_sentiment: str) -> bool:
    """Как в подборе KB: не смешиваем явный negative с positive-шаблоном и наоборот."""
    a = analysis_sentiment.strip()
    b = row_sentiment.strip()
    if a == "negative" and b == "positive":
        return True
    if a == "positive" and b == "negative":
        return True
    return False


def _kb_rows_for_user(
    matches: tuple[KnowledgeBaseRow, ...],
    *,
    analysis_topic: str,
    analysis_sentiment: str,
) -> list[KnowledgeBaseRow]:
    """
    До 2 шаблонов: только «сильные» (topic + sentiment как у анализа), порядок как в пайплайне.

    Если сильных нет — не более одного варианта по той же теме без полярного конфликта.
    Совпадения только по тональности с другой темой (fallback пайплайна) в чат не показываем.
    """
    rows = list(matches)
    if not rows:
        return []

    strong = [r for r in rows if r.topic == analysis_topic and r.sentiment == analysis_sentiment]
    if strong:
        return strong[:2]

    same_topic = [
        r
        for r in rows
        if r.topic == analysis_topic and not _polar_clash(analysis_sentiment, r.sentiment)
    ]
    if same_topic:
        return same_topic[:1]

    return []


def format_user_review_analysis_message(
    pr: ProcessedReviewResult,
    *,
    error_text_if_no_analysis: str,
) -> str:
    """
    Компактный ответ пользователю после ``process_review`` (Telegram).
    """
    if pr.analysis is None:
        return error_text_if_no_analysis

    a = pr.analysis
    head = (
        "Готово."
        if pr.status == "processed"
        else "Сохранено; при записи анализа возникла ошибка. Ниже текст модели."
    )
    lines = [
        head,
        f"Тональность: {sentiment_label_ru(a.sentiment)}",
        f"Тема: {topic_label_ru(a.topic)}",
        "",
        "Сводка:",
        a.summary,
        "",
        "Черновик ответа:",
        a.reply_draft,
        "",
        f"Товар: {product_display_ru(pr.review.get('product_name'))}",
        f"Оценка: {rating_display_ru(pr.review.get('rating'))}",
    ]
    kb_show = _kb_rows_for_user(
        pr.kb_matches,
        analysis_topic=a.topic,
        analysis_sentiment=a.sentiment,
    )
    if kb_show:
        lines.extend(["", "Подходящие шаблоны:"])
        for i, row in enumerate(kb_show, 1):
            short = row.reply_template.strip()
            if len(short) > _KB_TEMPLATE_MAX:
                short = short[:_KB_TEMPLATE_MAX] + "…"
            cap = format_kb_match_caption(row.review_type, row.topic)
            lines.append(f"{i}. {cap} — {short}")
    return "\n".join(lines)
