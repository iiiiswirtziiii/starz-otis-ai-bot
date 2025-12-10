import os
import json
import websockets
import random
from datetime import datetime, timedelta, UTC
from typing import Dict, Any
from typing import Dict, Any, List, Tuple, Optional
from dotenv import load_dotenv
load_dotenv()
import re
import discord
from discord.ext import commands, tasks
from discord import app_commands
from openai import OpenAI
import asyncio
from enum import Enum
from admin_mon_system import log_admin_activity_for_ids
from promoter_mon_system import maybe_handle_promoter_spawn

startup_file_failures = []  # Tracks any files that failed to load

rcon_failures: list[str] = []
# ================================
# STARTUP LOGGING SYSTEM
# ================================
import os
from datetime import datetime
import traceback

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
    rcon_manager,  # 🔹 add this
)



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

from config_starz import ADMIN_MONITOR_LOG_CHANNEL_ID



from admin_monitor import (
    init_admin_monitor_db,
    prune_old_admin_events,
    register_or_update_admin,
    remove_admin_by_discord_id,
    record_admin_event,
    update_admin_log_for_admin,
    find_matching_admin_ids_from_text,
    server_name_for_channel,
    fetch_admin_basic,
    get_admin_profile,
)




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

# ============= SANITY CHECKS =============

if not DISCORD_BOT_TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN is not set.")

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is not set.")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ============= GLOBAL STATE =============
# RCON console watcher tasks
RCON_WATCH_TASKS: list[asyncio.Task] = []

style_text      = load_style_text()
rules_text      = load_rules_text()
zorp_guide_text = load_zorp_guide_text()
raffle_text     = load_raffle_text()

ticket_sessions: Dict[int, Dict[str, Any]] = {}
# For join dedup in RCON watcher: (admin_id, server_name) -> last join ts
recent_join_events: Dict[tuple[int, str], float] = {}
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
TRACKER_DISABLED_UNTIL: datetime | None = None

# (admin_id, server_key, item) -> last enforcement ts
recent_enforcement_events: Dict[tuple[int, str, str], float] = {}


active_ai_channels: set[int] = set()
ticket_openers: Dict[int, int] = {}
ai_greeting_sent: set[int] = set()

# ============= BUILD GREETING EMBED =============

def build_ai_greeting_embed(opener: discord.Member | None = None) -> discord.Embed:
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

async def ensure_ai_control_message(channel: discord.TextChannel, opener: discord.Member | None) -> None:
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
        await channel.send(embed=embed, view=view)
    except Exception as e:
        print(f"[AI-TOGGLE] Failed to send AI control message: {e}")


# ===================== BAN HELPERS =====================

async def refresh_active_bans_embed() -> None:
    channel = bot.get_channel(ACTIVE_BANS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[BANS] Active bans channel {ACTIVE_BANS_CHANNEL_ID} not found.")
        return

    embed = build_active_bans_embed()

    try:
        last_messages = [
            msg async for msg in channel.history(limit=10)
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
# ========= Admin spawn enforcement (auto-ban + review embed) =========

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
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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
    discord_user: discord.Member | None,
    reason: str,
    moderator: discord.Member | None,
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

    print(f"[BANS] Created ban for {gamertag} tier={offense_tier} duration={duration_text}")

    cmd = f'banid "{gamertag}" "{reason_text}"'

    try:
        await rcon_send_all(cmd)

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
    moderator: discord.Member | None,
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
        await rcon_send_all(cmd)
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
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not user_is_enforcement(interaction.user):
            return await interaction.response.send_message(
                "❌ You are not authorized to use this button.",
                ephemeral=True,
            )

        # Use your existing ban helper, track in DB + RCON
        reason = "Admin spawned high-risk items (rockets / C4 / MLRS)."
        await perform_ban(
            gamertag=self.gamertag,
            discord_user=None,  # we don't need the Discord user to log the ban
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
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not user_is_enforcement(interaction.user):
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
    gamertag: str | None = None
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
        reason = (
            f"High-risk admin spawn: {matched_item} x{amount} "
            f"on {server_name}"
        )

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
            print(
                f"[SPAWN-ENFORCE] Sent in-game ban command for {gamertag} on "
                f"{server_key}: {ban_cmd}"
            )

        except Exception as e:
            print(f"[SPAWN-ENFORCE] RCON ban failed for {gamertag}: {e}")

    # ======== Build & send review embed ========
    # Try to get nicer profile info for embed
    profile = get_admin_profile(admin_id)
    embed_discord_id = profile["discord_id"] if profile else discord_id

    channel = bot.get_channel(ADMIN_ENFORCEMENT_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[SPAWN-ENFORCE] Enforcement channel {ADMIN_ENFORCEMENT_CHANNEL_ID} not found.")
        return

    dt = datetime.fromtimestamp(created_at_ts)
    time_str = dt.strftime("%Y-%m-%d %I:%M %p").lstrip("0")

    # If we couldn't parse an amount (kits), assume 1 for display
    if amount <= 0:
        amount = 1

    summary_line = f"{server_name} ({matched_item}) x{amount} {time_str}"

    desc_lines = [
        f"**Server:** {server_name}",
        f"**Gamertag:** `{gamertag}`",
        f"**Item:** `{matched_item}` x{amount}",
        f"**Time (UTC):** {time_str}",
        "",
        f"```{console_line[:900]}```",
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

    # Uses your existing buttons:
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

    parts = []

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

    server_name = server_name_for_channel(ch_id)

    for admin_id in matching_admin_ids:
        record_admin_event(
            admin_id=admin_id,
            event_type=event_type,
            server_name=server_name,
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
            gamertag: str | None = None

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


    # --- Forward into KAOS command channel ---
    kaos_channel = (
        message.guild.get_channel(KAOS_COMMAND_CHANNEL_ID)
        if message.guild
        else None
    )

    if not isinstance(kaos_channel, discord.TextChannel):
        print(f"[SHOP] KAOS channel {KAOS_COMMAND_CHANNEL_ID} missing.")
        return

    parts = []
    if message.content:
        parts.append(message.content)
    for e in message.embeds:
        if e.description:
            parts.append(e.description)

    if not parts:
        return

    forward = "\n".join(parts)[:1800]

    try:
        await kaos_channel.send(forward)
    except Exception as e:
        print(f"[SHOP] Failed to forward log: {e}")

async def watch_rcon_console_for_server(
    server_key: str,
    host: str,
    port: int,
    password: str,
) -> None:
    """
    Open a dedicated WebRCON connection and stream console logs.
    For every log line, if it contains any registered admin GT,
    record a JOIN or SPAWN event (and ignore all other noise).

    Pipeline per matching log:
      1) Admin monitor logging  → admin_mon_system.log_admin_activity_for_ids
      2) Promoter monitoring    → promoter_mon_system.maybe_handle_promoter_spawn
      3) High-risk enforcement  → handle_spawn_enforcement_for_event
    """
    url = f"ws://{host}:{port}/{password}/"
    print(f"[RCON-WATCH:{server_key}] Watcher starting for {url}")

    while True:
        # If RCON is globally disabled, just sleep and re-check later
        if not RCON_ENABLED:
            await asyncio.sleep(30)
            continue

        try:
            print(f"[RCON-WATCH:{server_key}] Connecting to {url} ...")
            async with websockets.connect(url, ping_interval=None) as ws:
                print(f"[RCON-WATCH:{server_key}] ✅ Connected, streaming console logs...")

                async for raw in ws:
                    # -------- Parse raw JSON from WebRCON --------
                    try:
                        data = json.loads(raw)
                    except Exception:
                        # Not JSON, ignore
                        continue

                    # Rust WebRCON format: { "Identifier": int, "Message": "text", "Name": "..." }
                    msg_text = (data.get("Message") or "").replace("\u0000", "").strip()
                    if not msg_text:
                        continue

                    ident = data.get("Identifier")
                    if ident not in (0, None):
                        # This is likely a direct response to some other command – skip
                        continue

                    # -------- Find matching admins in this log line --------
                    matching_admin_ids = find_matching_admin_ids_from_text(msg_text)
                    if not matching_admin_ids:
                        continue

                    lt = msg_text.lower()

                    #  - joins: "... has entered the game" / " joined ["
                    #  - spawns: rockets/C4/MLRS via [ServerVar], OR any kit usage
                    is_join = "has entered the game" in lt or " joined [" in lt

                    is_spawn = False

                    # High-risk items via ServerVar with explicit amount (rockets/C4/MLRS)
                    if "[servervar] giving" in lt and " x " in lt:
                        is_spawn = True

                    # Any kit usage (elitekits etc) should count as a spawn for logging
                    if not is_spawn and (
                        "elitekit" in lt
                        or "kit givetoplayer" in lt
                        or "[kitmanager]" in lt
                    ):
                        is_spawn = True


                    # Ignore killfeed, gravity memes, slot dumps, console say spam, etc.
                    if not is_join and not is_spawn:
                        continue

                    # Map s1..s10 → "Server 1".."Server 10" for nicer display
                    if server_key.lower().startswith("s") and server_key[1:].isdigit():
                        server_name = f"Server {server_key[1:]}"
                    else:
                        server_name = server_key.upper()

                    now_ts = datetime.utcnow().timestamp()

                    # Decide event_type + detail ONCE for this line
                    if is_join:
                        event_type = "join"
                        detail = "Joined server"
                    else:
                        event_type = "spawn"
                        detail = msg_text[:900]

                    # -------- 1) Admin monitor logging (DB + per-admin embed) --------
                    try:
                        await log_admin_activity_for_ids(
                            bot,
                            matching_admin_ids,
                            event_type=event_type,
                            server_name=server_name,
                            detail=detail,
                        )
                    except Exception as e:
                        print(f"[RCON-WATCH:{server_key}] Admin log error: {e}")

                    # -------- 2) Promoter monitoring – monitor-only, no punishments --------
                    if event_type == "spawn":
                        for admin_id in matching_admin_ids:
                            try:
                                await maybe_handle_promoter_spawn(
                                    bot=bot,
                                    admin_id=admin_id,
                                    server_name=server_name,
                                    detail=detail,
                                    created_at_ts=now_ts,
                                )
                            except Exception as e:
                                print(f"[RCON-WATCH:{server_key}] Promoter monitor error: {e}")

                    # -------- 3) High-risk spawn enforcement (rockets / C4 / MLRS / certain kits) --------
                    if event_type == "spawn" and "handle_spawn_enforcement_for_event" in globals():
                        lt_msg = msg_text.lower()
                        matched_item: str | None = None

                        # Item-based high risk (rockets, C4, MLRS) – any [ServerVar] line
                        for item in HIGH_RISK_SPAWN_ITEMS:
                            if item.lower() in lt_msg:
                                matched_item = item
                                break

                        # Kit-based high risk ({elitekitX}) – only enforce on the KITMANAGER success line
                        if matched_item is None and "[kitmanager]" in lt_msg:
                            for kit in HIGH_RISK_KITS:
                                if kit.lower() in lt_msg:
                                    matched_item = kit
                                    break

                        if matched_item:
                            for admin_id in matching_admin_ids:
                                # De-dup same admin/server within a short window
                                key = (admin_id, server_key)
                                last = recent_enforcement_events.get(key)
                                if last and now_ts - last < 5:
                                    # Already enforced this burst; skip extra embeds
                                    continue

                                recent_enforcement_events[key] = now_ts

                                try:
                                    await handle_spawn_enforcement_for_event(
                                        admin_id=admin_id,
                                        server_key=server_key,
                                        server_name=server_name,
                                        matched_item=matched_item,
                                        console_line=msg_text,
                                        created_at_ts=now_ts,
                                    )
                                except Exception as e:
                                    print(f"[RCON-WATCH:{server_key}] Enforcement error: {e}")

                    print(
                        f"[RCON-WATCH:{server_key}] Matched admins "
                        f"{matching_admin_ids} in log: {msg_text}"
                    )

        except Exception as e:
            print(f"[RCON-WATCH:{server_key}] WebRCON error: {e}")
            await asyncio.sleep(10)


# ===================== BOT READY EVENT =====================

@bot.event
async def on_ready():
    ...

    global rcon_failures
    # Track startup health so we can show it in the embed
    ban_db_ok = False
    admin_db_ok = False
    slash_count = 0

    # ==========================================
    # STARTUP CHECKS FOR ALL CORE MODULES
    # ==========================================
    write_startup_log("===== STARZ BOT STARTUP =====")

    # -------- ENV VARS --------
    check_env_var("DISCORD_BOT_TOKEN")
    check_env_var("OPENAI_API_KEY")
    check_env_var("OPENAI_MODEL")

    # -------- PYTHON MODULE FILES --------
    check_file_exists("config_starz.py", required=True)
    check_file_exists("ticket_ai.py", required=True)
    check_file_exists("ticket_helpers.py", required=True)
    check_file_exists("workflows.py", required=True)
    check_file_exists("zorp_lookup.py", required=True)
    check_file_exists("kit_helpers.py", required=True)
    check_file_exists("rcon_web.py", required=True)
    check_file_exists("bans.py", required=True)
    check_file_exists("admin_monitor.py", required=True)

    # -------- DATA FILES --------
    check_file_exists("kit_claims.txt", required=True)
    check_file_exists("configzorp_guide.txt", required=False)

    # -------- DATABASES --------
    check_file_exists("starz_bans.db", required=False)

    write_startup_log("Core file + DB checks complete.")

    # ===================== NORMAL READY LOGIC =====================
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

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


    # Background loops
    if not ban_expiry_loop.is_running():
        ban_expiry_loop.start()
    if not admin_monitor_cleanup_loop.is_running():
        admin_monitor_cleanup_loop.start()

    rcon_failures = []

    # RCON check
    if not RCON_ENABLED:
        print("[RCON] Disabled by master switch.")
        rcon_failures.append("RCON master switch disabled; skipping check.")
    # RCON check
    if not RCON_ENABLED:
        print("[RCON] Disabled by master switch.")
        rcon_failures.append("RCON master switch disabled; skipping check.")
    else:
        # Temporary: skip startup health check; watchers will act as our real test
        print("[RCON] Enabled → skipping startup health check (using live console watchers instead).")
        rcon_failures.append("Startup RCON health check skipped; console watchers will validate.")

    # Active bans embed
    try:
        await refresh_active_bans_embed()
    except Exception as e:
        print(f"[BANS] Unable to refresh active bans at startup: {e}")

    # Startup embed
    try:
        log_channel = bot.get_channel(STAFF_ALERT_CHANNEL_ID)

        if not isinstance(log_channel, discord.TextChannel):
            print(f"[STARTUP] Staff alert channel {STAFF_ALERT_CHANNEL_ID} not found.")
            return

        now = datetime.now(UTC)
        ts = int(now.timestamp())

        rcon_switch_text = "✅ Enabled" if RCON_ENABLED else "❌ Disabled"

        if not rcon_failures or (
            len(rcon_failures) == 1
            and "skipping check" in rcon_failures[0]
        ):
            status_line = "All systems operational."
            systems_line = "0 system failures."
        else:
            status_line = "Some systems reported issues."
            systems_line = f"{len(rcon_failures)} failure(s)."

        # ---------- Build base embed ----------
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

        # ---------- File load summary ----------
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

        # ---------- Startup time ----------
        embed.add_field(
            name="Startup Time",
            value=f"<t:{ts}:R>",
            inline=False,
        )

        # ---------- Commands & AI ----------
        embed.add_field(
            name="Commands & AI",
            value=(
                f"• **Prefix:** `!`\n"
                f"• **Slash commands:** {slash_count}\n"
                f"• **AI Model:** `{OPENAI_MODEL}`"
            ),
            inline=False,
        )

        # ---------- Config summary ----------
        total_rcon = len(rcon_manager.clients)
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

        # ---------- Database status ----------
        ban_db_status = "✅ Bans DB" if ban_db_ok else "❌ Bans DB"
        admin_db_status = "✅ Admin Monitor DB" if admin_db_ok else "❌ Admin Monitor DB"

        embed.add_field(
            name="Database Status",
            value=f"{ban_db_status} • {admin_db_status}",
            inline=False,
        )

        # ---------- Kits & guides ----------
        kit_count = len(kit_claims)
        zorp_guide_exists = os.path.exists("configzorp_guide.txt")
        zorp_guide_text = "✅ Found" if zorp_guide_exists else "⚠ Missing"

        embed.add_field(
            name="Kits & Guides",
            value=(
                f"• **Kit configs loaded:** {kit_count}\n"
                f"• **ZORP guide file:** {zorp_guide_text}"
            ),
            inline=False,
        )

        # ---------- RCON notes (if failures) ----------
        if rcon_failures and RCON_ENABLED:
            notes = "\n".join(rcon_failures[:3])
            embed.add_field(
                name="RCON Notes",
                value=notes,
                inline=False,
            )

        # Thumbnail + send
        try:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
        except Exception:
            pass

        await log_channel.send(embed=embed)
        print(f"[STARTUP] Embed sent to {log_channel.id}.")

    except Exception as e:
        print(f"[STARTUP] Error sending embed: {e}")

    # RCON console watchers (only if enabled, and only start once)
    global RCON_WATCH_TASKS
    if RCON_ENABLED and not RCON_WATCH_TASKS:
        try:
            # Local import to avoid changing the top-of-file imports
            from rcon_web import RCON_CONFIGS

            for key, cfg in RCON_CONFIGS.items():
                task = asyncio.create_task(
                    watch_rcon_console_for_server(
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

from enum import Enum

class StaffType(Enum):
    ADMIN = "Admin"
    PROMOTER = "Promoter"

    def __str__(self) -> str:
        return self.value


# ===================== SLASH COMMANDS =====================
from typing import Optional
import discord
from discord import app_commands

@bot.tree.command(name="ban", description="Ban a player by gamertag.")
@app_commands.describe(gamertag="Player's IGN", reason="Reason for ban")
async def slash_ban(interaction: discord.Interaction, gamertag: str, reason: str = ""):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Only guild members can use this.", ephemeral=True)
        return

    if not any(r.id in AI_CONTROL_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    discord_user = None
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

    await interaction.followup.send(f"✅ Banned **{gamertag}**.", ephemeral=True)


@bot.tree.command(name="unban", description="Unban a player by gamertag.")
@app_commands.describe(gamertag="Player's IGN")
async def slash_unban(interaction: discord.Interaction, gamertag: str):
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
            "You do not have permission.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    await perform_unban(
        gamertag=gamertag,
        moderator=interaction.user,
        source="slash_unban",
    )

    await interaction.followup.send(
        f"✅ Unbanned **{gamertag}**.",
        ephemeral=True,
    )


@bot.tree.command(
    name="reduceoffense",
    description="Reduce a player's ban offenses by 1 if last ban is older than 90 days.",
)
@app_commands.describe(gamertag="Player's in-game name (IGN)")
async def slash_reduceoffense(interaction: discord.Interaction, gamertag: str):
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

    await interaction.response.defer(ephemeral=True)

    # Try to reduce one offense if the last ban is older than 90 days
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
        await interaction.followup.send(
            (
                f"ℹ️ No offense was reduced for **{gamertag}**.\n"
                "Either they have no bans on record, or the most recent ban "
                "is not older than 90 days."
            ),
            ephemeral=True,
        )
        return

    # Offense actually reduced: show updated ladder status
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
    duration_hours="How many hours (0–6) to disable. 0 = until you run /enable-tracker.",
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
        human = "until you run /enable-tracker"
    else:
        TRACKER_DISABLED_UNTIL = datetime.now(UTC) + timedelta(hours=duration_hours)
        human = f"for **{duration_hours}** hour(s)"

    await interaction.response.send_message(
        f"⏸️ Admin spawn tracker **DISABLED** {human}. "
        f"Flagged rockets/C4/MLRS spawns will **not** trigger alerts or bans.",
        ephemeral=True,
    )


@bot.tree.command(
    name="enable-tracker",
    description="Re-enable the admin spawn tracker alerts if they were disabled.",
)
async def enable_tracker_slash(interaction: discord.Interaction):
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
    staff_type: StaffType,             # 👈 dropdown in Discord
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

    # ===== ADMIN PATH – same behavior as old /register-admin =====
    if staff_type is StaffType.ADMIN:
        # 1) Write / update in DB (same as before)
        admin_id = register_or_update_admin(
            discord_user=member,
            main_gamertag=gamertag,
            alt_gamertag=alt_gamertag,
        )

        # 2) Immediately build / refresh their admin-actions embed (same)
        try:
            await update_admin_log_for_admin(
                bot,
                admin_id,
                ADMIN_MONITOR_LOG_CHANNEL_ID,
            )
            print(f"[ADMIN-MONITOR] Updated admin log for admin_id={admin_id}")
        except Exception as e:
            print(f"[ADMIN-MONITOR] Failed to update admin log after register: {e}")

        # 3) Ephemeral confirmation back to the command user (same info)
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Admin Registered",
                description=(
                    f"**Type:** Admin\n"
                    f"**Discord:** {member.mention}\n"
                    f"**Main GT:** `{gamertag}`\n"
                    f"**Alt GT:** `{alt_gamertag or 'None'}`\n"
                    f"**Admin ID:** `{admin_id}`\n\n"
                    "OTIS will now track this admin in feeds / RCON and has created/updated their activity log."
                ),
                color=0x2ECC71,
            ),
            ephemeral=True,
        )
        return

    # ===== PROMOTER PATH – placeholder for promoter_mon_system =====
    if staff_type is StaffType.PROMOTER:
        # TODO: hook into promoter_mon_system when it's ready
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Promoter Registered",
                description=(
                    f"**Type:** Promoter\n"
                    f"**Discord:** {member.mention}\n"
                    f"**Main GT:** `{gamertag}`\n"
                    f"**Alt GT:** `{alt_gamertag or 'None'}`\n\n"
                    "Promoter monitoring is enabled logically here. "
                    "We'll wire this into promoter_mon_system next."
                ),
                color=0xF1C40F,
            ),
            ephemeral=True,
        )
        return


# OPTIONAL: keep old /register-admin as an alias that behaves EXACTLY the same
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
    # Just call the unified /register handler with staff_type=ADMIN
    await register_staff_slash(
        interaction=interaction,
        staff_type=StaffType.ADMIN,
        gamertag=gamertag,
        member=member,
        alt_gamertag=alt_gamertag,
    )




async def _do_remove_admin(interaction: discord.Interaction, member: discord.Member):
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


@bot.tree.command(name="remove-admin", description="Remove an admin from activity monitoring.")
@app_commands.describe(
    member="Discord account to remove from admin monitoring",
)
async def remove_admin_slash(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await _do_remove_admin(interaction, member)


@bot.tree.command(name="unregister-admin", description="Alias of /remove-admin – remove an admin from monitoring.")
@app_commands.describe(
    member="Discord account to remove from admin monitoring",
)
async def unregister_admin_slash(
    interaction: discord.Interaction,
    member: discord.Member,
):
    await _do_remove_admin(interaction, member)


# ===================== MAIN MESSAGE HANDLER =====================

@bot.event
async def on_message(message: discord.Message):
    # Debug watcher
    try:
        print(
            f"[DEBUG] MSG in #{getattr(message.channel, 'name', '??')} "
            f"({getattr(message.channel, 'id', 'no-id')}) "
            f"from {message.author} ({message.author.id}): {message.content!r}"
        )
    except Exception:
        pass

    # 0) Ignore our own messages (Otis)
    if message.author == bot.user:
        return

    channel = message.channel

    # 1) Shop logs
    if isinstance(channel, discord.TextChannel) and channel.id == SHOP_LOG_CHANNEL_ID:
        await handle_shop_log_message(message)
        return

    # 2) [LEGACY] Admin monitoring via Discord feeds is disabled.
    # RCON console watcher now handles admin activity logs.
    # if isinstance(channel, discord.TextChannel) and (
    #     channel.id in PLAYER_FEED_CHANNEL_IDS
    #     or channel.id in ADMIN_FEED_CHANNEL_IDS
    # ):
    #     await handle_admin_monitor_log(message)
    #     return


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

        # If this is the first time we're touching this ticket, send the Otis
        # greeting now based on the ticket embed, before any player messages.
        if channel.id not in ai_greeting_sent:
            await ensure_ai_control_message(channel, opener=None)

        # Never run AI/workflows/kit logic on bot messages.
        return

    # -------------- SAFETY GUARD: no greeting or AI on other bot messages --------------
    if message.author.bot:
        return
    # -----------------------------------------------------------------------


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

    # 12) NEW workflow triggers (only for human, non-staff)
    is_staff = False
    if isinstance(message.author, discord.Member):
        is_staff = any(r.id in AI_CONTROL_ROLES for r in message.author.roles)

    if not message.author.bot and not is_staff:
        opener = message.author if isinstance(message.author, discord.Member) else None

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
