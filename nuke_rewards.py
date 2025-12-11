# nuke_rewards.py
from __future__ import annotations

from typing import Dict, Set, List, Optional
from collections import deque
from datetime import datetime

import re
import discord

from config_starz import (
    KAOS_LOG_CHANNEL_ID,
    KAOS_NUKE_ANNOUNCE_CHANNEL_ID,
    KAOS_COMMAND_CHANNEL_ID,
    NUKE_IMAGE_URL,
)

# ================= NUKE REWARD CONFIG =================
# Servers that the KAOS nuke reward should apply to
NUKE_REWARD_SERVERS = "1,2,3,4,5,6,7,8,9,10"

# Scrap / points amount per server (edit this if you change your nuke reward)
NUKE_REWARD_POINTS = 50000
# ======================================================


# In-memory tracking
# message_id -> set(user_ids who claimed)
NUKE_CLAIMS: Dict[int, Set[int]] = {}

# recent nukes, newest first
# each entry: {"message_id": int, "buyer_id": int, "created_at": datetime}
NUKE_HISTORY: deque[Dict] = deque(maxlen=50)


def _record_new_nuke(message_id: int, buyer_id: int) -> None:
    """
    Track a freshly-announced nuke so /nukecheck can report on it.
    """
    entry = {
        "message_id": message_id,
        "buyer_id": buyer_id,
        "created_at": datetime.utcnow(),
    }
    NUKE_HISTORY.appendleft(entry)
    NUKE_CLAIMS[message_id] = set()


def get_recent_nuke_stats(limit: int = 10) -> List[Dict]:
    """
    Return up to `limit` recent nukes with claim counts.

    Each dict looks like:
        {
          "message_id": int,
          "buyer_id": int,
          "created_at": datetime,
          "claims": int
        }
    """
    out: List[Dict] = []
    for entry in list(NUKE_HISTORY)[:limit]:
        msg_id = entry["message_id"]
        claims = len(NUKE_CLAIMS.get(msg_id, set()))
        row = dict(entry)
        row["claims"] = claims
        out.append(row)
    return out


def _parse_nuke_buyer_id_from_log(content: str) -> Optional[int]:
    """
    Detect a KAOS 'dropped nuke' log line and extract the Discord user ID.

    Expected format (in KAOS log channel):
      "# <@123456789012345678> dropped nuke"
    """
    m = re.search(r"<@(\d+)> dropped nuke", content)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


class NukeClaimView(discord.ui.View):
    def __init__(self, buyer_id: int):
        super().__init__(timeout=3600)  # 1 hour buttons
        self.buyer_id = buyer_id

    @discord.ui.select(
        placeholder=f"Pick your server to claim {NUKE_REWARD_POINTS:,} SCRAP",
        options=[
            discord.SelectOption(label="Server 1", value="1"),
            discord.SelectOption(label="Server 2", value="2"),
            discord.SelectOption(label="Server 3", value="3"),
            discord.SelectOption(label="Server 4", value="4"),
            discord.SelectOption(label="Server 5", value="5"),
            discord.SelectOption(label="Server 6", value="6"),
            discord.SelectOption(label="Server 7", value="7"),
            discord.SelectOption(label="Server 8", value="8"),
            discord.SelectOption(label="Server 9", value="9"),
            discord.SelectOption(label="Server 10", value="10"),
        ],
    )
    async def select_server(self, interaction: discord.Interaction, select: discord.ui.Select):
        message = interaction.message
        if not message:
            await interaction.response.send_message(
                "This NUKE claim message is no longer valid.", ephemeral=True
            )
            return

        msg_id = message.id
        user_id = interaction.user.id

        # Has this user already claimed for this nuke?
        claimed_set = NUKE_CLAIMS.get(msg_id)
        if claimed_set is None:
            await interaction.response.send_message(
                "This NUKE claim has expired or is no longer being tracked.",
                ephemeral=True,
            )
            return

        if user_id in claimed_set:
            await interaction.response.send_message(
                "You’ve already claimed this NUKE reward.", ephemeral=True
            )
            return

        server_choice = select.values[0]

        # Send KAOS command to grant the reward
        kaos_channel = interaction.guild.get_channel(KAOS_COMMAND_CHANNEL_ID) if interaction.guild else None
        if not isinstance(kaos_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Internal error: KAOS command channel not found. Please tell a Head Admin.",
                ephemeral=True,
            )
            return

        kaos_cmd = (
            f"[KAOS][ADD][<@{user_id}>]"
            f"[{server_choice}]=[POINTS][{NUKE_REWARD_POINTS}]"
        )

        await kaos_channel.send(kaos_cmd)

        # Mark as claimed
        claimed_set.add(user_id)

        await interaction.response.send_message(
            f"You claimed **{NUKE_REWARD_POINTS:,} SCRAP** on **Server {server_choice}**.",
            ephemeral=True,
        )





async def maybe_handle_nuke_purchase(bot: discord.Client, message: discord.Message) -> bool:
    """
    Watch the KAOS log channel for nuke purchase lines and,
    when detected, post a claim embed in the NUKE announce channel
    with a dropdown so players can claim their reward.

    Also records nukes so /nukecheck can show the last 10 and
    how many people claimed each one.
    """
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return False

    # Only watch the configured KAOS logs channel
    if channel.id != KAOS_LOG_CHANNEL_ID:
        return False

    # Combine message content + any embed descriptions (KAOS often uses embeds)
    content_parts: list[str] = []
    if message.content:
        content_parts.append(message.content)

    for e in message.embeds:
        if e.description:
            content_parts.append(e.description)

    combined = "\n".join(p.strip() for p in content_parts if p.strip())
    if not combined:
        return False

    # Try to extract the buyer's Discord ID from a line like:
    # "# <@123456789012345678> dropped nuke"
    buyer_id = _parse_nuke_buyer_id_from_log(combined)
    if not buyer_id:
        return False

    buyer_mention = f"<@{buyer_id}>"

    # Find the nuke announce channel where players will click to claim
    announce_channel = bot.get_channel(KAOS_NUKE_ANNOUNCE_CHANNEL_ID)
    if not isinstance(announce_channel, discord.TextChannel):
        print("[NUKE] Nuke announce channel not found or not a text channel.")
        return False

    # Build the embed that players will see
    desc_lines = [
        f"{buyer_mention} **dropped a NUKE!**",
        "",
        f"Click the menu below to claim **{NUKE_REWARD_POINTS:,} SCRAP**",
        "on the server you play on.",
    ]
    embed = discord.Embed(
        title="☢️ KAOS NUKE DROPPED!",
        description="\n".join(desc_lines),
        color=0xE67E22,
    )
    if NUKE_IMAGE_URL:
        try:
            embed.set_image(url=NUKE_IMAGE_URL)
        except Exception:
            pass

    view = NukeClaimView(buyer_id=buyer_id)

    # Send the announce message with the dropdown view
    announce_msg = await announce_channel.send(
        content="@everyone",
        embed=embed,
        view=view,
    )


    # Record this nuke so /nukecheck can see it
    _record_new_nuke(message_id=announce_msg.id, buyer_id=buyer_id)

    print(f"[NUKE] Announce message sent for buyer_id={buyer_id} (msg_id={announce_msg.id}).")
    return True


