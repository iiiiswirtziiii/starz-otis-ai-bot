# ================================================================
# admin_promotion_watch.py
# STARZ EMPIRE – Unauthorized Admin/Moderator Promotion Detection
# ================================================================

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List

import discord

from config_starz import (
    ADMIN_ENFORCEMENT_CHANNEL_ID,
    PROMOTER_ROLE_IDS,
)
from rcon_web import RCON_CONFIGS
from admin_monitor import (
    get_admin_profile,
    find_matching_admin_ids_from_text,
    get_admin_id_for_discord,
    fetch_admin_basic,
)
from bans import create_ban_record

# We treat the admin enforcement channel as the "head admin" channel
HEAD_ADMIN_CHANNEL_ID = ADMIN_ENFORCEMENT_CHANNEL_ID

# ===========================================================
# CONFIG
# ===========================================================

# Discord commands used to promote admins
DISCORD_PROMOTION_COMMANDS = [
    "!consoles adminid",
    "!consoles moderatorid",
]

# Allowed roles that *may* promote without punishment
ROLE_HEAD_ADMIN = 1345469982485516339
ROLE_ADMIN_MANAGEMENT = 1329989557512568932

SAFE_ROLES = {ROLE_HEAD_ADMIN, ROLE_ADMIN_MANAGEMENT}

# Delay between RCON commands sent to different servers
COMMAND_GAP_SECONDS = 0.5


# ===========================================================
# Extract gamertag from RCON promotion log
# Example log:
#   12/11/2025 20:27:41:LOG: [SERVER] Added [Soar sway1831] to Group [Admin]
# ===========================================================
def extract_promoted_gamertag(line: str) -> tuple[str | None, str | None]:
    """
    Parse a Rust console line for a promotion of the form:
      [SERVER] Added [NameHere] to Group [Admin]
      [SERVER] Added [NameHere] to Group [Moderator]

    Returns (player_name, group_name) or (None, None) if no match.
    """
    m = re.search(
        r"Added\s+\[(?P<name>[^\]]+)\]\s+to\s+Group\s+\[(?P<group>[^\]]+)\]",
        line,
    )
    if m:
        player_name = m.group("name").strip()
        group_name = m.group("group").strip()
        return player_name, group_name

    return None, None


# ===========================================================
# Search Discord messages to find promoter
# ===========================================================
async def find_promoter_from_discord(bot, gamertag: str) -> discord.Member | None:
    """
    Try to find who ran the !consoles adminid/moderator command
    that likely caused this promotion.

    Strategy:
    - Look back 5 minutes.
    - Prefer messages that both:
        * contain a promotion command, and
        * contain the promoted gamertag.
    - If none match the gamertag, fall back to the most recent
      message that contains a promotion command at all.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)

    fallback_match: Optional[discord.Member] = None
    fallback_timestamp: Optional[datetime] = None

    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for msg in channel.history(limit=100):
                    if msg.created_at < cutoff:
                        continue
                    if not msg.content:
                        continue

                    lower = msg.content.lower()

                    # Must contain a relevant promotion command
                    if not any(cmd in lower for cmd in DISCORD_PROMOTION_COMMANDS):
                        continue

                    # Exact match: contains promoted gamertag
                    if gamertag.lower() in lower:
                        return msg.author

                    # Otherwise fallback to most recent consoles command
                    if fallback_timestamp is None or msg.created_at > fallback_timestamp:
                        fallback_match = msg.author
                        fallback_timestamp = msg.created_at

            except Exception as e:
                print(
                    f"[ADMIN-PROMOTION] Error scanning channel {getattr(channel, 'id', '?')}: {e}"
                )
                continue

    return fallback_match


# ===========================================================
# Check if promoter has protection roles
# ===========================================================
def promoter_is_protected(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    return bool(role_ids & SAFE_ROLES)


# ===========================================================
# Send RCON command to ALL servers
# ===========================================================
async def send_rcon_all(rcon_manager, command: str) -> Dict[str, str]:
    """
    Sends the given RCON command to every server in RCON_CONFIGS.
    Returns a dict of {server_name: success or error message}
    """
    results: Dict[str, str] = {}

    for server_name in RCON_CONFIGS.keys():
        try:
            resp = await rcon_manager.send(server_name, command)
            results[server_name] = resp or "OK"
        except Exception as e:
            results[server_name] = f"ERROR: {e}"
        await asyncio.sleep(COMMAND_GAP_SECONDS)

    return results


# ===========================================================
# Fetch current player list for a single server
# ===========================================================
async def fetch_playerlist_for_server(server_name: str) -> str:
    """
    Ask the given server for its current player list via RCON.
    Uses the 'playerlist' command. Returns a trimmed text summary.
    """
    from rcon_web import rcon_manager

    try:
        resp = await rcon_manager.send(server_name, "playerlist")
        raw_msg = (
            (resp.get("Message") or "").strip() if isinstance(resp, dict) else str(resp)
        )

        if not raw_msg:
            return "No player list returned."

        if len(raw_msg) > 900:
            raw_msg = raw_msg[:900] + " ..."

        return raw_msg
    except Exception as e:
        print(f"[ADMIN-PROMOTION] Error fetching player list for {server_name}: {e}")
        return f"Error fetching player list: {e}"


# ===========================================================
# BUTTON VIEW
# ===========================================================
class PromotionDecisionView(discord.ui.View):
    """
    Head Admin review buttons attached to the promotion alert embed.
    """

    def __init__(self, promoted_player: str, promoter_player: str | None, auto_banned: List[str]):
        super().__init__(timeout=None)
        self.promoted_player = promoted_player
        self.promoter_player = promoter_player
        self.auto_banned = auto_banned

    @staticmethod
    def _is_success(per_server_results: Dict[str, str]) -> bool:
        if not per_server_results:
            return False
        for msg in per_server_results.values():
            if "error" in str(msg).lower():
                return False
        return True

    @staticmethod
    def _status_line(label: str, ts_str: str, ok: bool) -> str:
        status = "✅ successful" if ok else "❌ error"
        return f"• `{label}` | `{ts_str}` | {status}"

    # 🟢 CLEAR ADMINS – unban + adminid
    @discord.ui.button(label="Clear Admins", style=discord.ButtonStyle.success)
    async def clear_admins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        from rcon_web import rcon_manager

        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        all_results: Dict[str, Dict[str, Dict[str, str]]] = {}

        for player in self.auto_banned:
            unban_cmd = f'unban "{player}"'
            admin_cmd = f'adminid "{player}"'

            all_results[player] = {
                "unban": await send_rcon_all(rcon_manager, unban_cmd),
                "adminid": await send_rcon_all(rcon_manager, admin_cmd),
            }

        desc_lines: List[str] = [
            "The following corrective actions were run:\n",
            "**Command Status**",
        ]

        for player, cmds in all_results.items():
            desc_lines.append(f"\n**{player}**")

            unban_ok = self._is_success(cmds.get("unban", {}))
            admin_ok = self._is_success(cmds.get("adminid", {}))

            desc_lines.append(self._status_line("unbanned", ts_str, unban_ok))
            desc_lines.append(self._status_line("adminid", ts_str, admin_ok))

        embed = discord.Embed(
            title="🟢 Admins Restored",
            description="\n".join(desc_lines),
            color=discord.Color.green(),
        )

        await interaction.followup.send(embed=embed, ephemeral=False)

    # 🔴 BAN ADMINS – banid
    @discord.ui.button(label="Ban Admins", style=discord.ButtonStyle.danger)
    async def ban_admins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        from rcon_web import rcon_manager

        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        players = [p for p in self.auto_banned if p]
        all_results: Dict[str, Dict[str, Dict[str, str]]] = {}

        for p in players:
            ban_cmd = f'banid "{p}"'
            all_results[p] = {"banid": await send_rcon_all(rcon_manager, ban_cmd)}

        desc_lines: List[str] = [
            "The following ban actions were run:\n",
            "**Command Status**",
        ]

        for player, cmds in all_results.items():
            desc_lines.append(f"\n**{player}**")
            ban_ok = self._is_success(cmds.get("banid", {}))
            desc_lines.append(self._status_line("banid", ts_str, ban_ok))

        embed = discord.Embed(
            title="🔴 Admins Fully Banned",
            description="\n".join(desc_lines),
            color=discord.Color.red(),
        )

        await interaction.followup.send(embed=embed, ephemeral=False)


# ===========================================================
# Build the head-admin embed with buttons
# ===========================================================
async def send_promotion_embed(
    bot,
    promoted: str,
    promoter: str | None,
    server: str,
    time_detected: float,
    cmd_results_initial: Dict[str, Dict[str, Dict[str, str]]],
    reason: str,
    auto_banned_players: List[str],
    playerlist_snapshot: str | None = None,
):
    channel = bot.get_channel(HEAD_ADMIN_CHANNEL_ID)
    if not channel:
        print("[ADMIN-PROMOTION] ERROR: Head admin channel not found")
        return

    dt_full = datetime.utcfromtimestamp(time_detected)
    dt_str = dt_full.strftime("%Y-%m-%d %H:%M:%S UTC")
    time_only_str = dt_full.strftime("%H:%M:%S UTC")

    promoter_display = promoter if promoter else "Unknown (no matching Discord command found)"

    base_desc = (
        f"🚨 **UNAUTHORIZED ADMIN/MOD PROMOTION DETECTED**\n\n"
        f"**Promoted Player:** `{promoted}`\n"
        f"**Promoter:** `{promoter_display}`\n"
        f"**Server:** `{server}`\n"
        f"**Time:** `{dt_str}`\n\n"
        f"**Action Taken:** Auto-ban applied to: "
        f"`{', '.join(auto_banned_players) or 'None'}`\n"
        f"**Reason:** `{reason}`\n"
    )

    embed = discord.Embed(
        title="STARZ SECURITY — Unauthorized Promotion",
        description=base_desc,
        color=discord.Color.red(),
    )

    # Neater command results
    result_lines: List[str] = []
    for player in auto_banned_players:
        cmd_map = cmd_results_initial.get(player, {})
        if not cmd_map:
            continue

        result_lines.append(f"**{player}**")

        per_server_ban = cmd_map.get("banid")
        if per_server_ban:
            ok = True
            for resp in per_server_ban.values():
                if isinstance(resp, str) and resp.startswith("ERROR"):
                    ok = False
                    break
            status = "✅ successful" if ok else "❌ not successful"
            result_lines.append(f"• `banned` | `{time_only_str}` | {status}")

        per_server_vip = cmd_map.get("vipid")
        if per_server_vip:
            ok = True
            for resp in per_server_vip.values():
                if isinstance(resp, str) and resp.startswith("ERROR"):
                    ok = False
                    break
            status = "✅ successful" if ok else "❌ not successful"
            result_lines.append(f"• `vip` | `{time_only_str}` | {status}")

        result_lines.append("")

    if result_lines:
        embed.add_field(name="Command Results", value="\n".join(result_lines), inline=False)

    view = PromotionDecisionView(
        promoted_player=promoted,
        promoter_player=promoter,
        auto_banned=auto_banned_players,
    )

    await channel.send(embed=embed, view=view)


# ===========================================================
# MAIN HANDLER — Called from bot.py RCON console watcher
# ===========================================================
async def maybe_handle_admin_promotion(
    bot,
    server_name: str,
    msg_text: str,
    created_at_ts: float,
) -> None:
    # Parse "Added [X] to Group [Admin|Moderator]"
    promoted, group = extract_promoted_gamertag(msg_text)
    if not promoted or not group:
        return

    group_lower = group.lower()
    if group_lower not in ("admin", "moderator"):
        return

    print(f"[ADMIN-PROMOTION] Detected promotion: {promoted} to group {group} on {server_name}")

    # 1) Playerlist snapshot
    playerlist_snapshot = await fetch_playerlist_for_server(server_name)

    # 2) Who ran the consoles command?
    promoter_member = await find_promoter_from_discord(bot, promoted)

    # Build promoter identity (prefer DB main gamertag if registered)
    promoter_name: Optional[str] = None
    is_registered_promoter = False

    if promoter_member is not None:
        promoter_admin_id = get_admin_id_for_discord(promoter_member.id)
        has_promoter_role = any(r.id in PROMOTER_ROLE_IDS for r in promoter_member.roles)

        is_registered_promoter = (promoter_admin_id is not None and has_promoter_role)

        # Prefer their registered main gamertag (more likely to match console/RCON)
        if promoter_admin_id is not None:
            basic = fetch_admin_basic(promoter_admin_id)
            if basic and basic.get("main_gamertag"):
                promoter_name = str(basic["main_gamertag"]).strip()

        # Fallback to Discord display name
        if not promoter_name:
            promoter_name = promoter_member.display_name

    # 3) SAFE ROLE path:
    #    - still no auto-bans
    #    - BUT only notify Head Admin if promoter is a REGISTERED promoter (your rule)
    if promoter_member and promoter_is_protected(promoter_member):
        print("[ADMIN-PROMOTION] SAFE ROLE detected — no auto-bans.")
        if is_registered_promoter:
            await send_promotion_embed(
                bot=bot,
                promoted=promoted,
                promoter=promoter_name,
                server=server_name,
                time_detected=created_at_ts,
                cmd_results_initial={},
                reason="Promotion by protected role (registered promoter triggered review)",
                auto_banned_players=[],
                playerlist_snapshot=playerlist_snapshot,
            )
        return

    # 4) Find registered admins currently online (from playerlist snapshot)
    suspected_from_playerlist: set[str] = set()
    if playerlist_snapshot and "Error fetching" not in playerlist_snapshot:
        admin_ids = find_matching_admin_ids_from_text(playerlist_snapshot)
        for aid in admin_ids:
            profile = get_admin_profile(aid)
            if profile and profile.get("gamertag"):
                suspected_from_playerlist.add(profile["gamertag"])

    # 5) Who do we ban? (your current behavior)
    players_to_ban: set[str] = {promoted}
    if promoter_name:
        players_to_ban.add(promoter_name)
    players_to_ban |= suspected_from_playerlist

    players_to_ban_list = sorted(players_to_ban)

    # 6) Run RCON bans + VIP flags
    from rcon_web import rcon_manager  # local import to avoid circulars

    cmd_results_initial: Dict[str, Dict[str, Dict[str, str]]] = {}

    for p in players_to_ban_list:
        ban_cmd = f'banid "{p}"'
        vip_cmd = f'vipid "{p}"'
        cmd_results_initial[p] = {
            "banid": await send_rcon_all(rcon_manager, ban_cmd),
            "vipid": await send_rcon_all(rcon_manager, vip_cmd),
        }

    # 7) Save bans in DB
    for p in players_to_ban_list:
        try:
            offense_tier, expires_at_ts, duration_text = create_ban_record(
                gamertag=p,
                discord_id=None,
                reason="Unauthorized admin/moderator promotion",
                source="auto_admin_promotion",
                moderator_id=None,
            )
            print(
                f"[ADMIN-PROMOTION] Ban record created for {p} "
                f"(tier {offense_tier}, duration={duration_text})."
            )
        except Exception as e:
            print(f"[ADMIN-PROMOTION] create_ban_record failed for {p}: {e}")

    # 8) HEAD ADMIN EMBED GATE (your rule):
    # Only send the Head Admin review embed if the promoter is a REGISTERED promoter.
    if is_registered_promoter:
        await send_promotion_embed(
            bot=bot,
            promoted=promoted,
            promoter=promoter_name,
            server=server_name,
            time_detected=created_at_ts,
            cmd_results_initial=cmd_results_initial,
            reason="Unauthorized admin/moderator promotion",
            auto_banned_players=players_to_ban_list,
            playerlist_snapshot=playerlist_snapshot,
        )
    else:
        print("[ADMIN-PROMOTION] Not alerting Head Admin (promoter not a registered promoter).")
