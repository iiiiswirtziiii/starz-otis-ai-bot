# ticket_helpers.py
"""
Helpers for ticket channels:
- Detect if a channel is a ticket
- Track ticket openers
- Handle "Claimed Ticket" embeds from STARZ TICKETS
- Handle close-confirmation flow for tickets
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, Set

import discord

from config_starz import TICKET_CATEGORY_IDS, AI_CONTROL_ROLES

# Who opened which ticket (channel.id -> user.id)
ticket_openers: Dict[int, int] = {}

# Channels waiting for YES to close
ticket_close_pending: Set[int] = set()

CLOSE_PATTERNS = (
    "you can close",
    "u can close",
    "can close this",
    "ticket can be closed",
    "you may close",
    "yall can close",
    "ya'll can close",
)

CLOSE_CONFIRM_WORDS = {
    "yes", "y", "yeah", "yep", "close", "close it", "sure", "ok", "okay",
}


def is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return isinstance(channel, discord.TextChannel) and channel.category_id in TICKET_CATEGORY_IDS


def slugify_channel_name(name: str) -> str:
    """
    Turn a display name into a safe channel name fragment.
    We'll still append '-ticket' later if needed.
    """
    name = name.lower()
    # Replace any non a-z0-9 with hyphens
    name = re.sub(r"[^a-z0-9-]+", "-", name)
    # Collapse duplicate hyphens
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "ticket"
    # Keep short enough to append "-ticket" safely
    if len(name) > 80:
        name = name[:80]
    return name


def note_ticket_opener(channel: discord.TextChannel, author: discord.abc.User) -> None:
    """
    Remember who opened the ticket (first non-bot in that channel).
    """
    if not isinstance(channel, discord.TextChannel):
        return
    if not is_ticket_channel(channel):
        return
    if not isinstance(author, discord.Member):
        return
    # Only set once: the first time they talk
    ticket_openers.setdefault(channel.id, author.id)


def get_ticket_opener_member(channel: discord.TextChannel) -> discord.Member | None:
    guild = channel.guild
    if guild is None:
        return None
    opener_id = ticket_openers.get(channel.id)
    if opener_id is None:
        return None
    return guild.get_member(opener_id)


async def auto_close_ticket(channel: discord.TextChannel, closer: discord.abc.User | None) -> None:
    """
    Close a ticket channel after confirmation.
    Only opener or staff (AI_CONTROL_ROLES) are allowed to confirm.
    """
    opener_id = ticket_openers.get(channel.id)

    is_staff = isinstance(closer, discord.Member) and any(
        r.id in AI_CONTROL_ROLES for r in closer.roles
    )

    if opener_id is not None and closer is not None:
        if closer.id != opener_id and not is_staff:
            await channel.send("❌ Only the ticket owner or staff can close this ticket.")
            ticket_close_pending.discard(channel.id)
            return

    # If there is no opener recorded and closer is not staff, deny
    if opener_id is None and closer is not None and not is_staff:
        await channel.send("❌ Only staff can close this ticket.")
        ticket_close_pending.discard(channel.id)
        return

    ticket_openers.pop(channel.id, None)
    ticket_close_pending.discard(channel.id)

    await channel.send("✅ Got it — I’ll close this ticket in 5 seconds.")
    await asyncio.sleep(5)
    try:
        await channel.delete(
            reason=f"Ticket closed by {closer} via AI confirmation" if closer else "Ticket auto-closed by AI"
        )
    except Exception as e:
        print(f"[TICKETS] Failed to delete ticket channel {channel.id}: {e}")


async def maybe_handle_close_message(message: discord.Message) -> bool:
    """
    Handle "you can close" and YES confirmations inside ticket channels.
    Returns True if it handled the message and **nothing else** should run.
    """
    channel = message.channel

    if not isinstance(channel, discord.TextChannel):
        return False
    if not is_ticket_channel(channel):
        return False
    if message.author.bot:
        return False

    raw_content = message.content or ""
    stripped = raw_content.strip()
    if not stripped:
        return False

    # Already asked "Do you want me to close this ticket?"
    if channel.id in ticket_close_pending:
        if stripped.lower() in CLOSE_CONFIRM_WORDS:
            await auto_close_ticket(channel, message.author)
        else:
            await channel.send("❌ Got it, I’ll keep this ticket open and continue helping.")
        ticket_close_pending.discard(channel.id)
        return True

    # Look for close patterns
    lowered = stripped.lower()
    for pattern in CLOSE_PATTERNS:
        if pattern in lowered:
            await channel.send(
                "Understood. Do you want me to close this ticket now? Reply **YES** to confirm."
            )
            ticket_close_pending.add(channel.id)
            return True

    return False


async def handle_ticket_claim_message(message: discord.Message) -> None:
    """
    When STARZ TICKETS posts a 'Claimed Ticket' embed, rename the ticket
    channel to '{claimer}-{opener}-{ticketNumber}'.

    - claimer  = admin/staff who claimed the ticket
    - opener   = player who opened the ticket (tracked via note_ticket_opener)
    - ticket # = number from the original ticket name, e.g. 'ticket-8239' -> 8239
    """
    channel = message.channel

    if not isinstance(channel, discord.TextChannel):
        return
    if not is_ticket_channel(channel):
        return
    if not message.embeds:
        return

    # Only react to bot messages (STARZ TICKETS)
    if not message.author.bot:
        return

    embed = message.embeds[0]
    title = embed.title or ""
    if "claimed ticket" not in title.lower():
        return

    handler: discord.Member | None = None

    # 1) Normal path: real mentions on the message
    if message.mentions:
        m = message.mentions[0]
        if isinstance(m, discord.Member):
            handler = m

    # 2) Fallback: parse <@1234567890> style text from the embed itself
    if handler is None and channel.guild is not None:
        blobs = []
        blobs.append(embed.description or "")
        blobs.append(embed.title or "")
        for f in embed.fields:
            blobs.append(f"{f.name} {f.value}")
        big_text = " ".join(blobs)

        m = re.search(r"<@!?(\d+)>", big_text)
        if m:
            uid = int(m.group(1))
            member = channel.guild.get_member(uid)
            if isinstance(member, discord.Member):
                handler = member

    if handler is None:
        print(
            f"[TICKETS] Claimed Ticket embed found in {channel.id} but no handler could be resolved. "
            f"Embed description: {embed.description!r}"
        )
        return

    # --- Build new channel name pieces ---

    # Claimer
    claimer_base = handler.display_name or handler.name
    claimer_slug = slugify_channel_name(claimer_base)

    # Ticket opener (may be None if we never saw them speak)
    opener_member = get_ticket_opener_member(channel)
    opener_slug = None
    if opener_member is not None:
        opener_base = opener_member.display_name or opener_member.name
        opener_slug = slugify_channel_name(opener_base)

    # Ticket number from original channel name, e.g. ticket-8239 -> 8239
    original_name = channel.name or ""
    ticket_number = None
    m_num = re.search(r"(\d+)$", original_name)
    if m_num:
        ticket_number = m_num.group(1)

    parts = [claimer_slug]
    if opener_slug:
        parts.append(opener_slug)
    if ticket_number:
        parts.append(ticket_number)

    new_name = "-".join(parts)
    # Safety trim for Discord’s 100-char limit
    if len(new_name) > 95:
        new_name = new_name[:95]

    try:
        await channel.edit(name=new_name, reason=f"Ticket claimed by {handler}")
        print(
            f"[TICKETS] Renamed ticket channel {channel.id} -> {new_name} "
            f"(claimer={handler}, opener={opener_member}, orig={original_name})."
        )
    except Exception as e:
        print(f"[TICKETS] Failed to rename ticket channel {channel.id}: {e}")
