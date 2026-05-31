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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)
GEMINI_SYSTEM_PROMPT = (
    "You are a nutrition and intent engine for a Telegram diet bot. Analyze the user's "
    "text or image and decide whether it is a food log or regular chat. Return only valid "
    'JSON with this exact schema: {"is_food": bool, "calories": int, "protein": int, "chat_reply": string}. '
    "Rules: If the message is food logging, set is_food=true and estimate calories/protein with integers. "
    "If the message is not food logging (greeting/small-talk/unrelated), set is_food=false, calories=0, protein=0, "
    "and provide a short friendly chat_reply in the same language as the user's message. "
    "Never return markdown or extra keys."
)


class GeminiRateLimitError(Exception):
    """Raised when Gemini keeps returning 429 rate-limit responses."""


class GeminiBadRequestError(Exception):
    """Raised when Gemini rejects the request payload with HTTP 400."""


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    stripped = raw_text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"Gemini response did not include JSON: {raw_text}")
    return json.loads(match.group(0))


def _post_with_retry(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """POST to Gemini with backoff on 429, raising typed exceptions on failure."""
    for attempt, delay in enumerate((1.0, 2.0, 4.0), start=1):
        response: Response = requests.post(
            f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )

        if response.ok:
            return response.json()

        if response.status_code == 429:
            LOGGER.warning(
                "Gemini 429 (attempt=%s) body=%s, retrying in %.1fs",
                attempt, response.text[:300], delay,
            )
            time.sleep(delay)
            continue

        if response.status_code == 400:
            LOGGER.error("Gemini 400 response: %s", response.text)
            raise GeminiBadRequestError(response.text)

        response.raise_for_status()

    raise GeminiRateLimitError("Gemini is rate-limited after all retries.")


def call_gemini_for_food(
    *,
    food_text: str | None = None,
    image_bytes: bytes | None = None,
    image_mime_type: str | None = None,
) -> dict[str, Any]:
    """Classify intent and estimate nutrition in a single Gemini call."""
    if not food_text and not image_bytes:
        raise ValueError("Either food_text or image_bytes must be provided.")

    prompt_text = (
        "Classify and analyze this entry. Return JSON only."
        if not food_text
        else f"Classify and analyze this entry. Return JSON only: {food_text}"
    )

    user_parts: list[dict[str, Any]] = [{"text": prompt_text}]
    if image_bytes:
        user_parts.append({
            "inlineData": {
                "mimeType": image_mime_type or "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            }
        })

    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }

    try:
        response_payload = _post_with_retry(payload)
    except GeminiBadRequestError:
        # Compatibility fallback: merge system prompt into user content and drop responseMimeType.
        LOGGER.warning("Gemini rejected primary payload, trying compatibility fallback")
        compat_parts: list[dict[str, Any]] = [{
            "text": (
                f"{GEMINI_SYSTEM_PROMPT}\n"
                f"User entry: {food_text or ''}\n"
                "Return JSON only."
            )
        }]
        if image_bytes:
            compat_parts.append({
                "inlineData": {
                    "mimeType": image_mime_type or "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                }
            })
        response_payload = _post_with_retry({
            "contents": [{"role": "user", "parts": compat_parts}],
            "generationConfig": {"temperature": 0.2},
        })

    candidates = response_payload.get("candidates") or []
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {response_payload}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_fragments = [p.get("text", "") for p in parts if "text" in p]
    if not text_fragments:
        raise ValueError(f"Gemini returned no text: {response_payload}")

    parsed = _extract_json_object("\n".join(text_fragments))
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
