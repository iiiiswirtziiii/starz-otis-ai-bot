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

# 🔥 MUST COME RIGHT AFTER THE FUTURE IMPORT
from time import time

# 🔥 MUST COME BEFORE ANY FUNCTIONS USE IT
_last_admin_embed_update: dict[int, float] = {}
ADMIN_EMBED_UPDATE_COOLDOWN = 10.0  # seconds

# Normal imports
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import io  # for in-memory text file

import asyncio
import discord
import re

from bans import get_db_connection
from config_starz import (
    ADMIN_MONITOR_LOG_CHANNEL_ID,
    ADMIN_ENFORCEMENT_CHANNEL_ID,
    ADMIN_ENFORCEMENT_ROLE_IDS,
    HIGH_RISK_SPAWN_ITEMS,
)
from rcon_web import rcon_send_all

# ============ SPAWN SUMMARY HELPERS ============

IGNORE_SPAWN_SUBSTRINGS = (
    '<slot:"name">',
    "was killed by",
    "executing console system command 'say",
)

ITEM_ALIASES = {
    "timed explosive charge": "c4",
    "explosive.timed": "c4",
    "mlrs rocket": "mlrs rocket",
    "mlrs aiming module": "mlrs module",
    "incendiary rocket": "incendiary rocket",
    "hv rocket": "hv rocket",
    "rocket launcher": "rocket launcher",
}


def summarize_spawn_row(server_name: str, detail: str, created_at: float) -> Optional[str]:
    """
    Turn a raw console spawn line into something short like:
      Server 10 c4 20 3:00 p.m.

    Returns None for noise lines we don't want to display.

    Special handling for KIT spawns:

    We get 3 lines per kit use:
      1) Executing console system command 'kit givetoplayer elitekit2 "CPTA1N" |K400|'.
      2) [ServerVar] SERVER giving CPTA1N kit elitekit2
      3) [KITMANAGER] Successfully gave [elitekit2] to [CPTA1N]

    We only want ONE clean line from #3:
      Server 10 elitekit2 1 11:08 p.m.
    """
    if not detail:
        return None

    lt = detail.lower()

    # Hard-ignore killfeed / gravity / slot dumps / console "say" spam
    for bad in IGNORE_SPAWN_SUBSTRINGS:
        if bad in lt:
            return None

    # ---- KIT spawns (clean collapse to one line) ----

    # 1) Hide the noisy console command line entirely
    if "executing console system command 'kit givetoplayer" in lt:
        return None

    # 2) Hide the SERVER giving ... kit ... line
    if "[servervar]" in lt and "server giving" in lt and " kit " in lt:
        return None

    # 3) Turn the KITMANAGER success line into "{server} {kit} 1 {time}"
    if "[kitmanager]" in lt and "successfully gave" in lt:
        # Try to pull the kit name from [elitekit2]
        m_kit = re.search(r"\[kitmanager\].*?\[([^\]]+)\]", detail, re.IGNORECASE)
        kit_name = m_kit.group(1) if m_kit else "kit"

        try:
            dt = datetime.fromtimestamp(float(created_at))
        except Exception:
            dt = datetime.utcnow()

        time_str = (
            dt.strftime("%I:%M %p")
            .lstrip("0")
            .lower()
            .replace("am", "a.m.")
            .replace("pm", "p.m.")
        )

        # We assume amount = 1 for kits
        return f"{server_name} {kit_name} 1 {time_str}"

    # ---- Normal rocket / C4 / MLRS parsing ----

    try:
        dt = datetime.fromtimestamp(float(created_at))
    except Exception:
        dt = datetime.utcnow()

    time_str = (
        dt.strftime("%I:%M %p")
        .lstrip("0")
        .lower()
        .replace("am", "a.m.")
        .replace("pm", "p.m.")
    )

    # Match "[ServerVar] giving CPTA1N 9 x Timed Explosive Charge"
    m = re.search(r"giving\s+\S+\s+(\d+)\s+x\s+(.+)", detail, re.IGNORECASE)
    if m:
        amount = m.group(1)
        item_raw = m.group(2).strip().strip(".")
        key = item_raw.lower()
        short_item = item_raw
        for k, alias in ITEM_ALIASES.items():
            if k in key:
                short_item = alias
                break
        return f"{server_name} {short_item} {amount} {time_str}"

    # Fallback: short generic line for weird spawns
    summary = detail.replace("\n", " ").strip()
    if len(summary) > 60:
        summary = summary[:57] + "..."
    return f"{server_name} {summary} {time_str}"


# ============ ADMIN BASIC LOOKUPS ============


def fetch_admin_basic(admin_id: int) -> Optional[dict]:
    """
    Let other modules (bot.py) ask: for this admin_monitor admin_id,
    what is their Discord user ID and main gamertag?
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT discord_id, main_gamertag
        FROM admin_monitor_admins
        WHERE id = ?
        """,
        (admin_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "discord_id": int(row["discord_id"]),
        "main_gamertag": row["main_gamertag"] or "",
    }


# How far back we look when summarising admin activity
ADMIN_ACTIVITY_WINDOW_HOURS = 48


def is_high_risk_spawn(detail: str) -> bool:
    """
    Return True if a spawn detail string (e.g. 'ammo.rocket.hv 50')
    matches one of the configured HIGH_RISK_SPAWN_ITEMS.
    """
    if not detail:
        return False

    parts = detail.split()
    if not parts:
        return False

    item = parts[0].strip().lower()
    return item in {name.lower() for name in HIGH_RISK_SPAWN_ITEMS}


class AdminSpawnEnforcementView(discord.ui.View):
    """
    Old enforcement review view (used by some systems).
    Note: newer auto-enforcement uses AdminSpawnAlertView in bot.py.
    """

    def __init__(
        self,
        admin_id: int,
        discord_id: str,
        main_gt: str,
        alt_gt: Optional[str],
        server_name: str,
        detail: str,
    ):
        super().__init__(timeout=600)  # 10 minutes

        self.admin_id = admin_id
        self.discord_id = discord_id
        self.main_gt = main_gt
        self.alt_gt = alt_gt
        self.server_name = server_name
        self.detail = detail

    async def _has_perms(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "You must be in the server to use this.", ephemeral=True
            )
            return False

        if any(r.id in ADMIN_ENFORCEMENT_ROLE_IDS for r in member.roles):
            return True

        await interaction.response.send_message(
            "❌ You do not have permission to act on this.",
            ephemeral=True,
        )
        return False

    async def _disable_view(self, interaction: discord.Interaction, note: str):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.followup.send(note, ephemeral=True)

    # 🔴 Ban button – permanent ban on ALL servers
    @discord.ui.button(
        label="Ban (All Servers)",
        style=discord.ButtonStyle.danger,
        emoji="🔴",
        custom_id="admin_spawn_ban",
    )
    async def ban_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._has_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        cmd = f'banid "{self.main_gt}" "OTIS: high-risk spawn – lifetime ban"'
        await rcon_send_all(cmd)

        await self._disable_view(
            interaction,
            f"🚫 Applied lifetime ban for `{self.main_gt}` on all servers.",
        )

    # 🟢 No Ban – unban + restore admin on ALL servers
    @discord.ui.button(
        label="No Ban (Unban + Restore Admin)",
        style=discord.ButtonStyle.success,
        emoji="🟢",
        custom_id="admin_spawn_no_ban",
    )
    async def no_ban_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await self._has_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        unban_cmd = f'unban "{self.main_gt}"'
        admin_cmd = f'adminid "{self.main_gt}"'

        await rcon_send_all(unban_cmd)
        await asyncio.sleep(2)
        await rcon_send_all(admin_cmd)

        await self._disable_view(
            interaction,
            f"✅ Cleared auto-flag and restored admin for `{self.main_gt}`.",
        )


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


def build_admin_actions_text(
    admin_id: int,
    window_hours: int = ADMIN_ACTIVITY_WINDOW_HOURS,
) -> str:
    """
    Export ALL admin_monitor_events for this admin in the last `window_hours`
    as multi-line text. One line per event:

      [YYYY-MM-DD HH:MM:SS] [Server X] [join|spawn] detail...
    """
    cutoff_ts = (datetime.utcnow() - timedelta(hours=window_hours)).timestamp()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT event_type, server_name, detail, created_at
        FROM admin_monitor_events
        WHERE admin_id = ?
          AND created_at >= ?
        ORDER BY created_at ASC
        """,
        (admin_id, cutoff_ts),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return f"No admin activity logged in the last {window_hours} hours.\n"

    lines: list[str] = []
    for r in rows:
        ts = r["created_at"] or 0
        try:
            dt = datetime.fromtimestamp(float(ts))
        except Exception:
            dt = datetime.utcnow()
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

        server = r["server_name"] or "Unknown"
        etype = r["event_type"] or "unknown"
        detail = (r["detail"] or "").replace("\n", " ").strip()

        lines.append(f"[{ts_str}] [{server}] [{etype}] {detail}")

    return "\n".join(lines) + "\n"


def get_admin_profile(admin_id: int) -> Optional[dict]:
    """
    Look up an admin's Discord ID + main gamertag
    by admin_monitor_admins.id.
    Returns {"discord_id": int, "gamertag": str} or None.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT discord_id, main_gamertag
        FROM admin_monitor_admins
        WHERE id = ?
        """,
        (admin_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    discord_id = int(row["discord_id"])
    gamertag = row["main_gamertag"] or ""
    return {"discord_id": discord_id, "gamertag": gamertag}


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


def remove_admin_by_discord_id(discord_id: int) -> int:
    """
    Remove an admin (and their events) by Discord ID.
    Returns how many admin rows were deleted (0 or 1).
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Find admin row(s)
    cur.execute(
        "SELECT id FROM admin_monitor_admins WHERE discord_id = ?",
        (str(discord_id),),
    )
    rows = cur.fetchall()
    admin_ids = [r["id"] for r in rows] if rows else []

    if not admin_ids:
        conn.close()
        return 0

    # Delete their events first
    cur.execute(
        f"DELETE FROM admin_monitor_events WHERE admin_id IN ({','.join('?' for _ in admin_ids)})",
        admin_ids,
    )

    # Delete the admins themselves
    cur.execute(
        f"DELETE FROM admin_monitor_admins WHERE id IN ({','.join('?' for _ in admin_ids)})",
        admin_ids,
    )

    deleted = cur.rowcount
    conn.commit()
    conn.close()
    print(f"[ADMIN-MONITOR] Removed {deleted} admin row(s) for discord_id={discord_id}.")
    return deleted


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

    Shows joins/spawns per server over the last ADMIN_ACTIVITY_WINDOW_HOURS,
    AND lists the most recent spawn events with their detail text.
    Also attaches a .txt file with the full last-48h history.
    """
    # ---- cooldown to avoid spam edits ----
    now = time()
    last = _last_admin_embed_update.get(admin_id, 0.0)
    if now - last < ADMIN_EMBED_UPDATE_COOLDOWN:
        return
    _last_admin_embed_update[admin_id] = now

    # ---- fetch admin row ----
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
    conn.close()

    if not row:
        print(f"[ADMIN-MONITOR] No admin row found for id={admin_id}")
        return

    discord_id = row["discord_id"]
    main_gt = row["main_gamertag"] or "Unknown"
    alt_gt = row["alt_gamertag"] or None
    existing_log_channel_id = row["log_channel_id"]
    existing_log_message_id = row["log_message_id"]

    # ---- resolve display name if possible ----
    discord_name = f"<@{discord_id}>" if discord_id else f"Admin {admin_id}"
    try:
        if discord_id:
            for guild in bot.guilds:
                member = guild.get_member(int(discord_id))
                if member is not None:
                    discord_name = member.display_name
                    break
    except Exception:
        pass

    # ---- aggregate last-48h join/spawn counts (still used for the .txt file etc.) ----
    cutoff_ts = (datetime.utcnow() - timedelta(hours=ADMIN_ACTIVITY_WINDOW_HOURS)).timestamp()
    conn2 = get_db_connection()
    cur2 = conn2.cursor()
    cur2.execute(
        """
        SELECT event_type, server_name, COUNT(*) as cnt
        FROM admin_monitor_events
        WHERE admin_id = ? AND created_at >= ?
        GROUP BY event_type, server_name
        """,
        (admin_id, cutoff_ts),
    )
    rows = cur2.fetchall()

    joins_by_server: Dict[str, int] = {}
    spawns_by_server: Dict[str, int] = {}

    for r in rows:
        etype = r["event_type"]
        sname = r["server_name"] or "Unknown"
        cnt = r["cnt"] or 0

        if etype == "join":
            joins_by_server[sname] = joins_by_server.get(sname, 0) + cnt
        elif etype == "spawn":
            spawns_by_server[sname] = spawns_by_server.get(sname, 0) + cnt

    # ---- build description text ----
    lines: list[str] = []

    # Admin info header
    lines.append("admin")
    lines.append("---------------")
    lines.append(discord_name)
    lines.append("")
    lines.append("gamertag")
    lines.append("---------------")
    lines.append(main_gt)
    if alt_gt:
        lines.append(f"alt: {alt_gt}")
    lines.append("")

    # Global description length cap so we never hit Discord 4096 hard limit
    max_desc_chars = 3500

    # Servers joined (detailed, one line per join)
    lines.append("servers loaded into (last 48h)")
    lines.append("---------------------")

    cur2.execute(
        """
        SELECT server_name, created_at
        FROM admin_monitor_events
        WHERE admin_id = ?
          AND event_type = 'join'
          AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (admin_id, cutoff_ts),
    )
    join_rows = cur2.fetchall()

    if join_rows:
        max_join_lines = 20
        join_added = 0
        for jr in join_rows:
            if join_added >= max_join_lines:
                lines.append("...and more joins in the last 48h.")
                break

            sname = jr["server_name"] or "Unknown"
            ts = jr["created_at"] or 0
            try:
                dtj = datetime.fromtimestamp(float(ts))
            except Exception:
                dtj = datetime.utcnow()

            time_str = (
                dtj.strftime("%I:%M %p")
                .lstrip("0")
                .lower()
                .replace("am", "a.m.")
                .replace("pm", "p.m.")
            )

            line_text = f"- {sname} joined {time_str}"
            # Optional safety check so joins don't blow past the char limit
            if len("\n".join(lines)) + 1 + len(line_text) > max_desc_chars:
                lines.append("...and more joins in the last 48h.")
                break

            lines.append(line_text)
            join_added += 1
    else:
        lines.append("no server joins recorded in the last 48 hours.")
    lines.append("")

    # Spawned items/kits
    lines.append("items and kits spawned (last 48h)")
    lines.append("-----------------------------")

    # recent spawn rows (detailed)
    cur2.execute(
        """
        SELECT server_name, detail, created_at
        FROM admin_monitor_events
        WHERE admin_id = ?
          AND event_type = 'spawn'
          AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (admin_id, cutoff_ts),
    )
    spawn_rows = cur2.fetchall()
    conn2.close()

    added = 0
    max_visible_spawn_lines = 8

    for row2 in spawn_rows:
        if added >= max_visible_spawn_lines:
            break

        sname = row2["server_name"] or "Unknown"
        detail = row2["detail"] or ""
        created_at = row2["created_at"] or 0

        summary = summarize_spawn_row(sname, detail, created_at)
        if not summary:
            continue

        prospective_line = f"- {summary}"
        if len("\n".join(lines)) + 1 + len(prospective_line) > max_desc_chars:
            lines.append("...and more spawn activity in the last 48h.")
            break

        lines.append(prospective_line)
        added += 1

    if added == 0:
        lines.append("no items or kits spawned logged in the last 48 hours.")

    desc = "\n".join(lines)

    # ---- resolve log channel ----
    log_channel = bot.get_channel(log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        print(f"[ADMIN-MONITOR] Log channel {log_channel_id} not found or not a TextChannel.")
        return

    # ---- build embed ----
    embed = discord.Embed(
        title=f"Admin Monitor – {discord_name}",
        description=desc,
        color=0x9B59B6,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Discord ID", value=str(discord_id), inline=True)
    embed.add_field(name="Main GT", value=main_gt, inline=True)
    if alt_gt:
        embed.add_field(name="Alt GT", value=alt_gt, inline=True)

    embed.add_field(
        name="Full 48h history",
        value="📄 See attached `.txt` file for all joins & spawns in the last 48 hours.",
        inline=False,
    )

    # ---- build attached text file with full history ----
    actions_text = build_admin_actions_text(admin_id)
    file_bytes = io.BytesIO(actions_text.encode("utf-8"))
    filename = f"admin_{admin_id}_actions_last{ADMIN_ACTIVITY_WINDOW_HOURS}h.txt"

    # ---- try to edit existing embed if we know its message id ----
    msg_obj: Optional[discord.Message] = None
    if existing_log_channel_id and existing_log_message_id:
        try:
            existing_ch = bot.get_channel(int(existing_log_channel_id))
            if isinstance(existing_ch, discord.TextChannel):
                msg_obj = await existing_ch.fetch_message(int(existing_log_message_id))
        except Exception as e:
            print(f"[ADMIN-MONITOR] Could not fetch existing log message for admin {admin_id}: {e}")
            msg_obj = None

    # --- Always keep exactly ONE message per admin: file + embed together ---
    if msg_obj:
        try:
            await msg_obj.delete()
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to delete old admin log message for {admin_id}: {e}")

    # Send a fresh message with the new embed + updated .txt file
    try:
        new_msg = await log_channel.send(
            embed=embed,
            file=discord.File(file_bytes, filename=filename),
        )
    except Exception as e:
        print(f"[ADMIN-MONITOR] Failed to send admin log embed/file: {e}")
        return

    # Update DB to point at this new message
    try:
        conn3 = get_db_connection()
        cur3 = conn3.cursor()
        cur3.execute(
            """
            UPDATE admin_monitor_admins
            SET log_channel_id = ?, log_message_id = ?
            WHERE id = ?
            """,
            (str(log_channel.id), str(new_msg.id), admin_id),
        )
        conn3.commit()
        conn3.close()
    except Exception as e:
        print(f"[ADMIN-MONITOR] Failed to store log message pointer: {e}")

