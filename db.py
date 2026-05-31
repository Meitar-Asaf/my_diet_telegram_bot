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
