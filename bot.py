import os
import json
import websockets
import random
from typing import Dict, Any, List, Tuple, Optional
from dotenv import load_dotenv

load_dotenv()
import re
import time
import traceback

import discord
from discord.ext import commands, tasks
from discord import app_commands
from openai import OpenAI
import asyncio
from enum import Enum
from starz_printpos.tp_zones import DEFAULT_ZONE_COLORS
class StaffType(Enum):
    ADMIN = "admin"
    PROMOTER = "promoter"
from datetime import datetime, timedelta, timezone, UTC





from admin_mon_system import log_admin_activity_for_ids
from promoter_mon_system import maybe_handle_promoter_spawn


async def send_rcon_disconnect_alert(bot, server_key: str, error: str):
    from datetime import datetime, UTC

    now = datetime.now(UTC).timestamp()
    last_time = RCON_DISCONNECT_COOLDOWN.get(server_key, 0)

    # If last alert was within 10 minutes, SKIP sending another
    if now - last_time < RCON_DISCONNECT_DELAY:
        return

    # Update timestamp for this server
    RCON_DISCONNECT_COOLDOWN[server_key] = now
    # Pause printpos/TP while RCON is unstable to prevent queue desync + command flooding
    try:
        set_printpos_enabled(False)
    except Exception:
        pass

    channel_id = 1447090579350618122  # RCON disconnect alerts channel
    channel = bot.get_channel(channel_id)

    if not channel:
        print(f"[RCON-ALERT] ERROR: Could not find channel {channel_id}")
        return

    embed = discord.Embed(
        title="⚠️ RCON Connection Lost",
        description=(
            f"**Server:** `{server_key}`\n"
            f"**Error:** `{error}`\n"
            f"**Time:** <t:{int(now)}:F>"
        ),
        color=0xE67E22,
    )

    await qsend(lambda: channel.send(embed=embed))


startup_file_failures: List[str] = []  # Tracks any files that failed to load
rcon_failures: List[str] = []

# Cooldown to prevent RCON spam alerts
RCON_DISCONNECT_COOLDOWN: Dict[str, float] = {}  # server_key -> timestamp
RCON_DISCONNECT_DELAY = 600  # 600 seconds = 10 minutes

# ================================
# STARTUP LOGGING SYSTEM
# ================================
STARTUP_LOG_FILE = "startup.log"


def write_startup_log(message: str):
    timestamp = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {message}"
    print(line)  # console
    try:
        with open(STARTUP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[STARTUP] Failed writing to log: {e}")


def check_file_exists(path: str, required: bool = False):
    if os.path.exists(path):
        write_startup_log(f"✔ Loaded file: {path}")
        return True
    else:
        # Track missing file
        startup_file_failures.append(path)

        if required:
            write_startup_log(f"❌ REQUIRED file missing: {path}")
        else:
            write_startup_log(f"⚠ Optional file missing: {path}")
        return False


def check_env_var(name: str):
    value = os.getenv(name)
    if value:
        write_startup_log(f"✔ ENV var loaded: {name}")
        return True
    else:
        write_startup_log(f"❌ ENV var missing: {name}")
        return False


# ================= IMPORTS ==================
# ================================
# DISCORD SEND QUEUE (ANTI-429 SPAM)
# ================================
DISCORD_SEND_QUEUE: asyncio.Queue = asyncio.Queue()
DISCORD_SEND_WORKER_STARTED = False

async def _discord_send_worker():
    while True:
        coro_factory, fut = await DISCORD_SEND_QUEUE.get()
        try:
            res = await coro_factory()
            if not fut.done():
                fut.set_result(res)
        except Exception as e:
            if not fut.done():
                fut.set_exception(e)
        finally:
            DISCORD_SEND_QUEUE.task_done()
            # gentle pacing to avoid bursts
            await asyncio.sleep(0.25)

async def qsend(coro_factory):
    """
    Queue any discord send/edit so we don't blast the API and trigger constant 429s.
    Usage: await qsend(lambda: channel.send(...))
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    await DISCORD_SEND_QUEUE.put((coro_factory, fut))
    return await fut

from ticket_helpers import (
    handle_ticket_claim_message,
    note_ticket_opener,
    maybe_handle_close_message,
)
from kit_helpers import (
    kit_first_help,
    looks_like_kit_question,
    looks_like_kit_issue,
    kit_claims,
)

from zorp_lookup import find_zorp_events_for_member, summarize_zorp_event

from config_starz import (
    DISCORD_BOT_TOKEN,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    STAFF_ALERT_CHANNEL_ID,
    TICKET_CATEGORY_IDS,
    TRIAL_ADMIN_ID,
    SERVER_ADMIN_ID,
    HEAD_ADMIN_ID,
    ADMIN_MANAGEMENT_ID,
    KAOS_MOD_ID,
    AI_CONTROL_ROLES,
    ACTIVE_BANS_CHANNEL_ID,
    BAN_LOG_CHANNEL_ID,
    KAOS_COMMAND_CHANNEL_ID,
    SHOP_LOG_CHANNEL_ID,
    UNBAN_SHOP_PREFIX,
    ZORP_FEED_CHANNEL_IDS,
    PLAYER_FEED_CHANNEL_IDS,
    ADMIN_FEED_CHANNEL_IDS,
    ADMIN_MONITOR_LOG_CHANNEL_ID,
    load_style_text,
    load_rules_text,
    load_zorp_guide_text,
    load_raffle_text,
    ADMIN_ENFORCEMENT_CHANNEL_ID,
    ADMIN_ENFORCEMENT_ROLE_IDS,
    HIGH_RISK_SPAWN_ITEMS,
    RCON_ENABLED,
)

# Kits that should be treated as HIGH-RISK when claimed.
# These are the exact strings that appear in RCON logs (case-insensitive).
HIGH_RISK_KITS = {
    "elitekit1",
    "elitekit3",
    "elitekit13",
    "elitekit17",
    "elitekit20",
    "elitekit27",
    "elitekit29",
    "elitekit31",
    "elitekit33",
    "elitekit36",
    "elitekit41",
}

from rcon_web import (
    check_rcon_health_on_startup,
    run_rcon_command,
    rcon_send_all,
    rcon_manager,
    RCON_CONFIGS,  
)


# Adapter so starz_printpos can use the same RCON helper
async def tp_send_rcon(server_key: str, command: str) -> str | None:
    """
    Adapter used by the TP / printpos system.
    MUST return the response string so tp_tracker can parse coordinates.
    """
    resp = await run_rcon_command(command, client_key=server_key)

    # ✅ IMPORTANT: different wrappers put the text in different keys.
    if isinstance(resp, dict):
        text = (
            resp.get("Response")
            or resp.get("response")
            or resp.get("Message")
            or resp.get("message")
            or resp.get("Result")
            or resp.get("result")
            or ""
        )

        # debug if it still comes back empty
        if not text:
            print(f"[TP-DEBUG] tp_send_rcon EMPTY dict keys={list(resp.keys())} resp={resp!r}")

        return str(text)

    return str(resp or "")




from bans import (
    init_ban_db,
    create_ban_record,
    mark_unbanned,
    deactivate_expired_bans,
    build_active_bans_embed,
    send_ban_log_embed,
    send_unban_log_embed,
    reduce_offense_for_gamertag_if_eligible,
    lookup_ban_status_by_gamertag,
    describe_next_offense,
)

from admin_monitor import (
    init_admin_monitor_db,
    prune_old_admin_events,
    register_or_update_admin,
    remove_admin_by_discord_id,
    record_admin_event,
    update_admin_log_for_admin,
    get_admin_id_for_discord,
    is_admin_immune,
    set_admin_immunity_hours,
    find_matching_admin_ids_from_text,
    fetch_admin_basic,
    get_admin_profile,
    server_name_for_channel,
)

from admin_promotion_watch import maybe_handle_admin_promotion
from ticket_ai import maybe_handle_ticket_ai_message

from workflows import (
    ticket_workflows,
    process_workflow_answer,
    ADMIN_ABUSE_KEYWORDS,
    ZORP_ISSUE_KEYWORDS,
    REFUND_KEYWORDS,
    KIT_ISSUE_WORKFLOW_KEYWORDS,
    start_admin_abuse_workflow,
    start_zorp_issue_workflow,
    start_refund_workflow,
    start_kit_issue_workflow,
)

from nuke_rewards import (
    maybe_handle_nuke_purchase,
    get_recent_nuke_stats,
)
# ----- Teleport / printpos system -----
from starz_printpos import (
    init_printpos_system,
    start_printpos_polling,
    handle_printpos_console_line,
    update_connected_players,
    set_enabled as set_printpos_enabled,
    is_enabled as is_printpos_enabled,
    TPType,
    set_tp_zone,
    get_all_zones,
    clear_tp_type,
)





from starz_printpos.tp_zones import (
    get_configured_tp_types,
    delete_tp_type,
)




# ==========================
# TP / ZONE RCON CONFIG
# ==========================

# Only send zone commands to servers that exist in RCON_CONFIGS
ZONE_RCON_SERVER_KEYS = list(RCON_CONFIGS.keys())


from typing import Dict, Tuple

# Friendly display names per TPType (for zone names + messages)
FRIENDLY_TP_NAMES: Dict[TPType, str] = {
    TPType.LAUNCHSITE: "Launchsite",
    TPType.AIRFIELD: "Airfield",
    TPType.JUNKYARD: "Junkyard",
    TPType.OXUMS_GAS_STATION: "Oxums Gas Station",
    TPType.WATER_TREATMENT_PLANT: "Water Treatment Plant",
    TPType.BANDIT_CAMP: "Bandit Camp",
    TPType.OUTPOST: "Outpost",
    TPType.FISHING_VILLAGE: "Fishing Village",
    TPType.MILLITARY_TUNNELS: "Military Tunnels",
    TPType.ABANDONED_MILITARY_BASE: "Abandoned Military Base",
}

# Per-type default enter / exit messages (for TP system, NOT the Rust plugin)
TP_ENTER_MESSAGES: Dict[TPType, str] = {
    tp: f"Teleporting via {name} zone..."
    for tp, name in FRIENDLY_TP_NAMES.items()
}

TP_EXIT_MESSAGES: Dict[TPType, str] = {
    tp: f"You have left the {name} zone."
    for tp, name in FRIENDLY_TP_NAMES.items()
}

# Per-type RGB colors for the Rust zones plugin
TP_ZONE_COLORS: Dict[TPType, Tuple[int, int, int]] = {
    TPType.LAUNCHSITE: (255, 0, 0),           # red
    TPType.AIRFIELD: (0, 255, 0),             # green (like your example)
    TPType.JUNKYARD: (139, 69, 19),           # brown-ish
    TPType.OXUMS_GAS_STATION: (255, 255, 0),  # yellow
    TPType.WATER_TREATMENT_PLANT: (0, 255, 255),  # cyan
    TPType.BANDIT_CAMP: (255, 0, 255),        # magenta
    TPType.OUTPOST: (0, 0, 255),              # blue
    TPType.FISHING_VILLAGE: (0, 128, 128),    # teal
    TPType.MILLITARY_TUNNELS: (128, 0, 128),  # purple
    TPType.ABANDONED_MILITARY_BASE: (128, 128, 128),  # gray
}



# ============= SANITY CHECKS =============

if not DISCORD_BOT_TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN is not set.")

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is not set.")

client_ai = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=30.0,   # Railway/network can be a little slow sometimes
    max_retries=3,  # built-in retry for transient connection issues
)


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ============= GLOBAL STATE =============
# RCON console watcher tasks
RCON_WATCH_TASKS: List[asyncio.Task] = []

# Prevent printpos/TP loops from starting twice on reconnects
PRINTPOS_SYSTEM_STARTED = False  # False at boot so on_ready initializes it once

# ===================== PRINTPOS CONFIG =====================
PLAYERLIST_REFRESH_SECONDS = 20  # how often to refresh playerlist names



style_text = load_style_text()
rules_text = load_rules_text()
zorp_guide_text = load_zorp_guide_text()
raffle_text = load_raffle_text()

ticket_sessions: Dict[int, Dict[str, Any]] = {}
# For join dedup in RCON watcher: (admin_id, server_name) -> last join ts
recent_join_events: Dict[Tuple[int, str], float] = {}
JOIN_DEDUP_WINDOW_SECONDS = 60  # treat joins within 60s as duplicates

# ============= ADMIN SPAWN ENFORCEMENT STATE =============

# How long to buffer rockets/C4 spawns before sending one combined alert
SPAWN_ENFORCEMENT_WINDOW_SECONDS = 10

# admin_id -> list of spawn events in the last window
spawn_enforcement_buffer: Dict[int, List[Dict[str, Any]]] = {}

# admin_id -> asyncio.Task that will flush their buffer
spawn_enforcement_tasks: Dict[int, asyncio.Task] = {}

# admins we have already kicked for this enforcement burst
spawn_enforcement_already_kicked: set[int] = set()

# When the tracker is temporarily disabled for events (UTC)
TRACKER_DISABLED_UNTIL: Optional[datetime] = None

# (admin_id, server_key, item) -> last enforcement ts
recent_enforcement_events: Dict[Tuple[int, str, str], float] = {}


active_ai_channels: set[int] = set()
ticket_openers: Dict[int, int] = {}
ai_greeting_sent: set[int] = set()
# (admin_id, server_name) -> last join ts (used to prevent false positives on connect-load kits)
admin_last_join_ts: Dict[Tuple[int, str], float] = {}
JOIN_GRACE_SECONDS_FOR_SPAWN_ENFORCE = 20  # ignore high-risk spawns right after joining

# ============= BUILD GREETING EMBED =============


def build_ai_greeting_embed(opener: Optional[discord.Member] = None) -> discord.Embed:
    desc = "Hello, I'm **Otis**. How can I help you today?"
    embed = discord.Embed(
        title="STARZ AI ADMIN (Otis)",
        description=desc,
        color=0x3498DB,
    )
    return embed


# ===================== AI TOGGLE VIEW =====================


class AIToggleView(discord.ui.View):
    def __init__(self, channel_id: int, enabled: bool = True):
        super().__init__(timeout=None)
        self.channel_id = channel_id

        # sync global state
        if enabled:
            active_ai_channels.add(channel_id)
        else:
            active_ai_channels.discard(channel_id)

        self.toggle_button = discord.ui.Button(
            label="Disable Otis" if enabled else "Enable Otis",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=f"toggle_ai_{channel_id}",
        )
        self.toggle_button.callback = self.on_toggle_clicked
        self.add_item(self.toggle_button)

    async def on_toggle_clicked(self, interaction: discord.Interaction):
        user = interaction.user

        if not isinstance(user, discord.Member) or not any(
            r.id in AI_CONTROL_ROLES for r in user.roles
        ):
            await interaction.response.send_message(
                "❌ Only STARZ staff can toggle Otis in this ticket.",
                ephemeral=True,
            )
            return

        if self.channel_id in active_ai_channels:
            active_ai_channels.discard(self.channel_id)
            self.toggle_button.label = "Enable Otis"
            self.toggle_button.style = discord.ButtonStyle.success
            msg = "🟢 Otis disabled for this ticket."
        else:
            active_ai_channels.add(self.channel_id)
            self.toggle_button.label = "Disable Otis"
            self.toggle_button.style = discord.ButtonStyle.danger
            msg = "🔴 Otis enabled for this ticket."

        try:
            await interaction.message.edit(view=self)
        except Exception as e:
            print(f"[AI-TOGGLE] Failed to edit toggle view: {e}")

        await interaction.response.send_message(msg, ephemeral=True)


# ===================== AI GREETING SENDER =====================


async def ensure_ai_control_message(
    channel: discord.TextChannel, opener: Optional[discord.Member]
) -> None:
    """Send the Otis greeting embed once per ticket."""
    print(f"[AI-TOGGLE] ensure_ai_control_message called for channel {channel.id}")

    has_existing_otis_embed = False

    # check channel history for an existing Otis embed
    try:
        async for msg in channel.history(limit=25):
            if msg.author == bot.user and msg.embeds:
                emb = msg.embeds[0]
                if emb.title == "STARZ AI ADMIN (Otis)":
                    has_existing_otis_embed = True
                    break
    except Exception as e:
        print(f"[AI-TOGGLE] Failed to inspect channel history: {e}")

    # always mark as greeted + enable Otis
    ai_greeting_sent.add(channel.id)
    active_ai_channels.add(channel.id)

    if has_existing_otis_embed:
        return

    # Build greeting view + embed
    view = AIToggleView(channel.id, enabled=True)
    embed = build_ai_greeting_embed(opener)

    await asyncio.sleep(1)

    try:
        await qsend(lambda: channel.send(embed=embed, view=view))
    except Exception as e:
        print(f"[AI-TOGGLE] Failed to send AI control message: {e}")

# ===================== PRINTPOS SAFE-RCON WRAPPER =====================

async def _run_rcon_high_priority(coro):
    """
    Temporarily pauses the printpos/TP polling while we run a high-priority RCON action.
    This prevents printpos spam from delaying/dropping bans/kicks.
    """
    try:
        was_on = is_printpos_enabled()
    except Exception:
        was_on = False

    try:
        if was_on:
            set_printpos_enabled(False)
        return await coro
    finally:
        if was_on:
            set_printpos_enabled(True)

# ===================== BAN HELPERS =====================


async def refresh_active_bans_embed() -> None:
    channel = bot.get_channel(ACTIVE_BANS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[BANS] Active bans channel {ACTIVE_BANS_CHANNEL_ID} not found.")
        return

    embed = build_active_bans_embed()

    try:
        last_messages = [
            msg
            async for msg in channel.history(limit=10)
            if msg.author == bot.user and msg.embeds
        ]
    except Exception as e:
        print(f"[BANS] Failed to fetch history: {e}")
        return

    if last_messages:
        msg = last_messages[0]
        try:
            await msg.edit(embed=embed)
            print("[BANS] Updated active bans embed.")
            return
        except Exception as e:
            print(f"[BANS] Failed to edit embed: {e}")

    try:
        await channel.send(embed=embed)
        print("[BANS] Sent new active bans embed.")
    except Exception as e:
        print(f"[BANS] Failed to send new embed: {e}")


def is_enforcement_member(member: discord.Member) -> bool:
    """
    True if this user is allowed to press Unban on admin-spawn alerts.
    Uses head admin + admin management roles from config_starz.
    """
    if not isinstance(member, discord.Member):
        return False
    return any(r.id in (HEAD_ADMIN_ID, ADMIN_MANAGEMENT_ID) for r in member.roles)


class AdminSpawnEnforcementView(discord.ui.View):
    """
    View attached to the auto-ban embed.

    We already kicked + banned automatically.
    This view mainly exists to let Head Admins UNBAN if the AI was wrong.
    """

    def __init__(self, gamertag: str):
        super().__init__(timeout=3600)
        self.gamertag = gamertag

    @discord.ui.button(label="Unban (undo auto-ban)", style=discord.ButtonStyle.success)
    async def unban_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        user = interaction.user

        if not is_enforcement_member(user):
            return await interaction.response.send_message(
                "❌ You are not authorized to use this button.",
                ephemeral=True,
            )

        await perform_unban(
            gamertag=self.gamertag,
            moderator=user,
            source="admin_spawn_review",
        )

        await interaction.response.send_message(
            f"✅ Unbanned **{self.gamertag}** and cleared the auto-ban.",
            ephemeral=True,
        )

        # Disable the button so it can't be spammed
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)


async def perform_ban(
    gamertag: str,
    discord_user: Optional[discord.Member],
    reason: str,
    moderator: Optional[discord.Member],
) -> None:
    reason_text = reason or "No reason provided."

    try:
        offense_tier, expires_at_ts, duration_text = create_ban_record(
            gamertag=gamertag,
            discord_id=discord_user.id if discord_user else None,
            reason=reason_text,
            source="slash_ban",
            moderator_id=moderator.id if moderator else None,
        )
    except Exception as e:
        print(f"[BANS] Failed to create ban record: {e}")
        return

    print(
        f"[BANS] Created ban for {gamertag} tier={offense_tier} duration={duration_text}"
    )

    cmd = f'banid "{gamertag}" "{reason_text}"'

    try:
        await _run_rcon_high_priority(rcon_send_all(cmd))
    except Exception as e:
        print(f"[BANS] RCON ban failed: {e}")

    try:
        await send_ban_log_embed(
            bot,
            BAN_LOG_CHANNEL_ID,
            gamertag=gamertag,
            discord_id=discord_user.id if discord_user else None,
            reason=reason_text,
            offense_tier=offense_tier,
            duration_text=duration_text,
            moderator=moderator,
            source="slash_ban",
        )
    except Exception as e:
        print(f"[BANS] Failed to send log embed: {e}")

    try:
        await refresh_active_bans_embed()
    except Exception as e:
        print(f"[BANS] Failed to refresh embed: {e}")


async def perform_unban(
    gamertag: str,
    moderator: Optional[discord.Member],
    source: str = "manual",
) -> None:
    try:
        changed = mark_unbanned(gamertag)
    except Exception as e:
        print(f"[BANS] mark_unbanned failed: {e}")
        changed = 0

    print(f"[BANS] Unban for {gamertag}: {changed} rows deactivated.")

    cmd = f'unban "{gamertag}"'

    try:
        await _run_rcon_high_priority(rcon_send_all(cmd))
    except Exception as e:
        print(f"[BANS] RCON unban failed: {e}")

    try:
        await send_unban_log_embed(
            bot,
            BAN_LOG_CHANNEL_ID,
            gamertag=gamertag,
            moderator=moderator,
            source=source,
        )
    except Exception as e:
        print(f"[BANS] Log embed failed: {e}")

    try:
        await refresh_active_bans_embed()
    except Exception as e:
        print(f"[BANS] Refresh after unban failed: {e}")
# ===================== PRINTPOS: AUTO-LOAD CONNECTED PLAYERS =====================

def _parse_names_from_playerlist(text: str) -> list[str]:
    if not text:
        return []

    raw = str(text).strip()

    # Strip wrapping quotes if present
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()

    # Unescape literal \n etc if present
    if "\\n" in raw or "\\t" in raw or '\\"' in raw:
        try:
            raw = raw.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass

    try:
        data = json.loads(raw)
        # Sometimes it's JSON-of-a-string (double encoded)
        if isinstance(data, str):
            data = json.loads(data)
    except Exception as e:
        print(f"[STARZ-PRINTPOS] playerlist JSON parse failed: {e}")
        print(f"[STARZ-PRINTPOS] raw_head={raw[:120]!r}")
        return []

    names: list[str] = []
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            name = row.get("DisplayName") or row.get("displayName") or row.get("Name")
            if not name:
                continue
            s = str(name).strip()
            if s:
                names.append(s)

    # de-dupe, keep order
    out: list[str] = []
    seen = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)

    return out






@tasks.loop(seconds=PLAYERLIST_REFRESH_SECONDS)
async def printpos_player_refresh_loop():
    await bot.wait_until_ready()

    if not RCON_ENABLED:
        return

    for server_key in RCON_CONFIGS.keys():
        try:
            resp = await run_rcon_command("playerlist", client_key=server_key)

            text = ""
            if isinstance(resp, dict):
                text = (
                    resp.get("Response")
                    or resp.get("response")
                    or resp.get("Message")
                    or resp.get("message")
                    or ""
                )
            else:
                text = str(resp or "")

            names = _parse_names_from_playerlist(text)

            print(f"[STARZ-PRINTPOS] {server_key}: playerlist loaded {len(names)} name(s).")
            if names:
                print(f"[STARZ-PRINTPOS] {server_key}: names={names}")

            update_connected_players(server_key, names)

        except Exception as e:
            print(f"[STARZ-PRINTPOS] {server_key}: playerlist refresh failed: {e}")





@tasks.loop(minutes=5)
async def ban_expiry_loop():
    await bot.wait_until_ready()

    try:
        changed = deactivate_expired_bans()
    except Exception as e:
        print(f"[BANS] deactivate_expired_bans error: {e}")
        return

    if changed:
        print(f"[BANS] {changed} bans expired; refreshing embed.")

        try:
            await refresh_active_bans_embed()
        except Exception as e:
            print(f"[BANS] Failed to refresh after expiry: {e}")


def user_is_enforcement(member: discord.Member) -> bool:
    """
    True if this user is allowed to press Ban/Unban for admin spawn alerts.
    For now we treat HEAD_ADMIN_ID + ADMIN_MANAGEMENT_ID as enforcement.
    """
    return any(r.id in (HEAD_ADMIN_ID, ADMIN_MANAGEMENT_ID) for r in member.roles)


class BanDecisionView(discord.ui.View):
    def __init__(self, gamertag: str):
        super().__init__(timeout=3600)
        self.gamertag = gamertag

    @discord.ui.button(label="Ban (all servers)", style=discord.ButtonStyle.danger)
    async def ban_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not isinstance(interaction.user, discord.Member) or not user_is_enforcement(
            interaction.user
        ):
            return await interaction.response.send_message(
                "❌ You are not authorized to use this button.",
                ephemeral=True,
            )

        # Use your existing ban helper, track in DB + RCON
        reason = "Admin spawned high-risk items (rockets / C4 / MLRS)."
        await perform_ban(
            gamertag=self.gamertag,
            discord_user=None,
            reason=reason,
            moderator=interaction.user,
        )

        await interaction.response.send_message(
            f"🚫 **{self.gamertag}** banned on all servers.",
            ephemeral=True,
        )

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Unban / clear", style=discord.ButtonStyle.secondary)
    async def unban_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not isinstance(interaction.user, discord.Member) or not user_is_enforcement(
            interaction.user
        ):
            return await interaction.response.send_message(
                "❌ You are not authorized to use this button.",
                ephemeral=True,
            )

        await perform_unban(
            gamertag=self.gamertag,
            moderator=interaction.user,
            source="admin_spawn_unban",
        )

        await interaction.response.send_message(
            f"✅ Unban + offense cleared for **{self.gamertag}**.",
            ephemeral=True,
        )

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)


# ===================== ADMIN SPAWN ENFORCEMENT HELPERS =====================


def _parse_spawn_from_console_line(console_line: str) -> Optional[Tuple[str, int]]:
    """
    Extract (gamertag, amount) from a line like:
        [ServerVar] giving CPTA1N 6 x MLRS Rocket
    Returns None if it doesn't match.
    """
    m = re.search(r"\[ServerVar\]\s+giving\s+(\S+)\s+(\d+)\s+x\s+", console_line)
    if not m:
        return None
    gamertag = m.group(1)
    try:
        amount = int(m.group(2))
    except ValueError:
        amount = 0
    return gamertag, amount


def _format_spawn_event_line(evt: Dict[str, Any]) -> str:
    """
    Format a single spawn event as:
        Server 10 C4 20 3:00 PM
    """
    dt = datetime.fromtimestamp(evt["time_ts"])
    time_str = dt.strftime("%I:%M %p").lstrip("0")  # 03:00 PM -> 3:00 PM

    server = evt["server"]
    item = evt["item"]
    amount = evt["amount"]
    return f"{server} {item} {amount} {time_str}"
def _parse_spawn_from_console_line_full(console_line: str) -> Optional[Tuple[str, int, str]]:
    """
    Extract (gamertag, amount, item_text) from Rust Console spawn lines.
    Handles quoted names, SERVER prefix, x6 / 6x formats.
    """
    m = re.search(
        r"""
        \[ServerVar\]          # prefix
        .*?giving\s+           # anything before 'giving'
        "?(.+?)"?\s+           # gamertag (quoted or not)
        (?:x?\s*(\d+)|(\d+)\s*x)\s+  # amount: x6 OR 6 x OR 6x
        (.+)                   # item text
        """,
        console_line,
        re.IGNORECASE | re.VERBOSE,
    )

    if not m:
        return None

    gamertag = m.group(1).strip()

    amount = m.group(2) or m.group(3)
    try:
        amount = int(amount)
    except Exception:
        amount = 0

    item_text = (m.group(4) or "").strip().strip(".")
    return gamertag, amount, item_text



class AdminSpawnAlertView(discord.ui.View):
    def __init__(self, gamertag: str, admin_id: int):
        super().__init__(timeout=60 * 30)  # 30 minutes
        self.gamertag = gamertag
        self.admin_id = admin_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only head admins / admin management can press buttons
        if not isinstance(interaction.user, discord.Member):
            return False
        if not any(r.id in ADMIN_ENFORCEMENT_ROLE_IDS for r in interaction.user.roles):
            await interaction.response.send_message(
                "❌ Only head admins / admin management can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Ban (all servers)",
        style=discord.ButtonStyle.danger,
        custom_id="admin_spawn_ban",
    )
    async def ban_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)

        reason_text = "Admin spawned high-risk items (C4 / rockets / MLRS)."
        try:
            await perform_ban(
                gamertag=self.gamertag,
                discord_user=None,
                reason=reason_text,
                moderator=interaction.user,
            )
            await interaction.followup.send(
                f"✅ Banned **{self.gamertag}** on all servers.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Failed to ban {self.gamertag}: `{e}`",
                ephemeral=True,
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(
        label="Unban / clear",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_spawn_unban",
    )
    async def unban_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            await perform_unban(
                gamertag=self.gamertag,
                moderator=interaction.user,
                source="admin_spawn_unban",
            )
            await interaction.followup.send(
                f"✅ Cleared flagged spawn for **{self.gamertag}** (unban + logs).",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Failed to unban {self.gamertag}: `{e}`",
                ephemeral=True,
            )
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.message.edit(view=self)


async def handle_spawn_enforcement_for_event(
    *,
    admin_id: int,
    server_key: str,
    server_name: str,
    matched_item: str,
    console_line: str,
    created_at_ts: float,
) -> None:
    """
    Called from the RCON watcher whenever a registered admin spawns a HIGH_RISK_SPAWN_ITEMS item.

    - Kicks them from the server they did it on.
    - Creates a ban record in the STARZ bans DB.
    - Sends an in-game ban command.
    - Sends a single embed with Ban / Unban buttons to the enforcement channel.
    """
    from config_starz import ADMIN_ENFORCEMENT_CHANNEL_ID

    global TRACKER_DISABLED_UNTIL

    # Respect temporary disable window
    now = datetime.utcnow()
    if TRACKER_DISABLED_UNTIL and now < TRACKER_DISABLED_UNTIL:
        return

    # Try to parse the line for gamertag + amount
    parsed = _parse_spawn_from_console_line(console_line)
    gamertag: Optional[str] = None
    amount: int = 0

    if parsed:
        gamertag, amount = parsed

    # Lookup basic info for DB / logs (gives us main GT + Discord ID)
    info = fetch_admin_basic(admin_id)  # from admin_monitor.py
    discord_id = info["discord_id"] if info else None

    # If we couldn't parse the gamertag from the console line (e.g. kit call),
    # fall back to the admin's main gamertag from the monitor DB.
    if gamertag is None:
        if info and info.get("main_gamertag"):
            gamertag = info["main_gamertag"]
        else:
            gamertag = "UNKNOWN"

    # ======== Kick + ban on that server ========
    if RCON_ENABLED:
        reason = f"High-risk admin spawn: {matched_item} x{amount} on {server_name}"

        try:
            # 1) KICK from that server
            await run_rcon_command(
                f'kick "{gamertag}" "FLAGGED ADMIN SPAWN (C4/Rockets/MLRS)"',
                client_key=server_key,
            )
            print(f"[SPAWN-ENFORCE] Kicked {gamertag} on {server_key} for flagged spawn.")
        except Exception as e:
            print(f"[SPAWN-ENFORCE] Kick failed for {gamertag}: {e}")

        try:
            # 2) BAN RECORD in SQLite
            offense_tier, expires_at_ts, duration_text = create_ban_record(
                gamertag=gamertag,
                discord_id=discord_id,
                reason=reason,
                source="auto_admin_spawn_enforce",
                moderator_id=None,
            )
            print(
                f"[SPAWN-ENFORCE] Ban record created for {gamertag} "
                f"(tier {offense_tier}, duration={duration_text})."
            )
        except Exception as e:
            print(f"[SPAWN-ENFORCE] create_ban_record failed for {gamertag}: {e}")

        try:
            # 3) IN-GAME BAN via RCON (Rust Console Edition uses banid)
            ban_cmd = f'banid "{gamertag}" "FLAGGED ADMIN SPAWN (C4/Rockets/MLRS)"'
            await run_rcon_command(ban_cmd, client_key=server_key)
            print(f"[SPAWN-ENFORCE] Sent in-game ban command for {gamertag} on {server_key}: {ban_cmd}")
        except Exception as e:
            print(f"[SPAWN-ENFORCE] RCON ban failed for {gamertag}: {e}")

    # ======== Build & send review embed ========
    profile = get_admin_profile(admin_id)
    embed_discord_id = profile["discord_id"] if profile else discord_id

    channel = bot.get_channel(ADMIN_ENFORCEMENT_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[SPAWN-ENFORCE] Enforcement channel {ADMIN_ENFORCEMENT_CHANNEL_ID} not found.")
        return

    dt = datetime.fromtimestamp(created_at_ts, tz=timezone.utc)
    time_str = dt.strftime("%Y-%m-%d %I:%M %p").lstrip("0")

    if amount <= 0:
        amount = 1

    desc_lines = [
        f"**Server:** `{server_name}`",
        f"**Gamertag:** `{gamertag}`",
        f"**Item:** `{matched_item}` x{amount}",
        f"**Time (UTC):** `{time_str}`",
        "",
        "_Otis auto-kicked and banned this admin for spawning high-risk items._",
    ]

    embed = discord.Embed(
        title="🚨 High-Risk Admin Spawn Detected",
        description="\n".join(desc_lines),
        color=0xE02424,
        timestamp=dt,
    )

    if embed_discord_id:
        embed.add_field(name="Admin", value=f"<@{embed_discord_id}>", inline=False)

    view = AdminSpawnAlertView(gamertag=gamertag, admin_id=admin_id)

    try:
        await channel.send(embed=embed, view=view)
        print(f"[SPAWN-ENFORCE] Sent alert for admin_id={admin_id}, GT={gamertag}")
    except Exception as e:
        print(f"[SPAWN-ENFORCE] Failed to send alert embed: {e}")



# ===================== ADMIN MONITOR =====================


@tasks.loop(hours=1)
async def admin_monitor_cleanup_loop():
    await bot.wait_until_ready()

    try:
        deleted = prune_old_admin_events()
        if deleted:
            print(f"[ADMIN-MONITOR] Pruned {deleted} old events.")
    except Exception as e:
        print(f"[ADMIN-MONITOR] Cleanup error: {e}")


async def handle_admin_monitor_log(message: discord.Message) -> None:
    if not isinstance(message.channel, discord.TextChannel):
        return

    parts: List[str] = []

    if message.content:
        parts.append(message.content)

    for e in message.embeds:
        if e.title:
            parts.append(e.title)
        if e.description:
            parts.append(e.description)
        for f in e.fields:
            if f.name:
                parts.append(str(f.name))
            if f.value:
                parts.append(str(f.value))

    content = "\n".join(parts).strip()

    if not content:
        return

    matching_admin_ids = find_matching_admin_ids_from_text(content)
    if not matching_admin_ids:
        return

    ch_id = message.channel.id

    if ch_id in PLAYER_FEED_CHANNEL_IDS:
        event_type = "join"
        detail = "Joined server"
    elif ch_id in ADMIN_FEED_CHANNEL_IDS:
        event_type = "spawn"
        detail = content
    else:
        return

    server = server_name_for_channel(ch_id)

    for admin_id in matching_admin_ids:
        record_admin_event(
            admin_id=admin_id,
            event_type=event_type,
            server_name=server,
            detail=detail,
        )

        await update_admin_log_for_admin(
            bot,
            admin_id,
            ADMIN_MONITOR_LOG_CHANNEL_ID,
        )


# ===================== SHOP LOG HANDLER =====================


async def handle_shop_log_message(message: discord.Message) -> None:
    content = (message.content or "").strip()

    # --- Detect unban purchases ---
    if UNBAN_SHOP_PREFIX in content:
        try:
            after = content.split(UNBAN_SHOP_PREFIX, 1)[1].strip()
            gamertag: Optional[str] = None

            # Format: UNBAN_PURCHASEIGN= <@1234567890>
            if after.startswith("<@") and after.endswith(">"):
                inner = after[2:-1].strip()  # strip <@ and >
                if inner.startswith("!"):
                    inner = inner[1:]

                try:
                    discord_id_int = int(inner)
                except ValueError:
                    print(f"[BANS] Could not parse Discord ID from {after!r}")
                    discord_id_int = None

                member = (
                    message.guild.get_member(discord_id_int)
                    if (discord_id_int is not None and message.guild is not None)
                    else None
                )

                if member is not None:
                    # Use Discord display name as IGN
                    gamertag = member.display_name or member.name
                else:
                    print(
                        f"[BANS] Could not resolve member for Discord ID {inner}; "
                        "skipping auto-unban."
                    )
            else:
                # Fallback: assume they sent a plain gamertag
                gamertag = after

            if gamertag:
                print(f"[BANS] Detected shop unban for gamertag={gamertag!r}")
                await perform_unban(gamertag, moderator=None, source="shop_unban")

        except Exception as e:
            print(f"[BANS] Failed to parse shop unban line: {e}")

    # We no longer forward shop logs into KAOS;
    # KAOS already handles its own commands.
    return


# ===================== RCON CONSOLE LINE HANDLER =====================

async def handle_rcon_console_line(
    server_key: str, msg_text: str, created_at_ts: float
) -> None:
    """
    Called for each console line...
    """

    # 0) Teleport / TP zone system: let it inspect every console line
    try:
        if is_printpos_enabled():
            await handle_printpos_console_line(server_key, msg_text)
    except Exception as e:
        print(f"[PRINTPOS:{server_key}] error handling console line: {e}")

    # 1) Admin promotion watch (runs on every console line)
    await maybe_handle_admin_promotion(
        bot=bot,
        server_name=server_key,
        msg_text=msg_text,
        created_at_ts=created_at_ts,
    )


    # 2) Find any registered admins mentioned in this line
    matching_admin_ids = find_matching_admin_ids_from_text(msg_text)
    if not matching_admin_ids:
        return
    server_name = server_key


    # 3) Admin monitor log update
    await log_admin_activity_for_ids(
        bot=bot,
        admin_ids=matching_admin_ids,
        event_type="spawn",
        server_name=server_name,
        detail=msg_text,
    )


    # 5) High-risk spawn enforcement (ONLY on real spawn/kit lines)
    if RCON_ENABLED:
        admin_ids = find_matching_admin_ids_from_text(msg_text)
        if admin_ids:
            lt = msg_text.lower()

            # 🔍 TEMP DEBUG — show raw spawn lines
            if "servervar" in lt or "giving" in lt:
                print(f"[SPAWN-RAW] {msg_text}")

            # ---- Case 1: real item spawn line ----
            parsed_full = _parse_spawn_from_console_line_full(msg_text)
            print(f"[SPAWN-DEBUG] parsed_full={parsed_full}")

            if parsed_full:
                _gt, _amt, item_text = parsed_full

                item_key = item_text.lower().strip()
                matched_item = None
                for hr in HIGH_RISK_SPAWN_ITEMS:
                    if hr.lower() in item_key:
                        matched_item = hr
                        break

                if matched_item:
                    for admin_id in admin_ids:
                        if is_admin_immune(admin_id):
                            continue

                        await handle_spawn_enforcement_for_event(
                            admin_id=admin_id,
                            server_key=server_key,
                            server_name=server_name,
                            matched_item=matched_item,
                            console_line=msg_text,
                            created_at_ts=created_at_ts,
                        )
                    return

            # ---- Case 2: kit claim success line (KITMANAGER) ----
            if "[kitmanager]" in lt and "successfully gave" in lt:
                m_kit = re.search(
                    r"\[kitmanager\].*?\[([^\]]+)\]",
                    msg_text,
                    re.IGNORECASE,
                )
                kit_name = (m_kit.group(1) if m_kit else "").strip().lower()

                if kit_name and kit_name in {k.lower() for k in HIGH_RISK_KITS}:
                    for admin_id in admin_ids:
                        if is_admin_immune(admin_id):
                            continue

                        await handle_spawn_enforcement_for_event(
                            admin_id=admin_id,
                            server_key=server_key,
                            server_name=server_name,
                            matched_item=kit_name,
                            console_line=msg_text,
                            created_at_ts=created_at_ts,
                        )
                    return


async def rcon_console_watch(server_key: str, host: str, port: int, password: str):
    """
    Connects to a Rust WebRCON stream and reads console logs forever.
    On network/socket errors, it reconnects.
    Handler errors (like our own Python code) are logged but do NOT kill the socket.
    """
    url = f"ws://{host}:{port}/{password}/"
    print(f"[RCON-WATCH:{server_key}] Watcher starting for {url}")

    while True:
        if not RCON_ENABLED:
            # Master switch off – don't spam reconnects
            await asyncio.sleep(30)
            continue

        try:
            print(f"[RCON-WATCH:{server_key}] Connecting to {url} ...")
            async with websockets.connect(url, ping_interval=None) as ws:
                print(
                    f"[RCON-WATCH:{server_key}] ✅ Connected, streaming console logs..."
                )

                # Re-enable printpos/TP after successful reconnect
                try:
                    set_printpos_enabled(True)
                except Exception:
                    pass


                async for raw in ws:
                    # 1) Parse JSON safely
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue

                    # 2) Extract console message text
                    try:
                        msg_text = (data.get("Message") or "").replace(
                            "\u0000", ""
                        ).strip()
                        if not msg_text:
                            continue

                        ident = data.get("Identifier")

                        # Always let printpos see response lines too (ident != 0)
                        if ident not in (0, None):
                            try:
                                if is_printpos_enabled():
                                    await handle_printpos_console_line(server_key, msg_text)
                            except Exception as e:
                                print(f"[PRINTPOS:{server_key}] error handling ident!=0 line: {e}")
                            continue



                        created_at_ts = time.time()

                        # use new unified handler
                        await handle_rcon_console_line(
                            server_key=server_key,
                            msg_text=msg_text,
                            created_at_ts=created_at_ts,
                        )
                    except Exception as e:
                        print(f"[RCON-WATCH:{server_key}] Handler error: {e}")
                        # keep listening on same ws

        except Exception as e:
            # 🔴 OUTER except: WebSocket / network errors
            error_text = str(e)
            print(f"[RCON-WATCH:{server_key}] WebRCON connection error: {error_text}")

            # Try to notify Discord
            try:
                await send_rcon_disconnect_alert(bot, server_key, error_text)
            except Exception as alert_err:
                print(f"[RCON-ALERT] Failed to send disconnect embed: {alert_err}")

            # Wait a bit before reconnecting
            await asyncio.sleep(10)


async def watch_rcon_console(server_key: str, host: str, port: int, password: str):
    """
    Small wrapper so on_ready() can call watch_rcon_console()
    while the main implementation lives in rcon_console_watch().
    """
    await rcon_console_watch(server_key, host, port, password)


# ===================== BOT READY EVENT =====================
# ===================== BOT READY EVENT =====================
@bot.event
async def on_ready():
    global rcon_failures
    global RCON_WATCH_TASKS
    global PRINTPOS_SYSTEM_STARTED
    global DISCORD_SEND_WORKER_STARTED

    ban_db_ok = False
    admin_db_ok = False
    slash_count = 0

    # Keep this list global so startup embed can show it
    rcon_failures = []

    # ===================== STARTUP LOGGING / FILE CHECKS =====================
    write_startup_log("===== STARZ BOT STARTUP =====")

    check_env_var("DISCORD_BOT_TOKEN")
    check_env_var("OPENAI_API_KEY")
    check_env_var("OPENAI_MODEL")

    check_file_exists("config_starz.py", required=True)
    check_file_exists("ticket_ai.py", required=True)
    check_file_exists("ticket_helpers.py", required=True)
    check_file_exists("workflows.py", required=True)
    check_file_exists("zorp_lookup.py", required=True)
    check_file_exists("kit_helpers.py", required=True)
    check_file_exists("rcon_web.py", required=True)
    check_file_exists("bans.py", required=True)
    check_file_exists("admin_monitor.py", required=True)

    check_file_exists("kit_claims.txt", required=True)
    check_file_exists("configzorp_guide.txt", required=False)
    check_file_exists("configraffle_guide.txt", required=False)  # ✅ add this
    check_file_exists("starz_bans.db", required=False)

    write_startup_log("Core file + DB checks complete.")

    # ===================== NORMAL READY LOGIC =====================
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # Start Discord send queue worker once
    if not DISCORD_SEND_WORKER_STARTED:
        bot.loop.create_task(_discord_send_worker())
        DISCORD_SEND_WORKER_STARTED = True

    # Slash sync
    try:
        synced = await bot.tree.sync()
        slash_count = len(synced)
        print(f"✅ Synced {slash_count} slash command(s).")
    except Exception as e:
        print(f"⚠️ Slash command sync error: {e}")

    # Init DBs
    try:
        init_ban_db()
        ban_db_ok = True
        print("[BANS] DB ready.")
    except Exception as e:
        print(f"[BANS] DB init failed: {e}")

    try:
        init_admin_monitor_db()
        admin_db_ok = True
        print("[ADMIN-MONITOR] Tables ready.")
    except Exception as e:
        print(f"[ADMIN-MONITOR] Init failed: {e}")

    # Background loops (safe on reconnect)
    try:
        if not ban_expiry_loop.is_running():
            ban_expiry_loop.start()
    except Exception as e:
        print(f"[BANS] ban_expiry_loop start failed: {e}")

    try:
        if not admin_monitor_cleanup_loop.is_running():
            admin_monitor_cleanup_loop.start()
    except Exception as e:
        print(f"[ADMIN-MONITOR] cleanup loop start failed: {e}")

    # ===============================
    # TP / PRINTPOS SYSTEM (ONE-TIME)
    # ===============================
    if not PRINTPOS_SYSTEM_STARTED:
        try:
            init_printpos_system(tp_send_rcon)
            start_printpos_polling()

            # ✅ DEBUG: confirm zones loaded
            try:
                z = list(get_all_zones())
                print(f"[TP-ZONES] Loaded {len(z)} zone rows.")
                if z:
                    print(f"[TP-ZONES] Sample: {z[0]}")
            except Exception as e:
                print(f"[TP-ZONES] Failed to read zones: {e}")

            if not printpos_player_refresh_loop.is_running():
                printpos_player_refresh_loop.start()

            set_printpos_enabled(True)

            PRINTPOS_SYSTEM_STARTED = True
            print("[STARZ-PRINTPOS] Teleport / printpos system initialized.")
        except Exception as e:
            print(f"[STARZ-PRINTPOS] Failed to init TP system: {e}")

    # ===============================
    # RCON HEALTH CHECK (ONCE)
    # ===============================
    if not RCON_ENABLED:
        print("[RCON] Disabled by master switch.")
        rcon_failures.append("RCON master switch disabled; skipping check.")
    else:
        try:
            failures = await check_rcon_health_on_startup()
            if failures:
                rcon_failures.extend(list(failures))
        except Exception as e:
            print(f"[RCON] Error during startup health check: {e}")
            rcon_failures.append(f"RCON health check error: {e}")

    # Active bans embed
    try:
        await refresh_active_bans_embed()
    except Exception as e:
        print(f"[BANS] Unable to refresh active bans at startup: {e}")


    # ===============================
    # STARTUP EMBED
    # ===============================
    try:
        log_channel = bot.get_channel(1325974275504738415)
        if not isinstance(log_channel, discord.TextChannel):
            print("[STARTUP] Status channel 1325974275504738415 not found.")
        else:
            now = datetime.now(UTC)
            ts = int(now.timestamp())

            rcon_switch_text = "✅ Enabled" if RCON_ENABLED else "❌ Disabled"

            if not rcon_failures or (
                len(rcon_failures) == 1
                and "skipping check" in (rcon_failures[0] or "").lower()
            ):
                status_line = "All systems operational."
                systems_line = "0 system failures."
            else:
                status_line = "Some systems reported issues."
                systems_line = f"{len(rcon_failures)} failure(s)."

            embed = discord.Embed(
                title="🟢 OTIS IS BACK ONLINE",
                description="\n".join(
                    [
                        status_line,
                        systems_line,
                        "",
                        f"**RCON Master Switch:** {rcon_switch_text}",
                    ]
                ),
                color=0x2ECC71,
            )

            if not startup_file_failures:
                embed.add_field(
                    name="File Check",
                    value="✅ All files loaded correctly.",
                    inline=False,
                )
            else:
                fail_list = "\n".join(f"• {f}" for f in startup_file_failures)
                embed.add_field(
                    name="File Check",
                    value=f"❌ Some files failed to load:\n{fail_list}",
                    inline=False,
                )

            embed.add_field(
                name="Startup Time",
                value=f"<t:{ts}:R>",
                inline=False,
            )

            embed.add_field(
                name="Commands & AI",
                value=(
                    f"• **Prefix:** `!`\n"
                    f"• **Slash commands:** {slash_count}\n"
                    f"• **AI Model:** `{OPENAI_MODEL}`"
                ),
                inline=False,
            )

            try:
                total_rcon = len(rcon_manager.clients)
            except Exception:
                total_rcon = len(RCON_CONFIGS)

            zorp_count = len(ZORP_FEED_CHANNEL_IDS)
            player_feeds = len(PLAYER_FEED_CHANNEL_IDS)
            admin_feeds = len(ADMIN_FEED_CHANNEL_IDS)

            embed.add_field(
                name="Config Summary",
                value=(
                    f"• **RCON servers configured:** {total_rcon}\n"
                    f"• **ZORP feed channels:** {zorp_count}\n"
                    f"• **Player feed channels:** {player_feeds}\n"
                    f"• **Admin feed channels:** {admin_feeds}"
                ),
                inline=False,
            )

            ban_db_status = "✅ Bans DB" if ban_db_ok else "❌ Bans DB"
            admin_db_status = "✅ Admin Monitor DB" if admin_db_ok else "❌ Admin Monitor DB"

            embed.add_field(
                name="Database Status",
                value=f"{ban_db_status} • {admin_db_status}",
                inline=False,
            )

            kit_count = len(kit_claims)
            zorp_guide_exists = os.path.exists("configzorp_guide.txt")
            zorp_guide_state = "✅ Found" if zorp_guide_exists else "⚠ Missing"

            embed.add_field(
                name="Kits & Guides",
                value=(
                    f"• **Kit configs loaded:** {kit_count}\n"
                    f"• **ZORP guide file:** {zorp_guide_state}"
                ),
                inline=False,
            )

            if rcon_failures and RCON_ENABLED:
                notes = "\n".join(rcon_failures[:3])
                embed.add_field(
                    name="RCON Notes",
                    value=notes,
                    inline=False,
                )

            try:
                embed.set_thumbnail(url=bot.user.display_avatar.url)
            except Exception:
                pass

            await qsend(lambda: log_channel.send(embed=embed))
            print(f"[STARTUP] Embed sent to {log_channel.id}.")

    except Exception as e:
        print(f"[STARTUP] Error sending embed: {e}")

    # ===============================
    # RCON CONSOLE WATCHERS (START ONCE)
    # ===============================
    if RCON_ENABLED and not RCON_WATCH_TASKS:
        try:
            for key, cfg in RCON_CONFIGS.items():
                task = asyncio.create_task(
                    watch_rcon_console(
                        server_key=key,
                        host=cfg["host"],
                        port=cfg["port"],
                        password=cfg["password"],
                    )
                )
                RCON_WATCH_TASKS.append(task)

            print(f"[RCON-WATCH] Started {len(RCON_WATCH_TASKS)} console watcher task(s).")
        except Exception as e:
            print(f"[RCON-WATCH] Failed to start console watchers: {e}")

# ===================== SLASH COMMANDS =====================

# ----- TP / printpos slash helpers -----
# Stores the last TP spawn_points list you used (per Discord user)
# user_id -> List[Tuple[float,float,float]]
TP_SPAWN_CLIPBOARD: dict[int, list[tuple[float, float, float]]] = {}

TP_TYPE_CHOICES = [
    app_commands.Choice(name="Launch Site", value=TPType.LAUNCHSITE.value),
    app_commands.Choice(name="Airfield", value=TPType.AIRFIELD.value),
    app_commands.Choice(name="Junkyard", value=TPType.JUNKYARD.value),
    app_commands.Choice(
        name="Oxums Gas Station", value=TPType.OXUMS_GAS_STATION.value
    ),
    app_commands.Choice(
        name="Water Treatment Plant", value=TPType.WATER_TREATMENT_PLANT.value
    ),
    app_commands.Choice(name="Bandit Camp", value=TPType.BANDIT_CAMP.value),
    app_commands.Choice(name="Outpost", value=TPType.OUTPOST.value),
    app_commands.Choice(name="Fishing Village", value=TPType.FISHING_VILLAGE.value),
    app_commands.Choice(
        name="Military Tunnels", value=TPType.MILLITARY_TUNNELS.value
    ),
    app_commands.Choice(
        name="Abandoned Military Base",
        value=TPType.ABANDONED_MILITARY_BASE.value,
    ),
]

# Default enter/exit messages per TP type.
# If /tp-set-zone is called without custom messages, these will be used.
DEFAULT_TP_ENTER_MESSAGES: Dict[str, str] = {
    TPType.AIRFIELD.value: (
        "<b><size=175%><color=#fb00d1>Heading to Airfield<b>\n"
        "<b><size=175%><color=#fb00d1>WAIT 10 - 20 SECONDS<b>"
    ),
    # You can add more defaults later
}

DEFAULT_TP_EXIT_MESSAGES: Dict[str, str] = {
    TPType.AIRFIELD.value: "<b><size=175%><color=#fb00d1>LEFT AIRFIELD TP<b>",
}


# ----- TP / printpos slash helpers -----

@bot.tree.command(
    name="tp-debug-force-names",
    description="[TEMP] Feed names into TP/printpos.",
)
@app_commands.describe(
    server_key="s1..s10",
    names="Comma-separated names, e.g. bob,alice",
)
async def tp_debug_force_names(
    interaction: discord.Interaction,
    server_key: str,
    names: str,
):
    if not isinstance(interaction.user, discord.Member) or not any(
        r.id in AI_CONTROL_ROLES for r in interaction.user.roles
    ):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    name_list = [n.strip() for n in names.split(",") if n.strip()]
    if not name_list:
        await interaction.response.send_message("❌ No valid names.", ephemeral=True)
        return

    update_connected_players(server_key, name_list)

    await interaction.response.send_message(
        f"✅ Loaded {len(name_list)} name(s) for `{server_key}`.",
        ephemeral=True,
    )



@bot.tree.command(
    name="tp-printpos-toggle",
    description="Enable or disable the automatic TP / printpos system.",
)
@app_commands.describe(enabled="True = turn TP system on, False = turn it off.")
async def tp_printpos_toggle(interaction: discord.Interaction, enabled: bool):
    # Permission check
    if not isinstance(interaction.user, discord.Member) or not any(
        r.id in AI_CONTROL_ROLES for r in interaction.user.roles
    ):
        await interaction.response.send_message(
            "❌ You do not have permission to change TP settings.",
            ephemeral=True,
        )
        return

    # Toggle system
    set_printpos_enabled(enabled)

    state = "ENABLED ✅" if enabled else "DISABLED ❌"
    await interaction.response.send_message(
        f"🚀 TP / printpos system is now **{state}**.",
        ephemeral=True,
    )
# ==========================
# TP DELETE (DYNAMIC MENU)
# ==========================
async def tp_type_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """
    Autocomplete TP types, but ONLY show types that currently exist in tp_zones.json.
    """
    types = get_configured_tp_types()
    cur = (current or "").strip().lower()

    out: List[app_commands.Choice[str]] = []
    for t in types:
        if not cur or cur in t.lower():
            out.append(app_commands.Choice(name=t, value=t))

    return out[:25]

async def tp_slot_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[int]]:
    # Get the currently selected tp_type from the interaction
    tp_type_value = None
    try:
        tp_type_value = interaction.namespace.tp_type
    except Exception:
        tp_type_value = None

    if not tp_type_value:
        return []

    slots = get_configured_slots(str(tp_type_value))
    cur = (current or "").strip()

    # Filter by typed digits
    if cur.isdigit():
        slots = [s for s in slots if str(s).startswith(cur)]

    slots = slots[:25]
    return [app_commands.Choice(name=str(s), value=int(s)) for s in slots]



# ==========================
# TP DELETE (DELETE WHOLE TYPE)
# ==========================

@bot.tree.command(
    name="tp-delete",
    description="Delete an entire TP type (all slots, MAIN + SPAWN zones).",
)
@app_commands.describe(
    tp_type="Only TP types that currently exist will appear",
)
@app_commands.autocomplete(tp_type=tp_type_autocomplete)
async def tp_delete(interaction: discord.Interaction, tp_type: str):
    await interaction.response.defer(ephemeral=True)

    # Convert string -> TPType
    try:
        tp_enum = TPType(tp_type)
    except ValueError:
        await interaction.followup.send(f"❌ Invalid TP type `{tp_type}`.", ephemeral=True)
        return

    # Delete ALL bot-side TP zones
    removed = delete_tp_type(tp_enum)

    # Build EXACT names we expect
    names: list[str] = []
    names.append(f"{tp_enum.value} MAIN")

    # You only ever create up to 5 spawns in tp-set-zone
    for i in range(1, 6):
        names.append(f"{tp_enum.value} SPAWN #{i}")

    # Build command list (try both variants)
    cmds: list[str] = []
    for name in names:
        cmds.append(f'zones.deletecustomzone "{name}"')
        cmds.append(f'zones.removecustomzone "{name}"')  # fallback alias on some builds

    try:
        for server_key in ZONE_RCON_SERVER_KEYS:
            for cmd in cmds:
                try:
                    print(f"[TP-DELETE:{server_key}] {cmd}")
                    await asyncio.wait_for(tp_send_rcon(server_key, cmd), timeout=6.0)
                except Exception as e:
                    print(f"[TP-DELETE:{server_key}] Failed: {cmd!r}: {e}")
                await asyncio.sleep(1)

        await interaction.followup.send(
            f"✅ Deleted **{tp_enum.value}** ({removed} slot(s)) + MAIN + SPAWN zones.",
            ephemeral=True,
        )

    except Exception as e:
        try:
            await interaction.followup.send(
                f"❌ tp-delete failed: `{e}`",
                ephemeral=True,
            )
        except Exception:
            pass

@bot.tree.command(
    name="tp-show-clipboard",
    description="Show the last TP spawn points you saved with /tp-set-zone.",
)
async def tp_show_clipboard(interaction: discord.Interaction):
    # Only AI control roles
    if not isinstance(interaction.user, discord.Member) or not any(
        r.id in AI_CONTROL_ROLES for r in interaction.user.roles
    ):
        await interaction.response.send_message(
            "❌ You do not have permission to view TP clipboard.",
            ephemeral=True,
        )
        return

    spawn_points = TP_SPAWN_CLIPBOARD.get(interaction.user.id)
    if not spawn_points:
        await interaction.response.send_message(
            "📋 **TP Clipboard is empty**\n"
            "Run **/tp-set-zone** first to save spawn points.",
            ephemeral=True,
        )
        return

    lines = []
    for idx, (x, y, z) in enumerate(spawn_points, start=1):
        lines.append(f"`#{idx}` → **X:** `{x:.2f}` **Y:** `{y:.2f}` **Z:** `{z:.2f}`")

    embed = discord.Embed(
        title="📋 TP Spawn Clipboard",
        description="\n".join(lines),
        color=0xE67E22,
    )
    embed.set_footer(
        text=f"Saved spawns: {len(spawn_points)} • Use /tp-copy-zone to reuse"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="tp-set-zone",
    description="Set a teleport zone and up to 5 teleport spawn positions.",
)
@app_commands.describe(
    tp_type="Type of teleport (Launch Site, Airfield, etc.)",
    zone_x="Zone X (where they walk to trigger the TP).",
    zone_y="Zone Y.",
    zone_z="Zone Z.",
    spawn1="Spawn 1 position as: x y z",
    spawn2="Spawn 2 position as: x y z (optional)",
    spawn3="Spawn 3 position as: x y z (optional)",
    spawn4="Spawn 4 position as: x y z (optional)",
    spawn5="Spawn 5 position as: x y z (optional)",
)
@app_commands.choices(tp_type=TP_TYPE_CHOICES)
async def tp_set_zone(
    interaction: discord.Interaction,
    tp_type: app_commands.Choice[str],
    zone_x: float,
    zone_y: float,
    zone_z: float,
    spawn1: str,
    spawn2: Optional[str] = None,
    spawn3: Optional[str] = None,
    spawn4: Optional[str] = None,
    spawn5: Optional[str] = None,
):
    """
    Set a single trigger zone for a monument type and define up to 5 teleport
    spawn locations.

    This does ALL of the following:
    - Updates the bot-side TP zone config (tp_zones.json)
    - Uses per-type enter / exit messages
    - Automatically sends the Rust `zones.*` custom zone commands over RCON
      for each server key in ZONE_RCON_SERVER_KEYS.
    """
    # Only AI control roles
    if not isinstance(interaction.user, discord.Member) or not any(
        r.id in AI_CONTROL_ROLES for r in interaction.user.roles
    ):
        await interaction.response.send_message(
            "❌ You do not have permission to set TP zones.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)



    # Convert choice -> TPType enum
    tp_enum = TPType(tp_type.value)
    # ------------------------------
    # Determine zone color (ALWAYS defined)
    # ------------------------------
    final_color = DEFAULT_ZONE_COLORS.get(tp_enum.value, "WHITE")

    # --- IMPORTANT: raise the trigger Y by +2 (so the visible zone appears correctly in-game) ---
    zone_y_for_bot = zone_y
    zone_y_for_rust = zone_y + 0.75




    # Helper to parse "x y z" into floats
    def _parse_spawn(s: Optional[str]) -> Optional[Tuple[float, float, float]]:
        if not s:
            return None
        parts = s.replace(",", " ").split()
        if len(parts) != 3:
            return None
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            return None

    # ==========================
    # Collect spawn points
    # ==========================
    spawn_points_list: List[Tuple[float, float, float]] = []

    for s in (spawn1, spawn2, spawn3, spawn4, spawn5):
        parsed = _parse_spawn(s)
        if parsed:
            x, y, z = parsed
            y += 0.75 # safety offset
            spawn_points_list.append((x, y, z))

    if not spawn_points_list:
        await interaction.followup.send(
            "❌ You must provide at least one valid spawn point. "
            "Use format: `x y z` (example: `-49.42 6.1 -914.22`).",
            ephemeral=True,
        )
        return

    # Save these spawns as the user's "clipboard" so they can reuse them via /tp-copy-zone
    TP_SPAWN_CLIPBOARD[interaction.user.id] = list(spawn_points_list)

    # ==============================
    # Per-type messages (TP system)
    # ==============================
    enter_msg = TP_ENTER_MESSAGES.get(tp_enum, f"Teleporting via {tp_enum.value} zone...")
    exit_msg = TP_EXIT_MESSAGES.get(tp_enum, f"You have left the {tp_enum.value} zone.")

    # ==============================
    # Clear old zones for this type
    # ==============================
    try:
        clear_tp_type(tp_enum)
    except Exception as e:
        print(f"[TP-ZONES] Failed to clear zones for {tp_enum.value}: {e}")

    created_slots = 0

    # ==============================
    # Save TP zones (bot-side)
    # ==============================
    for slot_idx, (x, y, z) in enumerate(spawn_points_list, start=1):
        set_tp_zone(
            tp_enum,
            slot_idx,
            zone_x,
            zone_y_for_bot,
            zone_z,
            x,
            y,
            z,
            color=final_color,
            enter_message=enter_msg,
            exit_message=exit_msg,
            spawn_points=spawn_points_list,
        )
        created_slots += 1

    # ==============================
    # Build Rust zones.* commands
    # ==============================
    friendly_name = FRIENDLY_TP_NAMES.get(tp_enum, tp_enum.value.title())
    zone_name = f"{tp_enum.value} MAIN"

    # Color for this tp_type (Rust zones plugin)
    r, g, b = TP_ZONE_COLORS.get(tp_enum, (255, 255, 255))

    enter_html = (
        f"<b><size=175%><color=#fb00d1>"
        f"TELEPORTING TO {friendly_name.upper()} IN 20 SECONDS"
        f"<b>"
    )
    leave_html = (
        "<b><size=175%><color=#fb00d1>"
        "NO LONGER TELEPORTING"
        "<b>"
    )

    # ==============================
    # Build Rust zones.* commands (PHASED)
    # Delete runs ONLY before create (never after)
    # ==============================

    # --- Phase 1: DELETE everything first (MAIN + all SPAWNS) ---
    delete_cmds: List[str] = [
        f'zones.deletecustomzone "{zone_name}"',
        f'zones.removecustomzone "{zone_name}"',
    ]
    for idx in range(1, len(spawn_points_list) + 1):
        spawn_zone_name = f"{tp_enum.value} SPAWN #{idx}"
        delete_cmds += [
            f'zones.deletecustomzone "{spawn_zone_name}"',
            f'zones.removecustomzone "{spawn_zone_name}"',
        ]

    # --- Phase 2: CREATE MAIN ---
    create_main_cmds: List[str] = [
        f'zones.createcustomzone "{zone_name}" ({zone_x},{zone_y_for_rust},{zone_z}) 120 sphere 1.5 1 0 0 0 1',
    ]

    # --- Phase 3: EDIT MAIN ---
    edit_main_cmds: List[str] = [
        f'zones.editcustomzone "{zone_name}" showarea 1',
        f'zones.editcustomzone "{zone_name}" color ({r},{g},{b})',
        f'zones.editcustomzone "{zone_name}" "allowbuildingdamage" "0"',
        f'zones.editcustomzone "{zone_name}" showchatmessage 1',
        f'zones.editcustomzone "{zone_name}" entermessage "{enter_html}"',
        f'zones.editcustomzone "{zone_name}" "leavemessage" "{leave_html}"',
    ]

    # --- Phase 4: CREATE+EDIT SPAWNS (invincible destinations) ---
    spawn_cmds: List[str] = []
    for idx, (sx, sy, sz) in enumerate(spawn_points_list, start=1):
        spawn_zone_name = f"{tp_enum.value} SPAWN #{idx}"
        spawn_cmds += [
            f'zones.createcustomzone "{spawn_zone_name}" ({sx},{sy},{sz}) 120 sphere 1.5 1 0 0 0 1',
            f'zones.editcustomzone "{spawn_zone_name}" showarea 0',
            f'zones.editcustomzone "{spawn_zone_name}" "allowbuildingdamage" "0"',
            f'zones.editcustomzone "{spawn_zone_name}" showchatmessage 1',
        ]

    # ==============================
    # Send zone commands via RCON (phased)
    # ==============================
    total_sent = 0
    total_sent += await _send_zone_setup_cmds(delete_cmds, zone_name)
    total_sent += await _send_zone_setup_cmds(create_main_cmds, zone_name)  # verify happens after create inside sender
    total_sent += await _send_zone_setup_cmds(edit_main_cmds, zone_name)
    total_sent += await _send_zone_setup_cmds(spawn_cmds, zone_name)

    await interaction.followup.send(
        f"✅ Set TP zone for **{friendly_name}** with trigger at "
        f"`({zone_x}, {zone_y_for_bot}, {zone_z})` and **{created_slots}** spawn point(s).\n"
        f"📡 Sent **{total_sent}** Rust `zones.*` setup command(s) via RCON "
        f"to servers: {', '.join(ZONE_RCON_SERVER_KEYS)}.",
        ephemeral=True,
    )
@bot.tree.command(
    name="tp-copy-zone",
    description="Create a TP zone using the last spawn points you set (no need to retype coords).",
)
@app_commands.describe(
    tp_type="Which monument type you are creating the trigger zone for",
    zone_x="Trigger zone center X",
    zone_y="Trigger zone center Y",
    zone_z="Trigger zone center Z",
)
@app_commands.choices(tp_type=TP_TYPE_CHOICES)
async def tp_copy_zone(
    interaction: discord.Interaction,
    tp_type: app_commands.Choice[str],
    zone_x: float,
    zone_y: float,
    zone_z: float,
):
    # Only AI control roles
    if not isinstance(interaction.user, discord.Member) or not any(
        r.id in AI_CONTROL_ROLES for r in interaction.user.roles
    ):
        await interaction.response.send_message(
            "❌ You do not have permission to set TP zones.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Must have a clipboard from /tp-set-zone
    spawn_points_list = TP_SPAWN_CLIPBOARD.get(interaction.user.id)
    if not spawn_points_list:
        await interaction.followup.send(
            "❌ No saved spawn points yet.\n"
            "Run **/tp-set-zone** once first (it saves your spawns), then use **/tp-copy-zone**.",
            ephemeral=True,
        )
        return

    # Convert choice -> TPType enum
    tp_enum = TPType(tp_type.value)

    # Per-type messages (same behavior as tp-set-zone)
    enter_msg = TP_ENTER_MESSAGES.get(tp_enum, f"Teleporting via {tp_enum.value} zone...")
    exit_msg = TP_EXIT_MESSAGES.get(tp_enum, f"You have left the {tp_enum.value} zone.")

    # Clear old zones for this type (same behavior as tp-set-zone)
    try:
        clear_tp_type(tp_enum)
    except Exception as e:
        print(f"[TP-ZONES] Failed to clear zones for {tp_enum.value}: {e}")

    # Color
    final_color = DEFAULT_ZONE_COLORS.get(tp_enum.value, "WHITE")

    # Save TP zone(s) to tp_zones.json (same pattern as tp-set-zone)
    created_slots = 0
    for slot_idx, (dx, dy, dz) in enumerate(spawn_points_list, start=1):
        set_tp_zone(
            tp_enum,
            slot_idx,
            zone_x,
            zone_y,
            zone_z,
            dx,
            dy,
            dz,
            color=final_color,
            enter_message=enter_msg,
            exit_message=exit_msg,
            spawn_points=spawn_points_list,
        )
        created_slots += 1

    # Build & send Rust zones.* commands (reuse the exact logic from tp-set-zone)
    friendly_name = FRIENDLY_TP_NAMES.get(tp_enum, tp_enum.value.title())
    zone_name = f"{tp_enum.value} MAIN"

    # Color mapping used by tp-set-zone
    r, g, b = ZONE_COLOR_RGB.get(final_color, (1, 1, 1))

    enter_html = (enter_msg or "").replace('"', "'")
    leave_html = (exit_msg or "").replace('"', "'")

    delete_cmds = [
        f'zones.removecustomzone "{zone_name}"',
    ]

    create_main_cmds = [
        f'zones.createcustomzone "{zone_name}" ({zone_x},{zone_y},{zone_z}) 120 sphere 1.5 1 0 0 0 1',
    ]

    edit_main_cmds = [
        f'zones.editcustomzone "{zone_name}" showarea 1',
        f'zones.editcustomzone "{zone_name}" color ({r},{g},{b})',
        f'zones.editcustomzone "{zone_name}" "allowbuildingdamage" "0"',
        f'zones.editcustomzone "{zone_name}" showchatmessage 1',
        f'zones.editcustomzone "{zone_name}" entermessage "{enter_html}"',
        f'zones.editcustomzone "{zone_name}" "leavemessage" "{leave_html}"',
    ]

    spawn_cmds: List[str] = []
    for idx, (sx, sy, sz) in enumerate(spawn_points_list, start=1):
        spawn_zone_name = f"{tp_enum.value} SPAWN #{idx}"
        spawn_cmds += [
            f'zones.createcustomzone "{spawn_zone_name}" ({sx},{sy},{sz}) 120 sphere 1.5 1 0 0 0 1',
            f'zones.editcustomzone "{spawn_zone_name}" showarea 0',
            f'zones.editcustomzone "{spawn_zone_name}" "allowbuildingdamage" "0"',
            f'zones.editcustomzone "{spawn_zone_name}" showchatmessage 1',
        ]

    total_sent = 0
    total_sent += await _send_zone_setup_cmds(delete_cmds, zone_name)
    total_sent += await _send_zone_setup_cmds(create_main_cmds, zone_name)
    total_sent += await _send_zone_setup_cmds(edit_main_cmds, zone_name)
    total_sent += await _send_zone_setup_cmds(spawn_cmds, zone_name)

    await interaction.followup.send(
        f"✅ Copied TP spawns to **{friendly_name}**.\n"
        f"Trigger: `({zone_x:.2f}, {zone_y:.2f}, {zone_z:.2f})`\n"
        f"Spawns reused: `{len(spawn_points_list)}`\n"
        f"RCON cmds sent: `{total_sent}`",
        ephemeral=True,
    )



async def _send_zone_setup_cmds(zone_setup_cmds: list[str], zone_name: str) -> int:
    """
    Send the Rust zones.* setup commands to every server in ZONE_RCON_SERVER_KEYS.

    IMPORTANT (Railway-safe):
    - Broadcast per step: send ONE command to ALL servers, then wait 2 seconds.
    - After CREATE, verify the zone exists on EACH server before continuing edits.

    Returns total commands sent successfully (counting successes).
    """
    STEP_DELAY = 2.0
    total_sent = 0

    async def _broadcast(cmd: str, timeout: float = 8.0) -> list[object]:
        nonlocal total_sent
        tasks = []
        for sk in ZONE_RCON_SERVER_KEYS:
            print(f"[TP-ZONES:{sk}] Sending zone setup command: {cmd}")
            tasks.append(asyncio.wait_for(run_rcon_command(cmd, client_key=sk), timeout=timeout))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sk, res in zip(ZONE_RCON_SERVER_KEYS, results):
            if isinstance(res, Exception):
                print(f"[TP-ZONES:{sk}] Failed to send zone setup command {cmd!r}: {res}")
            else:
                total_sent += 1

        await asyncio.sleep(STEP_DELAY)
        return results

    for cmd in zone_setup_cmds:
        await _broadcast(cmd)

        # ✅ VERIFY right after CREATE, before any edits
        if cmd.startswith('zones.createcustomzone '):
            verify_cmd = "zones.listcustomzones"

            verify_tasks = []
            for sk in ZONE_RCON_SERVER_KEYS:
                verify_tasks.append(asyncio.wait_for(run_rcon_command(verify_cmd, client_key=sk), timeout=8.0))

            verify_results = await asyncio.gather(*verify_tasks, return_exceptions=True)

            for sk, resp in zip(ZONE_RCON_SERVER_KEYS, verify_results):
                if isinstance(resp, Exception):
                    raise RuntimeError(f"Zone verify failed on {sk}: {resp}")

                # run_rcon_command returns dict or None depending on your helper;
                # handle both safely:
                msg = ""
                if isinstance(resp, dict):
                    msg = (resp.get("Message") or "")
                elif resp is None:
                    msg = ""
                else:
                    try:
                        msg = str(resp)
                    except Exception:
                        msg = ""

                if zone_name not in msg:
                    raise RuntimeError(f"Zone '{zone_name}' failed to create on {sk}")

            await asyncio.sleep(STEP_DELAY)

    return total_sent



@bot.tree.command(name="ban", description="Ban a player by gamertag.")
@app_commands.describe(
    gamertag="Player's IGN",
    reason="Reason for ban",
)
async def slash_ban(
    interaction: discord.Interaction,
    gamertag: str,
    reason: str = "",
):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Only guild members can use this.",
            ephemeral=True,
        )
        return

    if not any(r.id in AI_CONTROL_ROLES for r in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    discord_user: Optional[discord.Member] = None
    if interaction.guild is not None:
        for m in interaction.guild.members:
            if m.display_name == gamertag or m.name == gamertag:
                discord_user = m
                break

    await perform_ban(
        gamertag=gamertag,
        discord_user=discord_user,
        reason=reason,
        moderator=interaction.user,
    )

    await interaction.followup.send(
        f"✅ Banned **{gamertag}**.",
        ephemeral=True,
    )



@bot.tree.command(
    name="reduceoffense",
    description="Reduce a player's ban offenses by 1 if last ban is older than 90 days.",
)
@app_commands.describe(
    gamertag="Player's in-game name (IGN)",
)
async def slash_reduceoffense(
    interaction: discord.Interaction,
    gamertag: str,
):
    """
    Reduce ban offenses by 1 for a player IF their most recent ban
    is older than 90 days.
    """
    # Must be a guild member
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Only guild members can use this.",
            ephemeral=True,
        )
        return

    # Permission check: same staff roles that can use /ban
    if not any(r.id in AI_CONTROL_ROLES for r in interaction.user.roles):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    # Defer so we can do DB work
    await interaction.response.defer(ephemeral=True)

    # 1) Try to reduce one offense if the last ban is older than 90 days
    try:
        changed = reduce_offense_for_gamertag_if_eligible(gamertag)
    except Exception as e:
        print(f"[BANS] reduce_offense_for_gamertag_if_eligible error: {e}")
        await interaction.followup.send(
            "❌ Error while trying to reduce offenses. Check logs.",
            ephemeral=True,
        )
        return

    if changed == 0:
        # Either no bans, or most recent ban is too recent
        await interaction.followup.send(
            (
                f"ℹ️ No offense was reduced for **{gamertag}**.\n"
                "Either they have no bans on record, or the most recent ban "
                "is not older than 90 days."
            ),
            ephemeral=True,
        )
        return

    # 2) Offense actually reduced: show updated ladder status
    try:
        active_row, total_bans = lookup_ban_status_by_gamertag(gamertag)
        next_tier, next_duration = describe_next_offense(total_bans)
    except Exception as e:
        print(f"[BANS] lookup/describe_next_offense failed: {e}")
        await interaction.followup.send(
            f"✅ Reduced offenses by 1 for **{gamertag}**.",
            ephemeral=True,
        )
        return

    lines = [
        f"✅ Reduced offenses by 1 for **{gamertag}**.",
        f"📊 Total bans now on record: **{total_bans}**.",
        f"⏭️ Next offense tier: **{next_tier}** ({next_duration}).",
    ]

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="disable-tracker",
    description="Temporarily disable admin spawn tracker alerts (rockets / C4 / MLRS).",
)
@app_commands.describe(
    confirm='Type "YES" to confirm disabling the tracker.',
    duration_hours="How many hours (0–6) to disable. 0 = until you run /enable-spawn-tracker.",
)
async def disable_tracker_slash(
    interaction: discord.Interaction,
    confirm: str,
    duration_hours: app_commands.Range[int, 0, 6],
):
    global TRACKER_DISABLED_UNTIL

    # Must be a guild member
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Only guild members can use this.",
            ephemeral=True,
        )
        return

    # Permission check: Head Admin / Admin Management only
    if not any(r.id in ADMIN_ENFORCEMENT_ROLE_IDS for r in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    if confirm.strip().lower() not in ("yes", "y"):
        await interaction.response.send_message(
            "❌ Tracker disable cancelled (you must type **YES** to confirm).",
            ephemeral=True,
        )
        return

    if duration_hours == 0:
        # Effectively "indefinite" until /enable-tracker is used
        TRACKER_DISABLED_UNTIL = datetime.max.replace(tzinfo=UTC)
        human = "until you run /enable-spawn-tracker"
    else:
        TRACKER_DISABLED_UNTIL = datetime.now(UTC) + timedelta(hours=duration_hours)
        human = f"for **{duration_hours}** hour(s)"

    await interaction.response.send_message(
        f"⏸️ Admin spawn tracker **DISABLED** {human}. "
        f"Flagged rockets/C4/MLRS spawns will **not** trigger alerts or bans.",
        ephemeral=True,
    )


@bot.tree.command(
    name="enable-spawn-tracker",
    description="Re-enable the admin spawn tracker alerts if they were disabled.",
)
async def enable_spawn_tracker_slash(interaction: discord.Interaction):
    global TRACKER_DISABLED_UNTIL

    # Must be a guild member
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Only guild members can use this.",
            ephemeral=True,
        )
        return

    # Same permission level as disable
    if not any(r.id in ADMIN_ENFORCEMENT_ROLE_IDS for r in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    TRACKER_DISABLED_UNTIL = None
    await interaction.response.send_message(
        "▶️ Admin spawn tracker **ENABLED**. Flagged rockets/C4/MLRS spawns will "
        "again trigger alerts and ban/unban buttons.",
        ephemeral=True,
    )


# ===================== ADMIN MONITOR COMMANDS =====================


def _has_ai_control_role(user: discord.abc.User | discord.Member) -> bool:
    """Return True if the user has one of the AI_CONTROL_ROLES."""
    if not isinstance(user, discord.Member):
        return False
    return any(r.id in AI_CONTROL_ROLES for r in user.roles)


@bot.tree.command(
    name="register",
    description="Register or update a staff member for monitoring (admin or promoter).",
)
@app_commands.describe(
    staff_type="Are they an Admin or a Promoter?",
    gamertag="Main in-game gamertag",
    member="Discord account to register",
    alt_gamertag="Optional alt account / second gamertag",
)
async def register_staff_slash(
    interaction: discord.Interaction,
    staff_type: StaffType,  # dropdown
    gamertag: str,
    member: discord.Member,
    alt_gamertag: Optional[str] = None,
):
    # Permission check: only staff with AI_CONTROL_ROLES can use this
    if not _has_ai_control_role(interaction.user):
        await interaction.response.send_message(
            "❌ You do not have permission to register staff.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # ===== ADMIN PATH =====
    if staff_type is StaffType.ADMIN:
        admin_id = register_or_update_admin(
            discord_user=member,
            main_gamertag=gamertag,
            alt_gamertag=alt_gamertag,
        )

        try:
            await update_admin_log_for_admin(
                bot,
                admin_id,
                ADMIN_MONITOR_LOG_CHANNEL_ID,
            )
            print(f"[ADMIN-MONITOR] Updated admin log for admin_id={admin_id}")
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to update admin log after register: {e}")

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Admin Registered",
                description=(
                    f"**Type:** Admin\n"
                    f"**Discord:** {member.mention}\n"
                    f"**Main GT:** `{gamertag}`\n"
                    f"**Alt GT:** `{alt_gamertag or 'None'}`\n"
                    f"**Admin ID:** `{admin_id}`\n\n"
                    "OTIS will now track this admin in feeds / RCON and has "
                    "created/updated their activity log."
                ),
                color=0x2ECC71,
            ),
            ephemeral=True,
        )
        return

    # ===== PROMOTER PATH =====
    if staff_type is StaffType.PROMOTER:
        admin_id = register_or_update_admin(
            discord_user=member,
            main_gamertag=gamertag,
            alt_gamertag=alt_gamertag,
        )

        try:
            await update_admin_log_for_admin(
                bot,
                admin_id,
                ADMIN_MONITOR_LOG_CHANNEL_ID,
            )
            print(
                f"[ADMIN-MONITOR] Updated admin log for promoter admin_id={admin_id}"
            )
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to update promoter log after register: {e}")

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Promoter Registered",
                description=(
                    f"**Type:** Promoter\n"
                    f"**Discord:** {member.mention}\n"
                    f"**Main GT:** `{gamertag}`\n"
                    f"**Alt GT:** `{alt_gamertag or 'None'}`\n"
                    f"**Admin ID:** `{admin_id}`\n\n"
                    "This promoter is now tracked in the admin monitor database. "
                    "Promoter vs admin behavior is still controlled by their Discord roles."
                ),
                color=0xF1C40F,
            ),
            ephemeral=True,
        )
        return


@bot.tree.command(
    name="register-admin",
    description="Alias of /register for Admins (uses the same logic).",
)
@app_commands.describe(
    gamertag="Admin's main in-game gamertag",
    member="Discord account to register as admin",
    alt_gamertag="Optional alt account / second gamertag",
)
async def register_admin_alias(
    interaction: discord.Interaction,
    gamertag: str,
    member: discord.Member,
    alt_gamertag: Optional[str] = None,
):
    await register_staff_slash(
        interaction=interaction,
        staff_type=StaffType.ADMIN,
        gamertag=gamertag,
        member=member,
        alt_gamertag=alt_gamertag,
    )


async def _do_remove_admin(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    """Shared logic for /remove-admin and /unregister-admin."""
    # Permission check
    if not _has_ai_control_role(interaction.user):
        await interaction.response.send_message(
            "❌ You do not have permission to remove admins.",
            ephemeral=True,
        )
        return

    removed = remove_admin_by_discord_id(member.id)
    if removed <= 0:
        msg = "No admin monitor entry found for that user."
        color = 0xE67E22
    else:
        msg = f"Removed **{removed}** admin row(s) for {member.mention}."
        color = 0xE74C3C

    await interaction.response.send_message(
        embed=discord.Embed(
            title="🗑 Admin Unregistered",
            description=msg,
            color=color,
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="remove-admin",
    description="Remove an admin from activity monitoring.",
)
@app_commands.describe(
    member="Discord account to remove from admin monitoring",
)
async def remove_admin_slash(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await _do_remove_admin(interaction, member)


@bot.tree.command(
    name="unregister-admin",
    description="Alias of /remove-admin – remove an admin from monitoring.",
)
@app_commands.describe(
    member="Discord account to remove from admin monitoring",
)
async def unregister_admin_slash(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await _do_remove_admin(interaction, member)


@bot.tree.command(
    name="nukecheck",
    description="Show the last 10 nukes and how many players claimed each reward.",
)
@app_commands.default_permissions(administrator=True)
async def nukecheck(interaction: discord.Interaction):
    stats = get_recent_nuke_stats(limit=10)

    if not stats:
        await interaction.response.send_message(
            "No NUKE purchases have been tracked yet.", ephemeral=True
        )
        return

    lines: List[str] = []
    for idx, entry in enumerate(stats, start=1):
        buyer_id = entry.get("buyer_id")
        buyer = f"<@{buyer_id}>" if buyer_id else "Unknown buyer"

        created_at = entry.get("created_at")
        if isinstance(created_at, datetime):
            ts = int(created_at.timestamp())
            when = f"<t:{ts}:R>"  # "5 minutes ago" style
        else:
            when = "time unknown"

        claims = entry.get("claims", 0)

        lines.append(f"**{idx}.** {buyer} – **{claims}** claim(s) – {when}")

    embed = discord.Embed(
        title="💣 NUKE Reward Stats (last 10)",
        description="\n".join(lines),
        color=0xE74C3C,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ===================== MAIN MESSAGE HANDLER =====================


@bot.event
async def on_message(message: discord.Message):
    # 0) Ignore our own messages (Otis)
    if message.author == bot.user:
        return

    channel = message.channel

    # 1) Detect KAOS nuke purchase events
    try:
        did_handle = await maybe_handle_nuke_purchase(bot, message)
        if did_handle:
            return
    except Exception as e:
        print(f"[NUKE] Error handling nuke purchase: {e}")

    # 1.5) Shop logs
    if isinstance(channel, discord.TextChannel) and channel.id == SHOP_LOG_CHANNEL_ID:
        await handle_shop_log_message(message)
        return

    # 2) Admin monitoring via Discord feeds is disabled (handled via RCON).

    # 3) Allow prefix commands
    await bot.process_commands(message)

    # 4) Only handle real text channels
    if not isinstance(channel, discord.TextChannel):
        return

    # 5) Determine if this is a ticket channel
    category = channel.category
    is_ticket = False

    # Category-based tickets
    if category and category.id in TICKET_CATEGORY_IDS:
        is_ticket = True

    # Name-based tickets (Tickets v2)
    if channel.name.lower().startswith("ticket-"):
        is_ticket = True

    if not is_ticket:
        return

    # 6) Track opener (first human)
    if not message.author.bot:
        try:
            note_ticket_opener(channel, message.author)
        except Exception as e:
            print(f"[TICKETS] note_ticket_opener error: {e}")

    # 7) Handle claim embeds from Tickets v2
    if message.author.bot and message.embeds:
        try:
            await handle_ticket_claim_message(message)
        except Exception as e:
            print(f"[TICKETS] handle_ticket_claim_message error: {e}")

        if channel.id not in ai_greeting_sent:
            await ensure_ai_control_message(channel, opener=None)

        return

    # SAFETY GUARD: no greeting or AI on other bot messages
    if message.author.bot:
        return

    # 9) Ticket close handling
    try:
        closed = await maybe_handle_close_message(message)
        if closed:
            return
    except Exception as e:
        print(f"[TICKETS] maybe_handle_close_message error: {e}")

    # 10) If Otis is disabled in this ticket, stop here.
    if channel.id not in active_ai_channels:
        return

    content = (message.content or "").strip()
    if not content:
        return

    lower_content = content.lower()

    # 11) Check if we're in the middle of a workflow intake
    try:
        consumed = await process_workflow_answer(bot, message)
    except Exception as e:
        print(f"[WORKFLOWS] process_workflow_answer error: {e}")
        consumed = False

    if consumed:
        return  # workflow handled it

    # 12) NEW workflow triggers / staff takeover detection
    is_staff = False
    if isinstance(message.author, discord.Member):
        is_staff = any(r.id in AI_CONTROL_ROLES for r in message.author.roles)

    # If a staff/support member talks in this ticket, permanently disable OTIS here.
    if is_staff:
        active_ai_channels.discard(channel.id)
        session = ticket_sessions.get(channel.id)
        if session is not None:
            session["ai_disabled"] = True
        return

    opener: Optional[discord.Member] = (
        message.author if isinstance(message.author, discord.Member) else None
    )

    # Admin abuse workflow
    if any(k in lower_content for k in ADMIN_ABUSE_KEYWORDS):
        await start_admin_abuse_workflow(channel, opener)
        return

    # ZORP issue workflow
    if any(k in lower_content for k in ZORP_ISSUE_KEYWORDS):
        await start_zorp_issue_workflow(channel, opener)
        return

    # Refund workflow
    if any(k in lower_content for k in REFUND_KEYWORDS):
        await start_refund_workflow(channel, opener)
        return

    # Kit issue workflow
    if any(k in lower_content for k in KIT_ISSUE_WORKFLOW_KEYWORDS):
        await start_kit_issue_workflow(channel, opener)
        return

    # 13) Kit helper (quickchat instructions)
    try:
        if looks_like_kit_question(content) or looks_like_kit_issue(content):
            helped = await kit_first_help(message, channel, content)
            if helped:
                return
    except Exception as e:
        print(f"[KITS] kit_first_help error: {e}")

    # 14) Main AI brain
    try:
        await maybe_handle_ticket_ai_message(
            bot=bot,
            client_ai=client_ai,
            message=message,
            style_text=style_text,
            rules_text=rules_text,
            zorp_guide_text=zorp_guide_text,
            raffle_text=raffle_text,
            ticket_sessions=ticket_sessions,
            ticket_category_ids=TICKET_CATEGORY_IDS,
            ai_control_roles=AI_CONTROL_ROLES,
        )
    except Exception as e:
        print(f"[TICKETS] maybe_handle_ticket_ai_message error: {e}")


# ===================== MAIN =====================


def main():
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
