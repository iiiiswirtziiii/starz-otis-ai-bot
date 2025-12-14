# nuke_rewards.py
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from datetime import datetime
from typing import Dict, Set, List, Optional, Tuple

import discord

from config_starz import (
    KAOS_LOG_CHANNEL_ID,
    KAOS_NUKE_ANNOUNCE_CHANNEL_ID,
    KAOS_COMMAND_CHANNEL_ID,
    NUKE_IMAGE_URL,
)

# ================= NUKE REWARD CONFIG =================
NUKE_REWARD_POINTS = 50000
# ======================================================

# announce_msg_id -> set(user_ids who claimed)
NUKE_CLAIMS: Dict[int, Set[int]] = {}

# announce_msg_id -> metadata
# {"message_id": int, "buyer_id": int, "created_at": datetime, "count": int, "points": int}
NUKE_META: Dict[int, Dict] = {}

# recent nukes newest first (for /nukecheck)
NUKE_HISTORY: deque[Dict] = deque(maxlen=50)

# ===================== DEDUPE (PERSISTENT) =====================
PROCESSED_KAOS_LOG_IDS: set[int] = set()
# Persist processed KAOS IDs on Railway volume
PROCESSED_KAOS_LOG_FILE = os.getenv(
    "PROCESSED_KAOS_LOG_FILE",
    "/data/processed_kaos_log_ids.json",
)

PROCESSED_KAOS_LOG_MAX = 5000
_PROCESSED_LOADED = False

# prevent race duplicates
NUKE_ANNOUNCE_LOCK = asyncio.Lock()


def _load_processed_ids_once() -> None:
    global _PROCESSED_LOADED, PROCESSED_KAOS_LOG_IDS
    if _PROCESSED_LOADED:
        return
    _PROCESSED_LOADED = True

    try:
        if not os.path.exists(PROCESSED_KAOS_LOG_FILE):
            return
        with open(PROCESSED_KAOS_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            PROCESSED_KAOS_LOG_IDS = set(int(x) for x in data if str(x).isdigit())
            print(f"[NUKE] Loaded {len(PROCESSED_KAOS_LOG_IDS)} processed KAOS log IDs from disk.")
    except Exception as e:
        print(f"[NUKE] Failed to load processed IDs: {e}")


def _save_processed_ids() -> None:
    try:
        ids_list = list(PROCESSED_KAOS_LOG_IDS)
        # Trim if too large (no guaranteed order, but good enough for dedupe)
        if len(ids_list) > PROCESSED_KAOS_LOG_MAX:
            ids_list = ids_list[-PROCESSED_KAOS_LOG_MAX:]
            PROCESSED_KAOS_LOG_IDS.clear()
            PROCESSED_KAOS_LOG_IDS.update(ids_list)

        with open(PROCESSED_KAOS_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(ids_list, f)
    except Exception as e:
        print(f"[NUKE] Failed to save processed IDs: {e}")


# ===================== PARSERS =====================

def _parse_nuke_purchase_from_log(text: str) -> Tuple[Optional[int], int]:
    """
    Returns (buyer_id, howmany)

    Supports:
      # <@123> dropped nuke
      # <@!123> dropped nuke[{custom:2}]
      # <@123> dropped nuke[2]
    """
    if not text:
        return (None, 1)

    m = re.search(r"<@!?(\d+)>", text)
    if not m:
        return (None, 1)

    try:
        buyer_id = int(m.group(1))
    except ValueError:
        return (None, 1)

    howmany = 1

    m2 = re.search(
        r"dropped\s+nuke\s*\[\s*(?:\{?\s*custom\s*:\s*)?(\d+)\s*\}?\s*\]",
        text,
        re.IGNORECASE,
    )
    if m2:
        try:
            howmany = int(m2.group(1))
        except ValueError:
            howmany = 1

    if howmany < 1:
        howmany = 1

    return (buyer_id, howmany)


def _record_new_nuke(message_id: int, buyer_id: int, count: int, points: int) -> None:
    created_at = datetime.utcnow()
    entry = {
        "message_id": message_id,
        "buyer_id": buyer_id,
        "created_at": created_at,
        "count": count,
        "points": points,
    }
    NUKE_HISTORY.appendleft(entry)
    NUKE_CLAIMS[message_id] = set()
    NUKE_META[message_id] = entry


def get_recent_nuke_stats(limit: int = 10) -> List[Dict]:
    out: List[Dict] = []
    for entry in list(NUKE_HISTORY)[:limit]:
        msg_id = entry["message_id"]
        claims = len(NUKE_CLAIMS.get(msg_id, set()))
        row = dict(entry)
        row["claims"] = claims
        out.append(row)
    return out


# ===================== VIEW =====================

class NukeClaimView(discord.ui.View):
    def __init__(self, buyer_id: int, reward_points: int):
        super().__init__(timeout=3600)
        self.buyer_id = buyer_id
        self.reward_points = reward_points

        # Build select dynamically so placeholder can use reward_points safely
        options = [
            discord.SelectOption(label=f"Server {i}", value=str(i))
            for i in range(1, 11)
        ]

        select = discord.ui.Select(
            placeholder=f"Pick your server to claim {self.reward_points:,} SCRAP",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)

            message = interaction.message
            if not message:
                await interaction.followup.send(
                    "This NUKE claim message is no longer valid.",
                    ephemeral=True,
                )
                return

            msg_id = message.id
            user_id = interaction.user.id

            claimed_set = NUKE_CLAIMS.get(msg_id)
            if claimed_set is None:
                await interaction.followup.send(
                    "This NUKE claim has expired or is no longer being tracked.",
                    ephemeral=True,
                )
                return

            if user_id in claimed_set:
                await interaction.followup.send(
                    "You’ve already claimed this NUKE reward.",
                    ephemeral=True,
                )
                return

            # Which server did they pick?
            picked = None
            for child in self.children:
                if isinstance(child, discord.ui.Select):
                    picked = child.values[0]
                    break
            if not picked:
                await interaction.followup.send("❌ No server selected.", ephemeral=True)
                return

            # Find KAOS command channel
            kaos_channel = (
                interaction.guild.get_channel(KAOS_COMMAND_CHANNEL_ID)
                if interaction.guild
                else None
            )
            if not isinstance(kaos_channel, discord.TextChannel):
                await interaction.followup.send(
                    "Internal error: KAOS command channel not found. Please tell a Head Admin.",
                    ephemeral=True,
                )
                return

            kaos_cmd = (
                f"[KAOS][ADD][<@{user_id}>]"
                f"[{picked}]=[POINTS][{self.reward_points}]"
            )

            # Send the KAOS command
            await kaos_channel.send(kaos_cmd)

            # Mark claimed after successful send
            claimed_set.add(user_id)

            await interaction.followup.send(
                f"✅ You claimed **{self.reward_points:,} SCRAP** on **Server {picked}**.",
                ephemeral=True,
            )

            print(f"[NUKE] Claim sent: user_id={user_id} server={picked} msg_id={msg_id}")

        except discord.NotFound:
            print("[NUKE] Ignored expired/unknown interaction on NUKE claim dropdown.")
        except Exception as e:
            print(f"[NUKE] Error handling NUKE claim: {e}")
            try:
                await interaction.followup.send(
                    "Something went wrong while processing your NUKE claim. Please tell a Head Admin.",
                    ephemeral=True,
                )
            except Exception:
                pass


# ===================== MAIN HANDLER =====================

async def maybe_handle_nuke_purchase(bot: discord.Client, message: discord.Message) -> bool:
    """
    Watch the KAOS log channel for nuke purchase lines and announce ONE claim embed.
    Dedupe is by KAOS log message ID (persistent across restarts) + lock for concurrency.
    """
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return False

    if channel.id != KAOS_LOG_CHANNEL_ID:
        return False

    _load_processed_ids_once()

    # Gather text first (content + embed descriptions)
    parts: List[str] = []
    if message.content:
        parts.append(message.content)

    for emb in message.embeds:
        if emb.description:
            parts.append(emb.description)

    combined = "\n".join(p.strip() for p in parts if p and p.strip())
    if not combined:
        return False

    lt = combined.lower()
    if "nuke" not in lt or "dropped" not in lt:
        return False

    buyer_id, howmany = _parse_nuke_purchase_from_log(combined)
    if buyer_id is None:
        return False

    reward_points = NUKE_REWARD_POINTS * howmany
    buyer_mention = f"<@{buyer_id}>"

    # DEDUPE + announce guarded by lock
    async with NUKE_ANNOUNCE_LOCK:
        if message.id in PROCESSED_KAOS_LOG_IDS:
            return False

        PROCESSED_KAOS_LOG_IDS.add(message.id)
        _save_processed_ids()

        announce_channel = bot.get_channel(KAOS_NUKE_ANNOUNCE_CHANNEL_ID)
        if not isinstance(announce_channel, discord.TextChannel):
            print("[NUKE] Nuke announce channel not found or not a text channel.")
            return False

        desc_lines = [
            f"{buyer_mention} 🔗 dropped **{howmany}** NUKE(s)!",
            "",
            f"Click the menu below to claim **{reward_points:,} SCRAP**",
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

        view = NukeClaimView(buyer_id=buyer_id, reward_points=reward_points)

        announce_msg = await announce_channel.send(
            content="@everyone",
            embed=embed,
            view=view,
        )

        _record_new_nuke(
            message_id=announce_msg.id,
            buyer_id=buyer_id,
            count=howmany,
            points=reward_points,
        )

        print(f"[NUKE] Announce message sent for buyer_id={buyer_id} count={howmany} points={reward_points} (msg_id={announce_msg.id}).")

    return True
