# promoter_mon_system.py
from __future__ import annotations

from time import time
from typing import Optional
from datetime import datetime

import discord

from config_starz import PROMOTER_ROLE_IDS, PROMOTER_ALERT_CHANNEL_ID
from admin_monitor import get_admin_profile, summarize_spawn_row

# Simple cooldown so you don't get 200 alerts in a second if a promoter
# spawns a whole kit at once.
_last_promoter_alert: dict[int, float] = {}
PROMOTER_ALERT_COOLDOWN_SECONDS = 5.0  # adjust if you want more/less spam


async def maybe_handle_promoter_spawn(
    bot: discord.Client,
    admin_id: int,
    server_name: str,
    detail: str,
    created_at_ts: float,
) -> None:
    """
    Called from the RCON watcher for *every* admin spawn.

    If the admin has a promoter role (PROMOTER_ROLE_IDS),
    send a notification embed to PROMOTER_ALERT_CHANNEL_ID.

    ❗ No RCON commands are sent here. This is PURELY monitoring.
    """

    # --- Cooldown per admin to avoid spam ---
    now = time()
    last = _last_promoter_alert.get(admin_id, 0.0)
    if now - last < PROMOTER_ALERT_COOLDOWN_SECONDS:
        return
    _last_promoter_alert[admin_id] = now

    # --- Look up profile info (discord_id + gamertag) ---
    profile = get_admin_profile(admin_id)
    if not profile:
        print(f"[PROMOTER-MON] No profile for admin_id={admin_id}")
        return

    discord_id = profile["discord_id"]
    gamertag = profile["gamertag"]

    # --- Resolve Discord member & check promoter role ---
    member: Optional[discord.Member] = None
    for guild in bot.guilds:
        m = guild.get_member(discord_id)
        if m is not None:
            member = m
            break

    if member is None:
        print(f"[PROMOTER-MON] Could not resolve Discord member for ID {discord_id}")
        return

    is_promoter = any(r.id in PROMOTER_ROLE_IDS for r in member.roles)
    if not is_promoter:
        # Not a promoter → we don't care for this monitor
        return

    # --- Resolve alert channel ---
    channel = bot.get_channel(PROMOTER_ALERT_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print(f"[PROMOTER-MON] Alert channel {PROMOTER_ALERT_CHANNEL_ID} not found.")
        return

    # Short friendly summary line
    summary = summarize_spawn_row(server_name, detail, created_at_ts) or detail[:80]

    embed = discord.Embed(
        title="📣 Promoter Spawn Logged",
        color=0xF1C40F,
        description=(
            f"Promoter **{gamertag}** spawned an item on **{server_name}**.\n"
            "This is **monitor-only** (no automatic punishment)."
        ),
        timestamp=datetime.utcnow(),
    )

    embed.add_field(name="Discord", value=f"<@{discord_id}>", inline=True)
    embed.add_field(name="Gamertag", value=gamertag, inline=True)
    embed.add_field(name="Summary", value=summary, inline=False)
    embed.add_field(
        name="Raw console line",
        value=f"```{detail[:900]}```",
        inline=False,
    )

    try:
        await channel.send(embed=embed)
        print(
            f"[PROMOTER-MON] Logged promoter spawn for admin_id={admin_id}, "
            f"GT={gamertag} on {server_name}"
        )
    except Exception as e:
        print(f"[PROMOTER-MON] Failed to send promoter alert: {e}")
