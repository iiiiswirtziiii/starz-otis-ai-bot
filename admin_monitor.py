"""
admin_monitor.py
----------------
Admin monitoring database + helpers.

This module handles:
- Admin registration (main + alt gamertags)
- Logging join/spawn events per admin
- Building / updating the per-admin summary embed

High-level Discord wiring (on_message listeners, tasks.loop, etc.)
should live in bot.py and call into these helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import discord
import re

from bans import get_db_connection

# How far back we look when summarising admin activity
ADMIN_ACTIVITY_WINDOW_HOURS = 48


# ===================== DB INITIALIZATION =====================

def init_admin_monitor_db() -> None:
    """
    Create admin monitor tables if they do not exist.

    Tables:
      - admin_monitor_admins: one row per registered admin
      - admin_monitor_events: join/spawn events, pruned after N hours
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_monitor_admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT NOT NULL UNIQUE,
            main_gamertag TEXT NOT NULL,
            alt_gamertag TEXT,
            log_channel_id TEXT,
            log_message_id TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_monitor_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,        -- 'join' or 'spawn'
            server_name TEXT,
            detail TEXT,
            created_at REAL NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()
    print("[ADMIN-MONITOR] Tables initialized.")


def prune_old_admin_events(max_age_hours: int = ADMIN_ACTIVITY_WINDOW_HOURS) -> int:
    """
    Delete admin_monitor_events rows older than max_age_hours.

    Returns number of rows deleted.
    """
    cutoff_ts = (datetime.utcnow() - timedelta(hours=max_age_hours)).timestamp()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM admin_monitor_events WHERE created_at < ?",
        (cutoff_ts,),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f"[ADMIN-MONITOR] Pruned {deleted} old admin events.")
    return deleted


# ===================== ADMIN REGISTRATION =====================

def register_or_update_admin(
    discord_user: discord.Member,
    main_gamertag: str,
    alt_gamertag: Optional[str] = None,
) -> int:
    """
    Insert or update an admin row. Returns admin_id.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM admin_monitor_admins WHERE discord_id = ?",
        (str(discord_user.id),),
    )
    row = cur.fetchone()

    if row:
        admin_id = row[0]
        cur.execute(
            """
            UPDATE admin_monitor_admins
            SET main_gamertag = ?, alt_gamertag = ?
            WHERE id = ?
            """,
            (main_gamertag, alt_gamertag, admin_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO admin_monitor_admins (discord_id, main_gamertag, alt_gamertag, log_channel_id, log_message_id)
            VALUES (?, ?, ?, NULL, NULL)
            """,
            (str(discord_user.id), main_gamertag, alt_gamertag),
        )
        admin_id = cur.lastrowid

    conn.commit()
    conn.close()
    print(f"[ADMIN-MONITOR] Registered admin {discord_user} as ID {admin_id}.")
    return admin_id


# ===================== NORMALIZATION / MATCHING =====================

def _normalize_gt(s: str) -> str:
    """
    Lowercase and strip anything that isn't a-z/0-9 so
    "XENO X genisis" and "XENO_X_genisis" both normalize to "xenoxgenisis".
    """
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_matching_admin_ids_from_text(text: str) -> List[int]:
    """
    Scan all registered admins and return any whose main/alt GT appears in the text.
    Normalizes spaces/underscores/punctuation so variants still match.
    """
    norm_text = _normalize_gt(text)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, main_gamertag, alt_gamertag FROM admin_monitor_admins"
    )
    rows = cur.fetchall()
    conn.close()

    matches: List[int] = []
    for row in rows:
        main_gt_norm = _normalize_gt(row["main_gamertag"])
        alt_gt_norm = _normalize_gt(row["alt_gamertag"]) if row["alt_gamertag"] else ""

        if main_gt_norm and main_gt_norm in norm_text:
            matches.append(row["id"])
            continue
        if alt_gt_norm and alt_gt_norm in norm_text:
            matches.append(row["id"])
            continue

    return matches


# ===================== SERVER NAME MAPPING =====================

def server_name_for_channel(channel_id: int) -> str:
    """
    Map feed channel IDs (player + admin feeds) to human-readable server names.
    Update this mapping when you add/remove feed channels.
    """
    mapping: Dict[int, str] = {
        # player feeds
        1351965195395928105: "Server 1",
        1351965257681338519: "Server 2",
        1351965286617579631: "Server 3",
        1351965377697153095: "Server 4",
        1351965349075091456: "Server 5",
        1384251939482501150: "Server 6",
        1384251959225094359: "Server 7",
        1384251979169009745: "Server 8",
        1386137324504617021: "Server 9",
        1386576907163926670: "Server 10",

        # admin feeds
        1325974344358301752: "Server 1",
        1340739830384038089: "Server 2",
        1340740030900994150: "Server 3",
        1341922496223383704: "Server 4",
        1341922468113158205: "Server 5",
        1384251796268257362: "Server 6",
        1384251815499141300: "Server 7",
        1384251834692272208: "Server 8",
        1386137257798275183: "Server 9",
        1386576777547088035: "Server 10",
    }
    return mapping.get(channel_id, f"Channel {channel_id}")


# ===================== EVENT LOGGING =====================

def record_admin_event(
    admin_id: int,
    event_type: str,
    server_name: str,
    detail: str,
) -> None:
    """
    Insert a join/spawn event for an admin.
    """
    now_ts = datetime.utcnow().timestamp()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO admin_monitor_events (admin_id, event_type, server_name, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (admin_id, event_type, server_name, detail[:900], now_ts),
    )
    conn.commit()
    conn.close()


async def update_admin_log_for_admin(
    bot: discord.Client,
    admin_id: int,
    log_channel_id: int,
) -> None:
    """
    Build or update the per-admin activity embed in the given log channel.

    Shows joins/spawns per server over the last ADMIN_ACTIVITY_WINDOW_HOURS.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT discord_id, main_gamertag, alt_gamertag, log_channel_id, log_message_id
        FROM admin_monitor_admins
        WHERE id = ?
        """,
        (admin_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        print(f"[ADMIN-MONITOR] No admin row found for id={admin_id}")
        return

    discord_id = row["discord_id"]
    main_gt = row["main_gamertag"] or "Unknown"
    alt_gt = row["alt_gamertag"] or None
    existing_log_channel_id = row["log_channel_id"]
    existing_log_message_id = row["log_message_id"]

    cutoff_ts = (datetime.utcnow() - timedelta(hours=ADMIN_ACTIVITY_WINDOW_HOURS)).timestamp()
    cur.execute(
        """
        SELECT event_type, server_name, COUNT(*) as cnt
        FROM admin_monitor_events
        WHERE admin_id = ? AND created_at >= ?
        GROUP BY event_type, server_name
        """,
        (admin_id, cutoff_ts),
    )
    events = cur.fetchall()
    conn.close()

    joins_by_server: Dict[str, int] = {}
    spawns_by_server: Dict[str, int] = {}

    for ev in events:
        etype = ev["event_type"]
        sname = ev["server_name"] or "Unknown"
        cnt = ev["cnt"] or 0
        if etype == "join":
            joins_by_server[sname] = joins_by_server.get(sname, 0) + cnt
        elif etype == "spawn":
            spawns_by_server[sname] = spawns_by_server.get(sname, 0) + cnt

    lines: List[str] = []
    all_servers = set(joins_by_server.keys()) | set(spawns_by_server.keys())
    if not all_servers:
        lines.append("No activity logged in the last 48 hours.")
    else:
        for sname in sorted(all_servers):
            j = joins_by_server.get(sname, 0)
            sp = spawns_by_server.get(sname, 0)
            lines.append(f"**{sname}** — joins: `{j}` | spawns: `{sp}`")

    desc = "\n".join(lines)

    log_channel = bot.get_channel(log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        print(f"[ADMIN-MONITOR] Log channel {log_channel_id} not found or not a TextChannel.")
        return

    embed = discord.Embed(
        title=f"Admin Monitor – {main_gt}",
        description=desc,
        color=0x9B59B6,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Discord ID", value=str(discord_id), inline=True)
    embed.add_field(name="Main GT", value=main_gt, inline=True)
    if alt_gt:
        embed.add_field(name="Alt GT", value=alt_gt, inline=True)

    msg_obj: Optional[discord.Message] = None

    # Try editing existing message first
    if existing_log_channel_id and existing_log_message_id:
        try:
            existing_ch = bot.get_channel(int(existing_log_channel_id))
            if isinstance(existing_ch, discord.TextChannel):
                msg_obj = await existing_ch.fetch_message(int(existing_log_message_id))
        except Exception as e:
            print(f"[ADMIN-MONITOR] Could not fetch existing log message for admin {admin_id}: {e}")
            msg_obj = None

    if msg_obj:
        try:
            await msg_obj.edit(embed=embed)
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to edit admin log embed: {e}")
    else:
        try:
            new_msg = await log_channel.send(embed=embed)
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute(
                """
                UPDATE admin_monitor_admins
                SET log_channel_id = ?, log_message_id = ?
                WHERE id = ?
                """,
                (str(log_channel.id), str(new_msg.id), admin_id),
            )
            conn2.commit()
            conn2.close()
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to send admin log embed: {e}")