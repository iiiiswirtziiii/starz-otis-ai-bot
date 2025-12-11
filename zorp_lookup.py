"""
Helpers to look up ZORP / offline zone events for a player
based on the ZORP feed channels.

Goal:
- Scan all ZORP_FEED_CHANNEL_IDS
- Find messages whose embed text mentions this player's name/nick
- Return the most recent ones so the bot can show
  "this is why your zone was deleted".
"""

from __future__ import annotations

from typing import List, Optional, Set

import logging
import discord

from config_starz import ZORP_FEED_CHANNEL_IDS

log = logging.getLogger(__name__)


def _build_name_set(member: discord.Member) -> Set[str]:
    """
    Build a set of lowercase strings we’ll try to match inside the
    ZORP embed text. We include:
    - display_name
    - username
    - nick (if any)
    - tokens split on spaces
    """
    names: Set[str] = set()

    def add(s: Optional[str]) -> None:
        if not s:
            return
        s = s.strip()
        if len(s) < 3:
            return
        names.add(s.lower())
        for part in s.split():
            part = part.strip()
            if len(part) >= 3:
                names.add(part.lower())

    add(member.display_name)
    add(member.name)
    # nick is usually already in display_name, but just in case:
    if hasattr(member, "nick") and member.nick:
        add(member.nick)

    return names


async def find_zorp_events_for_member(
    bot: discord.Client,
    member: discord.Member,
    limit: int = 100,
) -> List[discord.Message]:
    """
    Look through all ZORP_FEED_CHANNEL_IDS for embeds that reference this member.

    Strategy:
    - Build a set of possible name strings for the member
    - For each ZORP channel, scan recent messages (up to `limit`)
    - Build a big lowercase text blob from message content + embed title,
      description, and all field names/values
    - If ANY of the member's name strings appear in the blob, treat it
      as a match.
    - Return messages sorted newest → oldest.
    """
    target_names = _build_name_set(member)
    if not target_names:
        log.debug("[ZORP] No target names built for member %s (%s)", member.id, member)
        return []

    log.debug("[ZORP] Looking up events for member %s (%s) names=%s", member.id, member, target_names)

    matches: List[discord.Message] = []

    for ch_id in ZORP_FEED_CHANNEL_IDS:
        channel = bot.get_channel(ch_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            continue

        try:
            async for msg in channel.history(limit=limit):
                if not msg.embeds:
                    continue

                # Build text blob from content + all embed pieces
                parts: list[str] = []
                if msg.content:
                    parts.append(msg.content)

                for e in msg.embeds:
                    if e.title:
                        parts.append(e.title)
                    if e.description:
                        parts.append(e.description)
                    for f in e.fields:
                        if f.name:
                            parts.append(str(f.name))
                        if f.value:
                            parts.append(str(f.value))

                blob = " ".join(parts).lower()
                if not blob:
                    continue

                if any(name in blob for name in target_names):
                    matches.append(msg)

        except Exception as e:
            log.exception("[ZORP] error scanning channel %s: %s", ch_id, e)

    # Newest first
    matches.sort(key=lambda m: m.created_at, reverse=True)
    log.debug("[ZORP] Found %d matching ZORP messages for %s", len(matches), member)
    return matches


def summarize_zorp_event(msg: discord.Message) -> Optional[str]:
    """
    Pull STATUS / REASON / LEADER lines out of the ZORP embed and
    turn them into a single human-readable sentence.

    This matches the embeds in your screenshot, which look like:

        ZORP REPORT
        • STATUS: zone expired (deleted)
        ZONE DETAILS
        • ZONE: ((name))
        • LEADER: someone
        ...
    """
    if not msg.embeds:
        return None

    e = msg.embeds[0]
    lines: list[str] = []

    if e.description:
        lines.extend(e.description.splitlines())

    for f in e.fields:
        if f.name:
            lines.append(str(f.name))
        if f.value:
            lines.extend(str(f.value).splitlines())

    status = None
    reason = None
    leader = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("STATUS:"):
            status = line.split(":", 1)[1].strip()
        elif upper.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
        elif upper.startswith("LEADER:"):
            leader = line.split(":", 1)[1].strip()

    if not status and not reason and not leader:
        return None

    pieces: list[str] = []
    if leader:
        pieces.append(f"Leader in that ZORP entry: **{leader}**.")
    if status:
        pieces.append(f"Status: **{status}**.")
    if reason:
        pieces.append(f"Reason: **{reason}**.")

    return " ".join(pieces) if pieces else None