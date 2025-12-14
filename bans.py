"""
bans.py
-------
Central place for:
- Ban database (SQLite)
- Helper functions to create / update bans
- Helper to build the "Active Bans" embed

This module does NOT know about slash commands or Discord bot events.
bot.py will call into these helpers.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Iterable

import discord

# ===================== DB CONFIG =====================

BAN_DB_PATH = os.getenv("BAN_DB_PATH", "starz_bans.db")
print(f"[BANS] Using DB path: {BAN_DB_PATH}")


def get_db_connection() -> sqlite3.Connection:
    """
    Open a connection to the bans DB with row access by column name.
    Added timeout to avoid 'database is locked' errors.
    """
    conn = sqlite3.connect(BAN_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn



def init_ban_db() -> None:
    """
    Make sure all necessary tables and indexes for bans exist.
    Safe to call multiple times (CREATE IF NOT EXISTS).
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Core bans table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gamertag TEXT NOT NULL,
            discord_id TEXT,
            reason TEXT,
            offense_tier INTEGER NOT NULL,
            banned_at REAL NOT NULL,
            expires_at REAL,
            active INTEGER NOT NULL DEFAULT 1,
            source TEXT,
            moderator_id TEXT
        )
        """
    )

    # Helpful indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bans_gamertag ON bans (gamertag)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bans_active ON bans (active)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_bans_expires_at ON bans (expires_at)"
    )

    conn.commit()
    conn.close()
    print("[BANS] DB initialized / already exists.")


# ===================== BAN LOG HELPERS =====================


async def send_ban_log_embed(
    bot: discord.Client,
    ban_log_channel_id: int,
    *,
    gamertag: str,
    discord_id: Optional[int],
    reason: str,
    offense_tier: int,
    duration_text: str,
    moderator: Optional[discord.Member],
    source: str,
) -> None:
    """
    Send a 'player banned' log embed to the ban log channel.
    """
    channel = bot.get_channel(ban_log_channel_id)
    if not isinstance(channel, discord.TextChannel):
        print(
            f"[BANS] Ban log channel {ban_log_channel_id} not found or not TextChannel."
        )
        return

    mod_text = moderator.mention if isinstance(moderator, discord.Member) else "Unknown"

    embed = discord.Embed(
        title="🔨 Player Banned",
        color=0xE74C3C,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Gamertag", value=gamertag, inline=True)
    if discord_id:
        embed.add_field(name="Discord", value=f"<@{discord_id}>", inline=True)
    embed.add_field(name="Tier", value=str(offense_tier), inline=True)
    embed.add_field(name="Duration", value=duration_text, inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
    embed.add_field(name="Moderator", value=mod_text, inline=False)
    embed.set_footer(text=f"Source: {source}")

    await channel.send(embed=embed)


async def send_unban_log_embed(
    bot: discord.Client,
    ban_log_channel_id: int,
    *,
    gamertag: str,
    moderator: Optional[discord.Member],
    source: Optional[str] = None,
) -> None:
    """
    Send an 'unban' log embed to the ban log channel.
    """
    channel = bot.get_channel(ban_log_channel_id)
    if not isinstance(channel, discord.TextChannel):
        print(
            f"[BANS] Ban log channel {ban_log_channel_id} not found or not TextChannel."
        )
        return

    mod_text = moderator.mention if isinstance(moderator, discord.Member) else "Unknown"

    embed = discord.Embed(
        title="✅ Player Unbanned",
        color=0x2ECC71,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Gamertag", value=gamertag, inline=True)
    embed.add_field(name="Moderator", value=mod_text, inline=True)
    if source:
        embed.set_footer(text=f"Source: {source}")

    await channel.send(embed=embed)


# ===================== BAN DB OPERATIONS =====================


def _tier_from_previous_count(previous_bans: int) -> int:
    """
    Decide which offense tier this is based on how many previous bans exist.

    New logic:
        0 previous -> tier 1 (1st offense)
        1 previous -> tier 2 (2nd offense)
        2 previous -> tier 3 (3rd offense)
        3+ previous -> tier 4 (4th offense, perm)
    """
    if previous_bans <= 0:
        return 1
    elif previous_bans == 1:
        return 2
    elif previous_bans == 2:
        return 3
    else:
        return 4


def _duration_for_tier(tier: int) -> tuple[Optional[float], str]:
    """
    Get (expires_at_timestamp, duration_text) for a given tier.

    New punishments:
        Tier 1 → 24 hours
        Tier 2 → 48 hours
        Tier 3 → 7 days
        Tier 4 → Permanent
    """
    now = datetime.utcnow()

    if tier == 1:
        expires = now + timedelta(hours=24)
        return expires.timestamp(), "24 hours"
    elif tier == 2:
        expires = now + timedelta(hours=48)
        return expires.timestamp(), "48 hours"
    elif tier == 3:
        expires = now + timedelta(days=7)
        return expires.timestamp(), "7 days"
    else:
        # Perm ban
        return None, "Permanent ban"


def create_ban_record(
    *,
    gamertag: str,
    discord_id: Optional[int],
    reason: str,
    source: str,
    moderator_id: Optional[int],
) -> tuple[int, Optional[float], str]:
    """
    Insert a new ban row for this gamertag and return:
        (offense_tier, expires_at_timestamp, duration_text)

    - offense_tier is decided from how many previous bans they have.
    - expires_at_timestamp may be None for permanent bans.
    """
    now_ts = datetime.utcnow().timestamp()

    conn = get_db_connection()
    cur = conn.cursor()

    # Count how many bans this player already has (active or inactive).
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM bans WHERE gamertag = ?",
        (gamertag,),
    )
    row = cur.fetchone()
    previous_count = int(row["cnt"] if row and row["cnt"] is not None else 0)

    offense_tier = _tier_from_previous_count(previous_count)
    expires_at_ts, duration_text = _duration_for_tier(offense_tier)

    cur.execute(
        """
        INSERT INTO bans (
            gamertag,
            discord_id,
            reason,
            offense_tier,
            banned_at,
            expires_at,
            active,
            source,
            moderator_id
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            gamertag,
            str(discord_id) if discord_id is not None else None,
            reason,
            offense_tier,
            now_ts,
            expires_at_ts,
            source,
            str(moderator_id) if moderator_id is not None else None,
        ),
    )

    conn.commit()
    conn.close()

    return offense_tier, expires_at_ts, duration_text


def lookup_ban_status_by_gamertag(
    gamertag: str,
) -> tuple[Optional[sqlite3.Row], int]:
    """
    Look up the current active ban (if any) for this gamertag, and count
    how many total bans they have on record (active or inactive).

    Returns:
        (active_ban_row_or_None, total_bans_count)
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Active ban, newest first
    cur.execute(
        """
        SELECT *
        FROM bans
        WHERE gamertag = ?
          AND active = 1
        ORDER BY banned_at DESC
        LIMIT 1
        """,
        (gamertag,),
    )
    active_row = cur.fetchone()

    # Total bans ever
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM bans WHERE gamertag = ?",
        (gamertag,),
    )
    row = cur.fetchone()
    total_bans = int(row["cnt"] if row and row["cnt"] is not None else 0)

    conn.close()
    return active_row, total_bans


def describe_next_offense(total_bans: int) -> tuple[int, str]:
    """
    Given how many bans they already have, describe the *next* offense
    tier and its duration text (using the same ladder as create_ban_record).

    Example:
        total_bans = 0 -> next offense is tier 1, "24 hours"
        total_bans = 1 -> next offense is tier 2, "48 hours"
        total_bans = 2 -> next offense is tier 3, "7 days"
        total_bans >= 3 -> next offense is tier 4, "Permanent ban"
    """
    next_tier = _tier_from_previous_count(total_bans)
    _, duration_text = _duration_for_tier(next_tier)
    return next_tier, duration_text


def mark_unbanned(gamertag: str) -> int:
    """
    Mark all active bans for this gamertag as inactive.
    Returns the number of rows updated.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE bans SET active = 0 WHERE gamertag = ? AND active = 1",
        (gamertag,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def purchased_unban(gamertag: str) -> int:
    """
    Unban a player because they purchased an unban.

    - Sets active = 0 for all active bans for this gamertag.
    - Keeps the rows in the DB so offenses still count for future bans.
    - Tags the source field with 'purchased unban' so staff can see why.

    Returns the number of rows updated.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE bans
        SET active = 0,
            source = CASE
                WHEN source IS NULL OR source = ''
                    THEN 'purchased unban'
                ELSE source || ' (purchased unban)'
            END
        WHERE gamertag = ?
          AND active = 1
        """,
        (gamertag,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()

    print(f"[BANS] Purchased unban for {gamertag}, deactivated {changed} active ban(s).")
    return changed


def reduce_offense_for_gamertag_if_eligible(
    gamertag: str,
    *,
    min_age_days: int = 90,
) -> int:
    """
    Reduce ban offenses by 1 for a specific player IF their most recent ban
    is older than `min_age_days` (default ~3 months).

    Implementation:
      - Find the most recent ban row for this gamertag.
      - If that ban's banned_at < now - min_age_days => delete that row.
      - Returns 1 if a row was deleted (offense reduced), 0 otherwise.

    NOTE:
      - This physically deletes one ban row from the DB.
      - This means the "offense history" visible in the DB will reduce by 1,
        which is exactly what we want for a decay system.
    """
    now = datetime.utcnow()
    cutoff_ts = (now - timedelta(days=min_age_days)).timestamp()

    conn = get_db_connection()
    cur = conn.cursor()

    # Get the most recent ban for this player
    cur.execute(
        """
        SELECT id, banned_at
        FROM bans
        WHERE gamertag = ?
        ORDER BY banned_at DESC
        LIMIT 1
        """,
        (gamertag,),
    )
    row = cur.fetchone()

    if row is None:
        conn.close()
        print(f"[BANS] No bans found for {gamertag}; nothing to reduce.")
        return 0

    last_banned_at = row["banned_at"]
    ban_id = row["id"]

    if last_banned_at is None or last_banned_at > cutoff_ts:
        # Last ban is too recent; don't reduce yet.
        conn.close()
        print(
            f"[BANS] Last ban for {gamertag} is not old enough for offense reduction."
        )
        return 0

    # OK to reduce one offense: delete this ban row
    cur.execute("DELETE FROM bans WHERE id = ?", (ban_id,))
    conn.commit()
    conn.close()

    print(f"[BANS] Reduced offenses by 1 for {gamertag} (deleted ban id={ban_id}).")
    return 1


def deactivate_expired_bans() -> int:
    """
    Deactivate any bans whose expires_at is in the past.
    Returns the number of rows updated.
    """
    now_ts = datetime.utcnow().timestamp()

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE bans
        SET active = 0
        WHERE active = 1
          AND expires_at IS NOT NULL
          AND expires_at <= ?
        """,
        (now_ts,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()

    print(f"[BANS] Deactivated {changed} expired ban(s).")
    return changed


# ===================== ACTIVE BANS EMBED =====================


def fetch_active_bans(limit: Optional[int] = None) -> Iterable[sqlite3.Row]:
    """
    Return active bans, newest first.
    If limit is provided, apply LIMIT to the query.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    base_query = """
        SELECT
            gamertag,
            reason,
            offense_tier,
            banned_at,
            expires_at
        FROM bans
        WHERE active = 1
        ORDER BY banned_at DESC
    """

    if limit is not None:
        base_query += " LIMIT ?"
        cur.execute(base_query, (limit,))
    else:
        cur.execute(base_query)

    rows = cur.fetchall()
    conn.close()
    return rows


def build_active_bans_embed(
    *,
    title: str = "🔒 Active Bans",
    limit: Optional[int] = None,
) -> discord.Embed:
    """
    Build a Discord Embed listing all active bans, or a 'none' message if empty.
    """
    rows = list(fetch_active_bans(limit=limit))

    if not rows:
        desc = "There are currently **no active bans**."
    else:
        lines: list[str] = []
        for row in rows:
            gt = row["gamertag"]
            tier = row["offense_tier"]
            reason = row["reason"] or "No reason provided."
            banned_at_ts = row["banned_at"]
            expires_at_ts = row["expires_at"]

            banned_at_str = datetime.utcfromtimestamp(banned_at_ts).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            if expires_at_ts is None:
                expire_text = "Permanent"
            else:
                expire_text = datetime.utcfromtimestamp(expires_at_ts).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )

            lines.append(
                f"• **{gt}** — Tier `{tier}` | "
                f"Banned: `{banned_at_str}` | Expires: `{expire_text}`\n"
                f"  Reason: {reason}"
            )

        desc = "\n".join(lines)

    embed = discord.Embed(
        title=title,
        description=desc,
        color=0xE74C3C,
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Ban list is auto-managed by STARZ AI / staff actions.")
    return embed
