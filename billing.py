# billing.py
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────
# Per-guild subscription tier + usage tracking (SQLite)
DB_PATH = os.getenv("BILLING_DB_PATH", "data/billing.db")

DEFAULT_TIER = "free"

TIER_LIMITS = {
    "free":    {"voice": False, "custom_personalities": False, "stt_seconds": 0,       "tts_chars": 0},
    "premium": {"voice": True,  "custom_personalities": False, "stt_seconds": 60 * 60, "tts_chars": 200_000},
    "pro":     {"voice": True,  "custom_personalities": True,  "stt_seconds": 180 * 60, "tts_chars": 750_000},
}


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _connect() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_billing (
            guild_id TEXT PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'free',
            stt_seconds_used REAL NOT NULL DEFAULT 0,
            tts_chars_used INTEGER NOT NULL DEFAULT 0,
            period_start TEXT NOT NULL
        )
    """)
    return conn


def _get_row(conn: sqlite3.Connection, guild_id: str) -> sqlite3.Row:
    """Fetch a guild's billing row, creating it or rolling over its monthly usage as needed."""
    row = conn.execute("SELECT * FROM guild_billing WHERE guild_id = ?", (guild_id,)).fetchone()
    period = _current_period()

    if row is None:
        conn.execute(
            "INSERT INTO guild_billing (guild_id, tier, period_start) VALUES (?, ?, ?)",
            (guild_id, DEFAULT_TIER, period),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM guild_billing WHERE guild_id = ?", (guild_id,)).fetchone()

    elif row["period_start"] != period:
        conn.execute(
            "UPDATE guild_billing SET stt_seconds_used = 0, tts_chars_used = 0, period_start = ? WHERE guild_id = ?",
            (period, guild_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM guild_billing WHERE guild_id = ?", (guild_id,)).fetchone()

    return row


def get_tier(guild_id) -> str:
    with closing(_connect()) as conn:
        return _get_row(conn, str(guild_id))["tier"]


def set_tier(guild_id, tier: str) -> None:
    if tier not in TIER_LIMITS:
        raise ValueError(f"Unknown tier: {tier}")
    with closing(_connect()) as conn:
        _get_row(conn, str(guild_id))
        conn.execute("UPDATE guild_billing SET tier = ? WHERE guild_id = ?", (tier, str(guild_id)))
        conn.commit()


def get_usage(guild_id) -> dict:
    with closing(_connect()) as conn:
        row = _get_row(conn, str(guild_id))

    limits = TIER_LIMITS[row["tier"]]
    return {
        "tier": row["tier"],
        "stt_seconds_used": row["stt_seconds_used"],
        "tts_chars_used": row["tts_chars_used"],
        "stt_seconds_limit": limits["stt_seconds"],
        "tts_chars_limit": limits["tts_chars"],
    }


def record_stt_usage(guild_id, seconds: float) -> None:
    with closing(_connect()) as conn:
        _get_row(conn, str(guild_id))
        conn.execute(
            "UPDATE guild_billing SET stt_seconds_used = stt_seconds_used + ? WHERE guild_id = ?",
            (seconds, str(guild_id)),
        )
        conn.commit()


def record_tts_usage(guild_id, chars: int) -> None:
    with closing(_connect()) as conn:
        _get_row(conn, str(guild_id))
        conn.execute(
            "UPDATE guild_billing SET tts_chars_used = tts_chars_used + ? WHERE guild_id = ?",
            (chars, str(guild_id)),
        )
        conn.commit()


# ─────────────────────────────────────────────────────────────
# Gate checks — return None if allowed, or a user-facing message if blocked
def voice_access_message(guild_id) -> str | None:
    if not TIER_LIMITS[get_tier(guild_id)]["voice"]:
        return "Voice's locked behind **Premium** or **Pro**, bro. Check `!plan` or `!upgrade`."
    return None


def stt_gate_message(guild_id, seconds_needed: float = 0.0) -> str | None:
    blocked = voice_access_message(guild_id)
    if blocked:
        return blocked

    usage = get_usage(guild_id)
    if usage["stt_seconds_used"] + seconds_needed > usage["stt_seconds_limit"]:
        limit_min = usage["stt_seconds_limit"] / 60
        return (
            f"You've burned through this month's {limit_min:.0f} STT minutes on the "
            f"**{usage['tier'].capitalize()}** plan. Check `!plan` or `!upgrade`."
        )
    return None


def tts_gate_message(guild_id, chars_needed: int = 0) -> str | None:
    blocked = voice_access_message(guild_id)
    if blocked:
        return blocked

    usage = get_usage(guild_id)
    if usage["tts_chars_used"] + chars_needed > usage["tts_chars_limit"]:
        return (
            f"You've burned through this month's {usage['tts_chars_limit']:,} TTS characters on the "
            f"**{usage['tier'].capitalize()}** plan. Check `!plan` or `!upgrade`."
        )
    return None


def has_custom_personality_access(guild_id) -> bool:
    return TIER_LIMITS[get_tier(guild_id)]["custom_personalities"]


def format_plan(guild_id) -> str:
    usage = get_usage(guild_id)
    stt_used_min = usage["stt_seconds_used"] / 60
    stt_limit_min = usage["stt_seconds_limit"] / 60
    return (
        f"**Plan:** {usage['tier'].capitalize()}\n"
        f"**STT:** {stt_used_min:.1f} / {stt_limit_min:.0f} min this month\n"
        f"**TTS:** {usage['tts_chars_used']:,} / {usage['tts_chars_limit']:,} characters this month"
    )
