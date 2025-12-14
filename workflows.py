from __future__ import annotations

from typing import Dict, Any, List, Optional

import discord

from config_starz import (
    TRIAL_ADMIN_ID,
    SERVER_ADMIN_ID,
    HEAD_ADMIN_ID,
    ADMIN_MANAGEMENT_ID,
    KAOS_MOD_ID,
    STAFF_ALERT_CHANNEL_ID,
)
from ticket_helpers import get_ticket_opener_member

# ====== KEYWORDS FOR WORKFLOWS ======

ADMIN_ABUSE_KEYWORDS = (
    "admin abuse",
    "abusive admin",
    "admin spawning",
    "admin spawned",
    "admin raided",
    "admin cheating",
    "admin cheated",
    "admin using admin powers",
    "admin used admin",
    "corrupt admin",
    "admin griefed",
    "admin grieved",
    "admin wiped",
)

KIT_ISSUE_WORKFLOW_KEYWORDS = (
    "kit not working",
    "kit isn't working",
    "kit isnt working",
    "kit never re-added",
    "kit never readded",
    "missing kit",
    "missing my kit",
    "no kit access",
    "lost my kit",
    "removed my kit",
    "kit was removed",
    "kit got removed",
    "kit got taken",
    "re-add my kit",
    "re add my kit",
    "readded to my perms",
    "re-added to my perms",
)

REFUND_KEYWORDS = (
    "refund",
    "refunded",
    "chargeback",
    "charge back",
    "money back",
    "bought a kit",
    "purchased a kit",
    "purchased a bundle",
    "bought a bundle",
    "double charged",
    "charged twice",
    "wrong amount",
)

ZORP_ISSUE_KEYWORDS = (
    "offline zone",
    "offline bubble",
    "red bubble",
    "red orp",
    "red zorp",
    "zorp bubble",
)


# ====== WORKFLOW DEFINITIONS ======

workflow_questions: Dict[str, List[str]] = {
    "admin_abuse": [
        "IGN (in-game name)?",
        "Which STARZ server did this happen on? (Example: NA 3x Trio, EU 2x Main)",
        "About when did this happen? (include timezone if you can)",
        "Admin name / gamertag?",
        "Briefly explain what happened (2–4 sentences).",
        "Please paste any links to clips, screenshots, or VODs (Twitch, YouTube, etc.).",
    ],
    "kit_issue": [
        "IGN (in-game name)?",
        "Which STARZ server are you on? (Example: NA 3x Trio, EU 2x Main)",
        "Which kit or bundle is having issues? (name from Discord / Kaos / Tip4Serv)",
        "What exactly happens when you try to claim it? (error message, nothing happens, wrong items, etc.)",
        "About when did you purchase this kit? (date and approximate time, with timezone)",
        "Please paste any screenshots or clips showing the problem or your purchase receipt.",
    ],
    "refund_request": [
        "IGN (in-game name)?",
        "Which STARZ server is this about? (Example: NA 3x Trio, EU 2x Main)",
        "What exactly did you purchase? (kit/bundle name and quantity)",
        "Where did you purchase it? (Tip4Serv website, Kaos, other)",
        "What went wrong? (charged twice, wrong kit, never received, etc.)",
        "Please paste your receipt, transaction ID, and any relevant screenshots.",
    ],
    "zorp_issue": [
        "IGN (in-game name)?",
        "Which STARZ server are you on? (Example: NA 3x Trio, EU 2x Main)",
        "What is the exact ZORP / ORP name from in-game?",
        "What grid or approximate location on the map is this happening at?",
        "About when did this happen or when did you notice the issue? (include timezone)",
        "Briefly explain what’s going wrong (can’t raid through bubble, wrong base is protected, etc.).",
        "Please paste any clips or screenshots showing the ZORP bubble and the base you’re trying to raid.",
    ],
}

workflow_roles: Dict[str, List[int]] = {
    # Who gets pinged when a workflow ticket is finalized
    "admin_abuse": [HEAD_ADMIN_ID, ADMIN_MANAGEMENT_ID],
    "kit_issue": [ADMIN_MANAGEMENT_ID, KAOS_MOD_ID],
    "refund_request": [ADMIN_MANAGEMENT_ID, KAOS_MOD_ID],
    "zorp_issue": [HEAD_ADMIN_ID, ADMIN_MANAGEMENT_ID],
}

# Per-channel workflow state
ticket_workflows: Dict[int, Dict[str, Any]] = {}

# Tracks which channels have had special admin-abuse perms applied
admin_abuse_locked_channels: set[int] = set()


# ====== PERMISSIONS: LOCK ADMIN ABUSE TICKET ======

async def apply_admin_abuse_permissions(channel: discord.TextChannel, opener: discord.Member) -> None:
    """
    Recreates old behavior:
    - Hide channel from @everyone and Trial Admin / Server Admin / Kaos Mod
    - Only show to Head Admin + Admin Management + opener
    """
    guild = channel.guild
    if guild is None:
        return

    overwrites = dict(channel.overwrites)  # copy

    everyone_role = guild.default_role
    overwrites[everyone_role] = discord.PermissionOverwrite(view_channel=False)

    # Roles that SHOULD NOT see admin abuse tickets
    hidden_roles = (TRIAL_ADMIN_ID, SERVER_ADMIN_ID, KAOS_MOD_ID)
    for rid in hidden_roles:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=False)

    # Roles that SHOULD see and reply
    visible_roles = (HEAD_ADMIN_ID, ADMIN_MANAGEMENT_ID)
    for rid in visible_roles:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    # Ticket opener
    overwrites[opener] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        await channel.edit(
            overwrites=overwrites,
            reason="Locking admin abuse ticket to management + opener",
        )
        print(f"[WORKFLOWS] Applied admin abuse perms in channel {channel.id}")
    except Exception as e:
        print(f"[WORKFLOWS] Failed to apply admin abuse perms in channel {channel.id}: {e}")


# ====== START / ASK / FINALIZE ======

async def _start_workflow_generic(
    category: str,
    channel: discord.TextChannel,
    opener: Optional[discord.Member] = None,
) -> None:
    """
    Initialize any workflow category and ask the first question.
    For admin_abuse we also apply the special permissions.
    """
    global ticket_workflows, admin_abuse_locked_channels

    ticket_workflows[channel.id] = {
        "category": category,
        "step": 0,
        "answers": {},
    }

    opener_member = opener or get_ticket_opener_member(channel)

    if category == "admin_abuse" and opener_member is not None and channel.id not in admin_abuse_locked_channels:
        await apply_admin_abuse_permissions(channel, opener_member)
        admin_abuse_locked_channels.add(channel.id)

    if category == "admin_abuse":
        await channel.send(
            "This sounds like a possible **admin abuse** report. "
            "I’ll ask a few quick questions in order so management can review everything clearly."
        )
    elif category == "kit_issue":
        await channel.send(
            "It looks like your kit might still be broken even after normal claim instructions. "
            "I’ll gather some details so **Kaos mods / management** can fix or re-add it."
        )
    elif category == "refund_request":
        await channel.send(
            "This sounds like a **refund / purchase issue**. "
            "I’ll ask a few questions so management can review your purchase and make a decision."
        )
    elif category == "zorp_issue":
        await channel.send(
            "This sounds like a **ZORP / offline zone issue**. "
            "I’ll gather details so management can review the ZORP settings and logs."
        )

    await ask_next_question(channel)


async def start_admin_abuse_workflow(
    channel: discord.TextChannel,
    opener: Optional[discord.Member] = None,
) -> None:
    await _start_workflow_generic("admin_abuse", channel, opener)


async def start_kit_issue_workflow(
    channel: discord.TextChannel,
    opener: Optional[discord.Member] = None,
) -> None:
    await _start_workflow_generic("kit_issue", channel, opener)


async def start_refund_workflow(
    channel: discord.TextChannel,
    opener: Optional[discord.Member] = None,
) -> None:
    await _start_workflow_generic("refund_request", channel, opener)


async def start_zorp_issue_workflow(
    channel: discord.TextChannel,
    opener: Optional[discord.Member] = None,
) -> None:
    await _start_workflow_generic("zorp_issue", channel, opener)


async def ask_next_question(channel: discord.TextChannel) -> None:
    """
    Send the next question in the active workflow.
    """
    s = ticket_workflows.get(channel.id)
    if not s:
        return

    category = s.get("category")
    step = int(s.get("step", 0))
    questions = workflow_questions.get(category or "", [])
    if not questions:
        return

    if step >= len(questions):
        # Will be finalized by process_workflow_answer after last answer
        return

    question = questions[step]
    total = len(questions)
    await channel.send(f"**Q{step + 1}/{total}:** {question}")


async def finalize_workflow(bot: discord.Client, channel: discord.TextChannel) -> None:
    """
    Build a summary embed of all Q/A and send it to STAFF_ALERT_CHANNEL_ID,
    pinging the right roles based on category.
    """
    s = ticket_workflows.get(channel.id)
    if not s:
        return

    category: str = s.get("category", "admin_abuse")
    answers: Dict[int, str] = s.get("answers", {})

    questions = workflow_questions.get(category, [])
    lines: List[str] = []
    for i, q in enumerate(questions):
        a = answers.get(i, "No answer provided.")
        lines.append(f"**Q{i+1}:** {q}\n**A:** {a}")

    description = "\n\n".join(lines) or "No structured answers were captured."

    # Ensure perms are applied at least once for admin abuse
    if category == "admin_abuse" and channel.id not in admin_abuse_locked_channels:
        opener_member = get_ticket_opener_member(channel)
        if opener_member is not None:
            await apply_admin_abuse_permissions(channel, opener_member)
            admin_abuse_locked_channels.add(channel.id)

    if category == "admin_abuse":
        title = "Admin Abuse Ticket Summary"
        color = 0xE74C3C
    elif category == "kit_issue":
        title = "Kit Issue Ticket Summary"
        color = 0xF1C40F
    elif category == "refund_request":
        title = "Refund / Purchase Issue Summary"
        color = 0x3498DB
    elif category == "zorp_issue":
        title = "ZORP / Offline Zone Ticket Summary"
        color = 0x9B59B6
    else:
        title = "Ticket Workflow Summary"
        color = 0x95A5A6

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )
    embed.add_field(name="Ticket Channel", value=channel.mention, inline=False)

    staff_channel = bot.get_channel(STAFF_ALERT_CHANNEL_ID)
    mention_ids = workflow_roles.get(category, [])
    mention_text = " ".join(f"<@&{rid}>" for rid in mention_ids) if mention_ids else ""

    try:
        if isinstance(staff_channel, discord.TextChannel):
            await staff_channel.send(
                content=f"{mention_text} New **{category.replace('_', ' ')}** report summary:",
                embed=embed,
            )
        else:
            # Fallback to sending inside the ticket
            await channel.send(
                content=f"{mention_text} New **{category.replace('_', ' ')}** report summary:",
                embed=embed,
            )
    except Exception as e:
        print(f"[WORKFLOWS] Failed to send {category} summary: {e}")

    # Let the player know staff has the details
    try:
        if category == "refund_request":
            await channel.send(
                "✅ I’ve sent your refund / purchase issue details to management. "
                "They’ll review your purchase and follow up in this ticket."
            )
        elif category == "kit_issue":
            await channel.send(
                "✅ I’ve sent your kit issue details to Kaos mods / management. "
                "They’ll review your purchase and re-add or fix the kit if appropriate."
            )
        elif category == "zorp_issue":
            await channel.send(
                "✅ I’ve sent your ZORP / offline zone issue details to management. "
                "They’ll review the ZORP logs and settings and update you here."
            )
        else:
            await channel.send(
                "✅ I’ve sent your report and a summary to management. "
                "They will review the logs and follow up. You can keep using this ticket to chat with them."
            )
    except Exception:
        pass

    # Clear workflow state (perm lock stays for admin_abuse)
    ticket_workflows.pop(channel.id, None)


# ====== MESSAGE HANDLER HOOK ======

async def process_workflow_answer(
    bot: discord.Client,
    message: discord.Message,
) -> bool:
    """
    If this channel has an active workflow, treat the user's message as the
    next answer and either move to the next question or finalize.

    Returns True if this message was handled by the workflow engine.
    """
    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return False
    if message.author.bot:
        return False

    s = ticket_workflows.get(channel.id)
    if not s:
        return False

    category = s.get("category")
    if category not in workflow_questions:
        # Unknown / unsupported workflow
        return False

    content = (message.content or "").strip()
    if not content:
        return False

    step = int(s.get("step", 0))
    questions = workflow_questions.get(category, [])
    if not questions:
        return False

    # Record answer
    answers: Dict[int, str] = s.setdefault("answers", {})
    answers[step] = content

    # Advance
    step += 1
    s["step"] = step

    if step >= len(questions):
        await finalize_workflow(bot, channel)
    else:
        await ask_next_question(channel)

    return True