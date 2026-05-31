from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any

import requests
from requests import Response

LOGGER = logging.getLogger("nutrition_bot")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

SYSTEM_PROMPT = (
    "You are a nutrition and intent engine for a Telegram diet bot. Analyze the user's "
    "text or image and decide whether it is a food log or regular chat. Return only valid "
    'JSON with this exact schema: {"is_food": bool, "calories": int, "protein": int, "chat_reply": string}. '
    "Rules: If the message is food logging, set is_food=true and estimate calories/protein with integers. "
    "If the message is not food logging (greeting/small-talk/unrelated), set is_food=false, calories=0, protein=0, "
    "and provide a short friendly chat_reply in the same language as the user's message. "
    "Never return markdown or extra keys."
)


class GeminiRateLimitError(Exception):
    """Raised when the AI API keeps returning 429 rate-limit responses."""


class GeminiBadRequestError(Exception):
    """Raised when the AI API rejects the request payload with HTTP 400."""


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    stripped = raw_text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"Response did not include JSON: {raw_text}")
    return json.loads(match.group(0))


def _post_with_retry(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """POST to Groq with backoff on 429, raising typed exceptions on failure."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    for attempt, delay in enumerate((1.0, 2.0, 4.0), start=1):
        response: Response = requests.post(
            GROQ_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=timeout,
        )

        if response.ok:
            return response.json()

        if response.status_code == 429:
            LOGGER.warning(
                "Groq 429 (attempt=%s) body=%s, retrying in %.1fs",
                attempt, response.text[:300], delay,
            )
            time.sleep(delay)
            continue

        if response.status_code == 400:
            LOGGER.error("Groq 400 response: %s", response.text)
            raise GeminiBadRequestError(response.text)

        response.raise_for_status()

    raise GeminiRateLimitError("Groq API is rate-limited after all retries.")


def call_gemini_for_food(
    *,
    food_text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime_type: str | None = None,
) -> dict[str, Any]:
    """Classify intent and estimate nutrition using Groq."""
    if not food_text and not image_bytes:
        raise ValueError("Either food_text or image_bytes must be provided.")

    prompt_text = (
        "Classify and analyze this entry. Return JSON only."
        if not food_text
        else f"Classify and analyze this entry. Return JSON only: {food_text}"
    )

    if image_bytes:
        model = GROQ_VISION_MODEL
        mime = image_mime_type or "image/jpeg"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_content: Any = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
        }
    else:
        model = GROQ_TEXT_MODEL
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

    response_payload = _post_with_retry(payload)

    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError(f"Groq returned no choices: {response_payload}")

    text = choices[0].get("message", {}).get("content", "")
    if not text:
        raise ValueError(f"Groq returned no content: {response_payload}")

    parsed = _extract_json_object(text)
    is_food = bool(parsed.get("is_food", True))
    calories = max(0, int(parsed.get("calories", 0)))
    protein = max(0, int(parsed.get("protein", 0)))
    chat_reply = str(parsed.get("chat_reply", "")).strip()

    return {
        "is_food": is_food,
        "calories": calories if is_food else 0,
        "protein": protein if is_food else 0,
        "chat_reply": chat_reply,
    }
