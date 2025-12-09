import os
import random
from datetime import datetime, timedelta, UTC
from typing import Dict, Any

from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands, tasks
from discord import app_commands
from openai import OpenAI
import asyncio

rcon_failures: list[str] = []

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
)

from rcon_web import (
    RCON_ENABLED,
    rcon_manager,
    check_rcon_health_on_startup,
    run_rcon_command,
    rcon_send_all,
)

from bans import (
    init_ban_db,
    create_ban_record,
    mark_unbanned,
    deactivate_expired_bans,
    build_active_bans_embed,
    send_ban_log_embed,
    send_unban_log_embed,
)

from admin_monitor import (
    init_admin_monitor_db,
    prune_old_admin_events,
    register_or_update_admin,
    find_matching_admin_ids_from_text,
    server_name_for_channel,
    record_admin_event,
    update_admin_log_for_admin,
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

style_text      = load_style_text()
rules_text      = load_rules_text()
zorp_guide_text = load_zorp_guide_text()
raffle_text     = load_raffle_text()

ticket_sessions: Dict[int, Dict[str, Any]] = {}

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

            if after.startswith("<@") and after.endswith(">"):
                gamertag = after[2:-1].strip()
            else:
                gamertag = after

            if gamertag:
                print(f"[BANS] Detected shop unban for {gamertag}")
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


# ===================== BOT READY EVENT =====================

@bot.event
async def on_ready():
    global rcon_failures

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # Slash sync
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"⚠️ Slash command sync error: {e}")

    # Init DBs
    try:
        init_ban_db()
        print("[BANS] DB ready.")
    except Exception as e:
        print(f"[BANS] DB init failed: {e}")

    try:
        init_admin_monitor_db()
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
    else:
        try:
            failures = await check_rcon_health_on_startup()
            if failures:
                rcon_failures = failures
                print("[RCON] Failures detected:")
                for f in failures:
                    print(" -", f)
            else:
                print("[RCON] All servers passed.")
        except Exception as e:
            err = f"Startup check crashed: {e}"
            rcon_failures = [err]
            print("[RCON]", err)

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

        embed.add_field(
            name="Startup Time",
            value=f"<t:{ts}:R>",
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

        await log_channel.send(embed=embed)
        print(f"[STARTUP] Embed sent to {log_channel.id}.")

    except Exception as e:
        print(f"[STARTUP] Error sending embed: {e}")


# ===================== SLASH COMMANDS =====================

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
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Only guild members can use this.", ephemeral=True)
        return

    if not any(r.id in AI_CONTROL_ROLES for r in interaction.user.roles):
        await interaction.response.send_message("You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    await perform_unban(
        gamertag=gamertag,
        moderator=interaction.user,
        source="slash_unban",
    )

    await interaction.followup.send(f"✅ Unbanned **{gamertag}**.", ephemeral=True)
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

    # 2) Admin monitor feeds
    if isinstance(channel, discord.TextChannel) and (
        channel.id in PLAYER_FEED_CHANNEL_IDS
        or channel.id in ADMIN_FEED_CHANNEL_IDS
    ):
        await handle_admin_monitor_log(message)
        return

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



