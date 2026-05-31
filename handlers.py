from __future__ import annotations

import hashlib
import logging
import mimetypes
import os

import telebot
from telebot.types import Message

from db import add_food_to_daily_totals, get_daily_nutrition
from gemini import GeminiBadRequestError, GeminiRateLimitError, call_gemini_for_food
from utils import (
    build_analysis_reply,
    current_local_date,
    detect_message_language,
    format_daily_summary,
    message_text,
)

LOGGER = logging.getLogger("nutrition_bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")


def instance_fingerprint() -> str:
    """Return the first 8 hex chars of SHA-256 of the bot token (safe for logs)."""
    return hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def ping(message: Message) -> None:
    LOGGER.info("Handling /ping for user_id=%s", message.from_user.id)
    bot.reply_to(message, f"pong {instance_fingerprint()}")


def send_welcome(message: Message) -> None:
    LOGGER.info("Handling /start or /help for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.text)
    bot.reply_to(message, message_text(lang, "welcome"))


def show_today_totals(message: Message) -> None:
    LOGGER.info("Handling /today for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.text)
    entry_date = current_local_date()
    record = get_daily_nutrition(message.from_user.id, entry_date) or {
        "total_calories": 0,
        "total_protein": 0,
    }
    bot.reply_to(message, format_daily_summary(record, entry_date, lang))


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

def _analyze_and_reply(
    message: Message,
    *,
    text: str | None,
    image_bytes: bytes | None,
    image_mime_type: str | None,
    lang: str,
) -> None:
    """Call Gemini, save to DB if food, and reply to user."""
    analysis = call_gemini_for_food(
        food_text=text,
        image_bytes=image_bytes,
        image_mime_type=image_mime_type,
    )

    if not analysis["is_food"]:
        bot.reply_to(message, analysis["chat_reply"] or message_text(lang, "non_food_default"))
        return

    entry_date = current_local_date()
    updated_record = add_food_to_daily_totals(
        message.from_user.id,
        calories=analysis["calories"],
        protein=analysis["protein"],
        entry_date=entry_date,
    )
    bot.reply_to(message, build_analysis_reply(analysis, updated_record, entry_date, lang))


def handle_text(message: Message) -> None:
    LOGGER.info("Handling text message for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.text)
    try:
        _analyze_and_reply(
            message,
            text=message.text.strip(),
            image_bytes=None,
            image_mime_type=None,
            lang=lang,
        )
    except GeminiRateLimitError:
        LOGGER.warning("Gemini rate-limited for user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "gemini_busy"))
    except Exception:
        LOGGER.exception("Failed to process text message for user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "text_error"))


def handle_photo(message: Message) -> None:
    LOGGER.info("Handling photo message for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.caption)
    try:
        largest_photo = message.photo[-1]
        file_info = bot.get_file(largest_photo.file_id)
        downloaded_bytes = bot.download_file(file_info.file_path)
        caption = message.caption.strip() if message.caption else None
        guessed_mime, _ = mimetypes.guess_type(file_info.file_path)
        _analyze_and_reply(
            message,
            text=caption,
            image_bytes=downloaded_bytes,
            image_mime_type=guessed_mime or "image/jpeg",
            lang=lang,
        )
    except GeminiRateLimitError:
        LOGGER.warning("Gemini rate-limited for photo user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "gemini_busy"))
    except Exception:
        LOGGER.exception("Failed to process photo message for user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "photo_error"))
