from __future__ import annotations

import logging
import os
import threading

# Configure logging before importing local modules so all loggers inherit this config.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from flask import Flask, abort, request
from telebot.types import Update

from handlers import bot, handle_photo, handle_text, ping, send_welcome, show_today_totals

LOGGER = logging.getLogger("nutrition_bot")

# --- Configuration ---
BOT_MODE = os.getenv("BOT_MODE", "webhook" if os.getenv("RENDER") else "polling")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_PATH = f"/{_TOKEN}"

# --- Startup validation ---
_missing: list[str] = []
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    _missing.append("TELEGRAM_BOT_TOKEN")
if not os.getenv("GROQ_API_KEY"):
    _missing.append("GROQ_API_KEY")
if not os.getenv("DATABASE_URL"):
    for _var in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.getenv(_var):
            _missing.append(_var)
if _missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(_missing))

# --- Flask app ---
app = Flask(__name__)

# --- Webhook management ---
_webhook_lock = threading.Lock()
_webhook_initialized = False


def _webhook_url() -> str:
    if not WEBHOOK_BASE_URL:
        raise RuntimeError("WEBHOOK_BASE_URL is required when BOT_MODE=webhook.")
    if WEBHOOK_BASE_URL.endswith(WEBHOOK_PATH):
        return WEBHOOK_BASE_URL
    return f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"


def ensure_webhook() -> None:
    """Register Telegram webhook once per process."""
    global _webhook_initialized
    if BOT_MODE != "webhook" or _webhook_initialized:
        return
    with _webhook_lock:
        if _webhook_initialized:
            return
        url = _webhook_url()
        bot.remove_webhook()
        if not bot.set_webhook(url=url, allowed_updates=["message"]):
            raise RuntimeError("Failed to register Telegram webhook.")
        _webhook_initialized = True
        LOGGER.info("Webhook registered at %s", url)


@app.before_request
def _init_webhook() -> None:
    if BOT_MODE == "webhook":
        ensure_webhook()


# --- Routes ---

@app.get("/healthz")
def healthcheck() -> tuple[dict[str, str], int]:
    return {"status": "ok", "mode": BOT_MODE}, 200


def _dispatch(message) -> None:
    """Route one Telegram message to the correct handler."""
    try:
        if message.content_type == "text":
            text = (message.text or "").strip()
            command = text.split()[0].split("@")[0].lower() if text else ""
            if command in {"/start", "/help"}:
                send_welcome(message)
            elif command == "/today":
                show_today_totals(message)
            elif command == "/ping":
                ping(message)
            elif text.startswith("/"):
                LOGGER.info("Ignoring unknown command: %s", text)
            else:
                handle_text(message)
        elif message.content_type == "photo":
            handle_photo(message)
        else:
            LOGGER.info("Ignoring unsupported content_type=%s", message.content_type)
    except Exception:
        LOGGER.exception("Unhandled error dispatching content_type=%s", message.content_type)


@app.post(WEBHOOK_PATH)
def telegram_webhook() -> tuple[str, int]:
    """Receive Telegram updates, return 200 immediately, process in background."""
    if not request.is_json:
        abort(403)
    raw = request.get_data(as_text=True)
    if not raw:
        abort(400)
    try:
        update = Update.de_json(raw)
        LOGGER.info("Incoming update_id=%s", getattr(update, "update_id", None))
        if update.message is not None:
            threading.Thread(target=_dispatch, args=(update.message,), daemon=True).start()
    except Exception:
        LOGGER.exception("Failed to parse Telegram update")
    return "ok", 200


# --- Entry point ---

def main() -> None:
    """Run in webhook mode (production) or polling mode (local dev)."""
    LOGGER.info("Starting nutrition bot in %s mode", BOT_MODE)
    if BOT_MODE == "webhook":
        ensure_webhook()
        app.run(host="0.0.0.0", port=PORT)
    else:
        bot.remove_webhook()
        bot.infinity_polling(timeout=60, long_polling_timeout=30)


if BOT_MODE == "webhook":
    ensure_webhook()

if __name__ == "__main__":
    main()
