from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

LOGGER = logging.getLogger("nutrition_bot")

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")


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
    """Fetch the user's nutrition totals for a specific date."""
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
                ON CONFLICT (user_id, date) DO UPDATE SET
                    total_calories = EXCLUDED.total_calories,
                    total_protein  = EXCLUDED.total_protein
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
    return upsert_daily_nutrition(
        user_id=user_id,
        entry_date=entry_date,
        total_calories=int(existing["total_calories"]) + calories,
        total_protein=int(existing["total_protein"]) + protein,
    )


def add_food_entry(
    user_id: int,
    *,
    description: str,
    calories: int,
    protein: int,
    entry_date: datetime.date,
) -> dict[str, Any]:
    """Insert a food_log row and increment daily totals atomically. Returns inserted row."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO food_log (user_id, date, description, calories, protein)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, description, calories, protein
                """,
                (user_id, entry_date, description, calories, protein),
            )
            row = dict(cur.fetchone())
            cur.execute(
                """
                INSERT INTO daily_nutrition (user_id, date, total_calories, total_protein)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, date) DO UPDATE SET
                    total_calories = daily_nutrition.total_calories + EXCLUDED.total_calories,
                    total_protein  = daily_nutrition.total_protein  + EXCLUDED.total_protein
                """,
                (user_id, entry_date, calories, protein),
            )
    return row


def get_today_food_log(user_id: int, entry_date: datetime.date) -> list[dict[str, Any]]:
    """Return today's individual food entries for a user, oldest first."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, description, calories, protein
                FROM food_log
                WHERE user_id = %s AND date = %s
                ORDER BY created_at ASC
                """,
                (user_id, entry_date),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_food_entry(entry_id: int, user_id: int) -> dict[str, Any] | None:
    """Delete a food_log entry (with ownership check) and subtract from daily totals."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                DELETE FROM food_log
                WHERE id = %s AND user_id = %s
                RETURNING id, description, calories, protein, date
                """,
                (entry_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            cur.execute(
                """
                UPDATE daily_nutrition
                SET total_calories = GREATEST(0, total_calories - %s),
                    total_protein  = GREATEST(0, total_protein  - %s)
                WHERE user_id = %s AND date = %s
                """,
                (row["calories"], row["protein"], user_id, row["date"]),
            )
    return row


def undo_last_food_entry(user_id: int, entry_date: datetime.date) -> dict[str, Any] | None:
    """Delete the most recent food_log entry for today and update totals."""
    with create_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                DELETE FROM food_log
                WHERE id = (
                    SELECT id FROM food_log
                    WHERE user_id = %s AND date = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                RETURNING id, description, calories, protein, date
                """,
                (user_id, entry_date),
            )
            row = cur.fetchone()
            if not row:
                return None
            row = dict(row)
            cur.execute(
                """
                UPDATE daily_nutrition
                SET total_calories = GREATEST(0, total_calories - %s),
                    total_protein  = GREATEST(0, total_protein  - %s)
                WHERE user_id = %s AND date = %s
                """,
                (row["calories"], row["protein"], user_id, entry_date),
            )
    return row
