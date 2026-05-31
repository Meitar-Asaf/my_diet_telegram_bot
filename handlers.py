from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import threading
from typing import Any

import telebot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db import (
    add_food_entry,
    delete_food_entry,
    get_daily_nutrition,
    get_today_food_log,
    undo_last_food_entry,
)
from gemini import GeminiRateLimitError, call_gemini_for_food
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

# Pending food confirmations: user_id → pending data dict
_pending_food: dict[int, dict[str, Any]] = {}
_pending_lock = threading.Lock()


def instance_fingerprint() -> str:
    """Return the first 8 hex chars of SHA-256 of the bot token (safe for logs)."""
    return hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:8]


def _lang_from_user(user: Any) -> str:
    lc = (getattr(user, "language_code", "") or "").lower()
    return "he" if lc.startswith("he") else "en"


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


def show_food_list(message: Message) -> None:
    LOGGER.info("Handling /list for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.text)
    entry_date = current_local_date()
    entries = get_today_food_log(message.from_user.id, entry_date)
    if not entries:
        bot.reply_to(message, message_text(lang, "list_empty"))
        return

    lines = [message_text(lang, "list_header"), ""]
    markup = InlineKeyboardMarkup()
    for i, entry in enumerate(entries, start=1):
        desc = entry["description"][:40]
        lines.append(f"{i}. {desc} — {entry['calories']} קל, {entry['protein']} גר")
        markup.add(InlineKeyboardButton(
            f"🗑 {i}. {entry['description'][:25]}",
            callback_data=f"del_ask:{entry['id']}",
        ))
    bot.reply_to(message, "\n".join(lines), reply_markup=markup)


def handle_undo(message: Message) -> None:
    LOGGER.info("Handling /undo for user_id=%s", message.from_user.id)
    lang = detect_message_language(message, message.text)
    entry_date = current_local_date()
    deleted = undo_last_food_entry(message.from_user.id, entry_date)
    if not deleted:
        bot.reply_to(message, message_text(lang, "undo_nothing"))
        return
    bot.reply_to(message, message_text(lang, "undo_success").format(
        description=deleted["description"],
        calories=deleted["calories"],
        protein=deleted["protein"],
    ))


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
    """Call Groq, then show confirmation keyboard if food detected."""
    analysis = call_gemini_for_food(
        food_text=text,
        image_bytes=image_bytes,
        image_mime_type=image_mime_type,
    )

    if not analysis["is_food"]:
        bot.reply_to(message, analysis["chat_reply"] or message_text(lang, "non_food_default"))
        return

    user_id = message.from_user.id
    description = text or message_text(lang, "photo_desc")

    with _pending_lock:
        _pending_food[user_id] = {
            "description": description,
            "calories": analysis["calories"],
            "protein": analysis["protein"],
            "entry_date": current_local_date(),
            "lang": lang,
        }

    confirm_text = message_text(lang, "confirm_food").format(
        description=description[:50],
        calories=analysis["calories"],
        protein=analysis["protein"],
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(message_text(lang, "btn_yes"), callback_data="food_yes"),
        InlineKeyboardButton(message_text(lang, "btn_no"), callback_data="food_no"),
    )
    bot.reply_to(message, confirm_text, reply_markup=markup)


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
        LOGGER.warning("AI rate-limited for user_id=%s", message.from_user.id)
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
        LOGGER.warning("AI rate-limited for photo user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "gemini_busy"))
    except Exception:
        LOGGER.exception("Failed to process photo message for user_id=%s", message.from_user.id)
        bot.reply_to(message, message_text(lang, "photo_error"))


# ---------------------------------------------------------------------------
# Callback query handler (inline keyboard buttons)
# ---------------------------------------------------------------------------

def handle_callback_query(call: CallbackQuery) -> None:
    """Route inline keyboard callbacks."""
    user_id = call.from_user.id
    data = call.data or ""
    lang = _lang_from_user(call.from_user)
    LOGGER.info("Callback user_id=%s data=%s", user_id, data)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if data == "food_yes":
        with _pending_lock:
            pending = _pending_food.pop(user_id, None)
        if not pending:
            bot.edit_message_text(message_text(lang, "no_pending"), chat_id=chat_id, message_id=message_id)
            return
        entry_lang = pending.get("lang", lang)
        add_food_entry(
            user_id,
            description=pending["description"],
            calories=pending["calories"],
            protein=pending["protein"],
            entry_date=pending["entry_date"],
        )
        record = get_daily_nutrition(user_id, pending["entry_date"]) or {
            "total_calories": pending["calories"],
            "total_protein": pending["protein"],
        }
        reply = build_analysis_reply(
            {"calories": pending["calories"], "protein": pending["protein"]},
            record,
            pending["entry_date"],
            entry_lang,
        )
        bot.edit_message_text(reply, chat_id=chat_id, message_id=message_id)

    elif data == "food_no":
        with _pending_lock:
            _pending_food.pop(user_id, None)
        bot.edit_message_text(message_text(lang, "food_cancelled"), chat_id=chat_id, message_id=message_id)

    elif data.startswith("del_ask:"):
        entry_id = int(data.split(":")[1])
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton(message_text(lang, "btn_yes"), callback_data=f"del_yes:{entry_id}"),
            InlineKeyboardButton(message_text(lang, "btn_no"), callback_data="del_no"),
        )
        bot.send_message(chat_id, message_text(lang, "delete_confirm"), reply_markup=markup)

    elif data.startswith("del_yes:"):
        entry_id = int(data.split(":")[1])
        deleted = delete_food_entry(entry_id, user_id)
        if deleted:
            reply = f"{message_text(lang, 'deleted')}: {deleted['description']} ({deleted['calories']} קל, {deleted['protein']} גר)"
        else:
            reply = message_text(lang, "deleted")
        bot.edit_message_text(reply, chat_id=chat_id, message_id=message_id)

    elif data == "del_no":
        bot.edit_message_text(message_text(lang, "del_cancelled"), chat_id=chat_id, message_id=message_id)

