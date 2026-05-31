from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, abort, request
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import telebot
from telebot.types import Message, Update


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("nutrition_bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "UTC")
BOT_MODE = os.getenv("BOT_MODE", "webhook" if os.getenv("RENDER") else "polling")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

missing_env_vars = []
if not TELEGRAM_BOT_TOKEN:
    missing_env_vars.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    missing_env_vars.append("GEMINI_API_KEY")

# Accept either DATABASE_URL or split DB_* settings.
if not DATABASE_URL:
    split_db_vars = {
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
    }
    missing_env_vars.extend(
        [name for name, value in split_db_vars.items() if not value]
    )

if missing_env_vars:
    raise RuntimeError(
        "Missing required environment variables: " + ", ".join(missing_env_vars)
    )

try:
    LOCAL_TIMEZONE = ZoneInfo(APP_TIMEZONE)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Invalid APP_TIMEZONE: {APP_TIMEZONE}") from exc

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)
GEMINI_SYSTEM_PROMPT = (
    "You are a nutrition estimation engine. Analyze the user's food text or image and "
    "estimate the total calories and protein for the described meal. Return only valid "
    'JSON matching this schema exactly: {"calories": int, "protein": int}. '
    "Always use integers. Never include explanations, markdown, or extra keys. If the "
    "meal is unclear, make the best reasonable estimate from the available information."
)

WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
webhook_lock = threading.Lock()
webhook_initialized = False


def current_local_date() -> datetime.date:
    """Return the current date in the configured application timezone."""
    return datetime.now(LOCAL_TIMEZONE).date()


def calorie_limit_for(day: datetime.date) -> int:
    """Return the daily calorie limit, using Saturday as the cheat day."""
    return 2550 if day.weekday() == 5 else 1500


def protein_goal() -> int:
    """Return the daily protein target in grams."""
    return 100


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from raw model output text."""
    stripped = raw_text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)

    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"Gemini response did not include JSON: {raw_text}")

    return json.loads(match.group(0))


def call_gemini_for_food(
    *,
    food_text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime_type: str | None = None,
) -> dict[str, int]:
    """Call Gemini Flash to estimate calories and protein from text or image input."""
    if not food_text and not image_bytes:
        raise ValueError("Either food_text or image_bytes must be provided.")

    prompt_text = (
        "Estimate nutrition for this entry and return JSON only."
        if not food_text
        else f"Estimate nutrition for this entry and return JSON only: {food_text}"
    )

    user_parts: list[dict[str, Any]] = [{"text": prompt_text}]
    if image_bytes:
        mime_type = image_mime_type or "image/jpeg"
        user_parts.append(
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                }
            }
        )

    payload = {
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    response = requests.post(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()

    response_payload = response.json()
    candidates = response_payload.get("candidates") or []
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {response_payload}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_fragments = [part.get("text", "") for part in parts if "text" in part]
    if not text_fragments:
        raise ValueError(f"Gemini returned no text response: {response_payload}")

    parsed = extract_json_object("\n".join(text_fragments))
    calories = max(0, int(parsed["calories"]))
    protein = max(0, int(parsed["protein"]))
    return {"calories": calories, "protein": protein}


def create_db_connection() -> psycopg2.extensions.connection:
    """Create a PostgreSQL connection from DATABASE_URL or split DB_* settings."""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)

    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=DB_SSLMODE,
    )


def get_daily_nutrition(user_id: int, entry_date: datetime.date) -> dict[str, Any] | None:
    """Fetch the user's nutrition totals for a specific date from PostgreSQL."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT user_id, date, total_calories, total_protein
                FROM daily_nutrition
                WHERE user_id = %s AND date = %s
                LIMIT 1
                """,
                (user_id, entry_date),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_daily_nutrition(
    user_id: int,
    entry_date: datetime.date,
    total_calories: int,
    total_protein: int,
) -> dict[str, Any]:
    """Insert or update the daily nutrition totals for a user and date."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO daily_nutrition (user_id, date, total_calories, total_protein)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, date)
                DO UPDATE SET
                    total_calories = EXCLUDED.total_calories,
                    total_protein = EXCLUDED.total_protein
                RETURNING user_id, date, total_calories, total_protein
                """,
                (user_id, entry_date, total_calories, total_protein),
            )
            return dict(cur.fetchone())


def add_food_to_daily_totals(
    user_id: int,
    *,
    calories: int,
    protein: int,
    entry_date: datetime.date,
) -> dict[str, Any]:
    """Add one meal estimate to the existing daily totals and persist the result."""
    existing = get_daily_nutrition(user_id, entry_date) or {
        "total_calories": 0,
        "total_protein": 0,
    }
    new_total_calories = int(existing["total_calories"]) + calories
    new_total_protein = int(existing["total_protein"]) + protein
    return upsert_daily_nutrition(
        user_id=user_id,
        entry_date=entry_date,
        total_calories=new_total_calories,
        total_protein=new_total_protein,
    )


def format_daily_summary(record: dict[str, Any], entry_date: datetime.date) -> str:
    """Format a readable daily summary with limits, goals, and remaining amounts."""
    total_calories = int(record["total_calories"])
    total_protein = int(record["total_protein"])
    calorie_limit = calorie_limit_for(entry_date)
    remaining_calories = calorie_limit - total_calories
    remaining_protein = protein_goal() - total_protein
    day_type = "Cheat day" if calorie_limit == 2550 else "Regular day"

    return (
        f"{day_type} totals for {entry_date.isoformat()}\n"
        f"Calories: {total_calories}/{calorie_limit} ({remaining_calories:+d} remaining)\n"
        f"Protein: {total_protein}/{protein_goal()}g ({remaining_protein:+d}g remaining)"
    )


def build_analysis_reply(
    analysis: dict[str, int],
    updated_record: dict[str, Any],
    entry_date: datetime.date,
) -> str:
    """Build the Telegram reply after adding a meal estimate to daily totals."""
    return (
        f"Added meal estimate:\n"
        f"Calories: {analysis['calories']}\n"
        f"Protein: {analysis['protein']}g\n\n"
        f"{format_daily_summary(updated_record, entry_date)}"
    )


def handle_food_entry(message: Message, *, text: str | None, image_bytes: bytes | None) -> None:
    """Analyze one food entry and store its nutrition impact for the current day."""
    LOGGER.info(
        "Handling food entry user_id=%s has_text=%s has_image=%s",
        message.from_user.id,
        bool(text),
        bool(image_bytes),
    )
    entry_date = current_local_date()
    mime_type = None
    if image_bytes:
        mime_type = "image/jpeg"

    analysis = call_gemini_for_food(
        food_text=text,
        image_bytes=image_bytes,
        image_mime_type=mime_type,
    )
    updated_record = add_food_to_daily_totals(
        message.from_user.id,
        calories=analysis["calories"],
        protein=analysis["protein"],
        entry_date=entry_date,
    )
    bot.reply_to(message, build_analysis_reply(analysis, updated_record, entry_date))


@bot.message_handler(commands=["start", "help"])
def send_welcome(message: Message) -> None:
    """Send help text for first-time users and command guidance."""
    LOGGER.info("Handling /start or /help for user_id=%s", message.from_user.id)
    bot.reply_to(
        message,
        "Send a food description or a meal photo. I will estimate calories and protein, "
        "save the totals for today, and track them against your daily goals.",
    )


@bot.message_handler(commands=["today"])
def show_today_totals(message: Message) -> None:
    """Show today's accumulated calories and protein for the current user."""
    LOGGER.info("Handling /today for user_id=%s", message.from_user.id)
    entry_date = current_local_date()
    record = get_daily_nutrition(message.from_user.id, entry_date) or {
        "total_calories": 0,
        "total_protein": 0,
    }
    bot.reply_to(message, format_daily_summary(record, entry_date))


@bot.message_handler(content_types=["photo"])
def handle_photo(message: Message) -> None:
    """Handle photo messages by estimating nutrition from image and caption."""
    LOGGER.info("Handling photo message for user_id=%s", message.from_user.id)
    try:
        largest_photo = message.photo[-1]
        file_info = bot.get_file(largest_photo.file_id)
        downloaded_bytes = bot.download_file(file_info.file_path)
        caption = message.caption.strip() if message.caption else None

        guessed_mime_type, _ = mimetypes.guess_type(file_info.file_path)
        image_mime_type = guessed_mime_type or "image/jpeg"
        analysis = call_gemini_for_food(
            food_text=caption,
            image_bytes=downloaded_bytes,
            image_mime_type=image_mime_type,
        )
        entry_date = current_local_date()
        updated_record = add_food_to_daily_totals(
            message.from_user.id,
            calories=analysis["calories"],
            protein=analysis["protein"],
            entry_date=entry_date,
        )
        bot.reply_to(message, build_analysis_reply(analysis, updated_record, entry_date))
    except Exception:
        LOGGER.exception("Failed to process photo message")
        bot.reply_to(
            message,
            "I could not analyze that photo right now. Please try again with a clearer image or add a caption.",
        )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_text(message: Message) -> None:
    """Handle plain text meal descriptions and ignore unknown slash commands."""
    LOGGER.info("Handling text message for user_id=%s", message.from_user.id)
    if message.text.startswith("/"):
        LOGGER.info("Ignoring unknown command text=%s", message.text)
        return

    try:
        handle_food_entry(message, text=message.text.strip(), image_bytes=None)
    except Exception:
        LOGGER.exception("Failed to process text message")
        bot.reply_to(
            message,
            "I could not analyze that meal description right now. Please try again with a more specific description.",
        )


def webhook_url() -> str:
    """Build the full Telegram webhook URL from the configured base URL."""
    if not WEBHOOK_BASE_URL:
        raise RuntimeError("WEBHOOK_BASE_URL is required when BOT_MODE=webhook.")

    # Allow either a plain service URL or a URL that already contains the token path.
    if WEBHOOK_BASE_URL.endswith(WEBHOOK_PATH):
        return WEBHOOK_BASE_URL

    return f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"


def ensure_webhook() -> None:
    """Register Telegram webhook once per process in webhook mode."""
    global webhook_initialized

    if BOT_MODE != "webhook" or webhook_initialized:
        return

    with webhook_lock:
        if webhook_initialized:
            return

        target_webhook_url = webhook_url()
        bot.remove_webhook()
        if not bot.set_webhook(url=target_webhook_url, allowed_updates=["message"]):
            raise RuntimeError("Failed to register Telegram webhook.")

        webhook_initialized = True
        LOGGER.info("Webhook registered at %s", target_webhook_url)


@app.before_request
def initialize_webhook_before_requests() -> None:
    """Ensure webhook is initialized before serving incoming HTTP requests."""
    if BOT_MODE == "webhook":
        ensure_webhook()


@app.get("/healthz")
def healthcheck() -> tuple[dict[str, str], int]:
    """Return a simple health response for Render and uptime checks."""
    return {"status": "ok", "mode": BOT_MODE}, 200


@app.post(WEBHOOK_PATH)
def telegram_webhook() -> tuple[str, int]:
    """Receive Telegram updates and forward them into the bot dispatcher."""
    if not request.is_json:
        abort(403)

    payload = request.get_json(silent=True)
    if not payload:
        abort(400)

    try:
        update = Update.de_json(payload)
        LOGGER.info("Incoming Telegram update_id=%s", getattr(update, "update_id", None))
        bot.process_new_updates([update])
    except Exception:
        LOGGER.exception("Failed to process incoming Telegram update")
        raise

    return "ok", 200


def main() -> None:
    """Run the bot in webhook mode for production or polling mode for local use."""
    LOGGER.info("Starting nutrition bot in %s mode", BOT_MODE)

    if BOT_MODE == "webhook":
        ensure_webhook()
        app.run(host="0.0.0.0", port=PORT)
        return

    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if BOT_MODE == "webhook":
    ensure_webhook()


if __name__ == "__main__":
    main()