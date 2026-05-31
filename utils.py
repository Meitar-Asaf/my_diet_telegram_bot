from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telebot.types import Message

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "UTC")
HEBREW_CHAR_PATTERN = re.compile(r"[\u0590-\u05FF]")

try:
    LOCAL_TIMEZONE = ZoneInfo(APP_TIMEZONE)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Invalid APP_TIMEZONE: {APP_TIMEZONE}") from exc


def current_local_date() -> datetime.date:
    """Return the current date in the configured application timezone."""
    return datetime.now(LOCAL_TIMEZONE).date()


def calorie_limit_for(day: datetime.date) -> int:
    """Return the daily calorie limit: 2550 on Saturday (cheat day), 1500 otherwise."""
    return 2550 if day.weekday() == 5 else 1500


def protein_goal() -> int:
    """Return the daily protein target in grams."""
    return 100


def detect_language_from_text(text: str) -> str:
    """Return 'he' if text contains Hebrew characters, otherwise 'en'."""
    return "he" if HEBREW_CHAR_PATTERN.search(text or "") else "en"


def detect_message_language(message: Message, text_hint: str | None = None) -> str:
    """Detect preferred reply language from message text or Telegram language code."""
    if text_hint and text_hint.strip():
        return detect_language_from_text(text_hint)
    language_code = (getattr(message.from_user, "language_code", "") or "").lower()
    return "he" if language_code.startswith("he") else "en"


_MESSAGES: dict[str, dict[str, str]] = {
    "he": {
        "welcome": "שלחי תיאור ארוחה או תמונת אוכל. אני אעריך קלוריות וחלבון, אשמור סיכום יומי ואעקוב אחרי היעדים שלך.",
        "small_talk": "בשמחה. כדי לעדכן יומן תזונה, שלחי תיאור אוכל (למשל: שתי פרוסות לחם לבן וגבינה) או תמונה של הארוחה.",
        "photo_error": "לא הצלחתי לנתח את התמונה כרגע. נסי שוב עם תמונה ברורה יותר או הוסיפי כיתוב.",
        "text_error": "לא הצלחתי לנתח את תיאור הארוחה כרגע. נסי שוב עם פירוט קצת יותר ברור.",
        "gemini_busy": "יש כרגע עומס זמני בניתוח AI. נסי שוב בעוד כמה שניות.",
        "non_food_default": "קיבלתי. אם תרצי לעדכן תזונה, שלחי תיאור אוכל או תמונה של הארוחה.",
        "added_header": "נוספה הערכת ארוחה:",
        "calories_label": "קלוריות",
        "protein_label": "חלבון",
        "regular_day": "יום רגיל",
        "cheat_day": "יום צ'יט",
        "totals_for": "סיכום יומי לתאריך",
        "remaining": "נותר",
        "photo_desc": "תמונת אוכל",
        "confirm_food": "ניתחתי:\n🍽 {description}\n🔥 {calories} קל | 💪 {protein} גר\nלאשר?",
        "btn_yes": "✓ כן",
        "btn_no": "✗ לא",
        "food_confirmed": "✓ נשמר",
        "food_cancelled": "ביטול",
        "no_pending": "פג תוקף האישור — שלחי שוב.",
        "undo_success": "בוטל: {description} ({calories} קל, {protein} גר)",
        "undo_nothing": "אין מה לבטל להיום.",
        "list_empty": "אין רישומים להיום.",
        "list_header": "ארוחות להיום:",
        "delete_confirm": "למחוק?",
        "deleted": "✓ נמחק",
        "del_cancelled": "ביטול",
    },
    "en": {
        "welcome": "Send a food description or a meal photo. I will estimate calories and protein, save today's totals, and track your goals.",
        "small_talk": "Happy to help. To log nutrition, send a food description (for example: two slices of white bread) or a meal photo.",
        "photo_error": "I could not analyze that photo right now. Please try again with a clearer image or add a caption.",
        "text_error": "I could not analyze that meal description right now. Please try again with a more specific description.",
        "gemini_busy": "The AI analyzer is temporarily busy. Please try again in a few seconds.",
        "non_food_default": "Got it. If you want to log nutrition, send a food description or a meal photo.",
        "added_header": "Added meal estimate:",
        "calories_label": "Calories",
        "protein_label": "Protein",
        "regular_day": "Regular day",
        "cheat_day": "Cheat day",
        "totals_for": "Daily totals for",
        "remaining": "remaining",
        "photo_desc": "Food photo",
        "confirm_food": "Analyzed:\n🍽 {description}\n🔥 {calories} cal | 💪 {protein}g protein\nConfirm?",
        "btn_yes": "✓ Yes",
        "btn_no": "✗ No",
        "food_confirmed": "✓ Saved",
        "food_cancelled": "Cancelled",
        "no_pending": "Confirmation expired — please send again.",
        "undo_success": "Removed: {description} ({calories} cal, {protein}g)",
        "undo_nothing": "Nothing to undo for today.",
        "list_empty": "No entries for today.",
        "list_header": "Today's meals:",
        "delete_confirm": "Delete?",
        "deleted": "✓ Deleted",
        "del_cancelled": "Cancelled",
    },
}


def message_text(lang: str, key: str) -> str:
    """Return a localized UI string for the given language and key."""
    return _MESSAGES[lang][key]


def format_daily_summary(record: dict[str, Any], entry_date: datetime.date, lang: str) -> str:
    """Format a readable daily summary with calorie and protein limits."""
    total_calories = int(record["total_calories"])
    total_protein = int(record["total_protein"])
    calorie_limit = calorie_limit_for(entry_date)
    remaining_calories = calorie_limit - total_calories
    remaining_protein = protein_goal() - total_protein
    day_type = (
        message_text(lang, "cheat_day") if calorie_limit == 2550 else message_text(lang, "regular_day")
    )

    if lang == "he":
        return (
            f"{day_type} - {message_text(lang, 'totals_for')} {entry_date.isoformat()}\n"
            f"{message_text(lang, 'calories_label')}: {total_calories}/{calorie_limit} ({remaining_calories:+d} {message_text(lang, 'remaining')})\n"
            f"{message_text(lang, 'protein_label')}: {total_protein}/{protein_goal()} גרם ({remaining_protein:+d} גרם {message_text(lang, 'remaining')})"
        )

    return (
        f"{day_type} {message_text(lang, 'totals_for')} {entry_date.isoformat()}\n"
        f"{message_text(lang, 'calories_label')}: {total_calories}/{calorie_limit} ({remaining_calories:+d} {message_text(lang, 'remaining')})\n"
        f"{message_text(lang, 'protein_label')}: {total_protein}/{protein_goal()}g ({remaining_protein:+d}g {message_text(lang, 'remaining')})"
    )


def build_analysis_reply(
    analysis: dict[str, Any],
    updated_record: dict[str, Any],
    entry_date: datetime.date,
    lang: str,
) -> str:
    """Build the Telegram reply after adding a meal estimate to daily totals."""
    protein_unit = " גרם" if lang == "he" else "g"
    return (
        f"{message_text(lang, 'added_header')}\n"
        f"{message_text(lang, 'calories_label')}: {analysis['calories']}\n"
        f"{message_text(lang, 'protein_label')}: {analysis['protein']}{protein_unit}\n\n"
        f"{format_daily_summary(updated_record, entry_date, lang)}"
    )
