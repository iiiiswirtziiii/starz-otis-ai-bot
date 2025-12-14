# admin_mon_system.py
"""
Admin monitor system glue.

This sits between:
- RCON / Discord feeds
and
- admin_monitor.py (DB + embeds)

bot.py should call into this module instead of talking directly to
record_admin_event / update_admin_log_for_admin.
"""

from __future__ import annotations

from typing import Iterable, Sequence
from datetime import datetime

import discord

from admin_monitor import (
    record_admin_event,
    update_admin_log_for_admin,
)
from config_starz import ADMIN_MONITOR_LOG_CHANNEL_ID


async def log_admin_activity_for_ids(
    bot: discord.Client,
    admin_ids: Sequence[int],
    *,
    event_type: str,
    server_name: str,
    detail: str,
) -> None:
    """
    Log an event (join / spawn) for one or more admin IDs
    and refresh their Admin Monitor embeds.

    - event_type: "join" or "spawn"
    - server_name: e.g. "Server 10"
    - detail: short text ("Joined server" or console line snippet)
    """
    if not admin_ids:
        return

    for admin_id in admin_ids:
        # Write to DB
        record_admin_event(
            admin_id=admin_id,
            event_type=event_type,
            server_name=server_name,
            detail=detail,
        )

        # Update / create their Admin Actions embed + 48h file
        try:
            await update_admin_log_for_admin(
                bot=bot,
                admin_id=admin_id,
                log_channel_id=ADMIN_MONITOR_LOG_CHANNEL_ID,
            )
        except Exception as e:
            print(f"[ADMIN-MON-SYSTEM] Failed to update admin log for {admin_id}: {e}")
