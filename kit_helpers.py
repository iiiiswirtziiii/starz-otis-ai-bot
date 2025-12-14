# kit_helpers.py
"""
Kit claim intelligence for STARZ:
- Load kit claim instructions from kit_claims.txt
- Detect kit-related questions / issues
- Reply with exact Quickchat commands (no contents)
- Format responses in an OTIS-style embed
"""

from __future__ import annotations

import os
import re
from typing import Dict, Optional, List

import discord

# Path to the claims file (relative to the working directory)
KIT_CLAIMS_FILE = os.getenv("KIT_CLAIMS_FILE", "kit_claims.txt")

# Global store: "elitekit23" -> {name, claim, step1, step2}
kit_claims: Dict[str, Dict[str, Optional[str]]] = {}

# Phrases that clearly mean "how do I claim my kit?"
KIT_CLAIM_KEYWORDS = (
    "how do i claim my kit",
    "how to claim my kit",
    "how do i claim a kit",
    "how to claim a kit",
    "how do i claim elitekit",
    "how to claim elitekit",
    "claim my kit",
    "kit claim",
    "my kit isnt working",
    "my kit isn't working",
    "kit bugged",
)


# Phrases that look like problems with kits
KIT_ISSUE_KEYWORDS = (
    "kit not working",
    "kit isnt working",
    "kit isn't working",
    "kit bugged",
    "kit broken",
    "missing kit",
    "didn't get my kit",
    "didnt get my kit",
    "no kit",
    "no perms for kit",
    "no permission for kit",
    "no permissions for kit",
    "wont let me claim",
    "won't let me claim",
)



def load_kit_claims_text() -> None:
    """
    Load kit claim instructions from KIT_CLAIMS_FILE into kit_claims.

    File format (yours):

        [elitekit6]
        name: boosting kit
        claim: I Need Water

        [elitekit23]
        name: oil rat
        step1: I'm Outta Ammo
        step2: I Need High Quality Metal
    """
    global kit_claims
    kit_claims = {}

    if not os.path.exists(KIT_CLAIMS_FILE):
        print(f"[KIT CLAIMS] File not found: {KIT_CLAIMS_FILE}")
        return

    current_key: Optional[str] = None
    current_block: Dict[str, Optional[str]] = {}

    with open(KIT_CLAIMS_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # Section header: [elitekit41]
            m = re.match(r"\[(.+?)\]", line)
            if m:
                # flush previous
                if current_key and current_block:
                    kit_claims[current_key] = current_block
                current_key = m.group(1).strip().lower()
                current_block = {
                    "name": None,
                    "claim": None,
                    "step1": None,
                    "step2": None,
                }
                continue

            if ":" not in line or current_key is None:
                continue

            field, value = [x.strip() for x in line.split(":", 1)]
            field = field.lower()
            if field == "name":
                current_block["name"] = value
            elif field == "claim":
                current_block["claim"] = value
            elif field == "step1":
                current_block["step1"] = value
            elif field == "step2":
                current_block["step2"] = value

    if current_key and current_block:
        kit_claims[current_key] = current_block

    print(f"[KIT CLAIMS] Loaded {len(kit_claims)} kit claim entries from {KIT_CLAIMS_FILE}.")


def _normalize_elite_key_from_match(num_str: str) -> str:
    """Turn a number string like '4' into 'elitekit4'."""
    num_str = num_str.lstrip("0")
    if not num_str:
        num_str = "0"
    return f"elitekit{num_str}"


def detect_kit_keys_in_text(text: str) -> List[str]:
    """
    Look for known kit references inside free text.
    - elite kit / elitekit / ek + number → elitekit<number>
    - direct key match (elitekit23)
    - kit name tokens from kit_claims ("mega raider", "boosting kit", etc.)
    Returns a list of unique kit keys in the order we discovered them.
    """
    lt = text.lower()
    found: List[str] = []

    # 1) elite kit / elitekit / ek patterns with a number
    for m in re.finditer(r"\b(?:elite\s*kit|elitekit|ek)\s*([0-9]{1,2})\b", lt):
        key = _normalize_elite_key_from_match(m.group(1))
        if key in kit_claims and key not in found:
            found.append(key)

    # 2) direct "elitekit23" style mentions
    for m in re.finditer(r"\belitekit\s*([0-9]{1,2})\b", lt):
        key = _normalize_elite_key_from_match(m.group(1))
        if key in kit_claims and key not in found:
            found.append(key)

    # 3) match by kit "name" field content
    for key, data in kit_claims.items():
        name = (data.get("name") or "").strip().lower()
        if not name:
            continue
        tokens = [t for t in re.split(r"\s+", name) if t and t not in {"kit", "elitekit"}]
        if tokens and all(t in lt for t in tokens):
            if key not in found:
                found.append(key)

    return found


def normalize_role_to_kit_key(role_name: str) -> Optional[str]:
    """
    Turn a Discord role name like:
      - 'EliteKit 13'
      - 'elite kit 13'
      - '3x-elitekit13'
      - '⭐ EliteKit 13 ⭐'
      - 'Boosting Kit'
    into a key like 'elitekit13' (or whatever matches in kit_claims).
    """
    rn_lower = role_name.lower()

    # Strip everything except letters/numbers
    rn_norm = re.sub(r"[^a-z0-9]", "", rn_lower)

    # 1) elitekit<number> or ek<number> anywhere
    m = re.search(r"(?:elitekit|ek)([0-9]{1,2})", rn_norm)
    if m:
        num = m.group(1).lstrip("0") or "0"
        key = f"elitekit{num}"
        if key in kit_claims:
            return key

    # 2) If any known kit key is a substring (e.g. 's13xelitekit6')
    for key in kit_claims.keys():
        if key in rn_norm:
            return key

    # 3) Match against known "name" fields (Boosting Kit, Mega Raider, etc.)
    for key, data in kit_claims.items():
        name = (data.get("name") or "").strip().lower()
        if not name:
            continue
        tokens = [t for t in re.split(r"\s+", name) if t and t not in {"kit", "elitekit"}]
        if tokens and all(t in rn_lower for t in tokens):
            return key

    return None


def build_claim_instruction_for_key(key: str) -> Optional[str]:
    """
    Build one block of text for a given kit key.

    Style:

        🔥 **Kit Name**
        `Quickchat1` → `Quickchat2`
    """
    data = kit_claims.get(key)
    if not data:
        return None

    name = data.get("name") or key
    claim = data.get("claim")
    step1 = data.get("step1")
    step2 = data.get("step2")

    # Single-phrase claim
    if claim:
        claim = claim.strip()
        if not claim:
            return None
        return f"🔥 **{name}**\n`{claim}`"

    # Two-step claim
    if step1 and step2:
        step1 = step1.strip()
        step2 = step2.strip()
        if not step1 or not step2:
            return None
        return f"🔥 **{name}**\n`{step1}` → `{step2}`"

    # Fallback if something is mis-configured
    return f"🔥 **{name}** – claim steps are not fully configured yet. Ping staff to fix this kit."


async def send_kit_instructions_for_member_roles(
    channel: discord.TextChannel,
    member: discord.Member,
) -> bool:
    """
    Look at the member's roles, map them to known kit keys, and send claim instructions.
    Returns True if we were able to send any kit info.
    """
    keys: List[str] = []
    for role in member.roles:
        key = normalize_role_to_kit_key(role.name)
        if not key:
            continue
        if key not in kit_claims:
            continue
        if key not in keys:
            keys.append(key)

    if not keys:
        return False

    lines: List[str] = []
    if len(keys) == 1:
        instr = build_claim_instruction_for_key(keys[0])
        if not instr:
            return False
        lines.append("Here’s how to claim your kit:\n")
        lines.append(instr)
    else:
        lines.append("I can see you have these kits. Here’s how to claim each one:\n")
        for key in keys:
            instr = build_claim_instruction_for_key(key)
            if instr:
                lines.append(instr)
                lines.append("")  # blank line between kits

    lines.append(
        "\nIf **any of those kits still don’t work**, tell me which one and what happens "
        "when you try to claim it."
    )
    msg = "\n".join(lines).strip()

    # 🔴 NEW: send as embed instead of plain text
    embed = discord.Embed(
        description=msg,
        color=0xE74C3C,  # STARZ red
    )
    embed.set_author(name="OTIS ‖ AI ADMIN")

    await channel.send(embed=embed)
    return True


async def send_kit_instructions_for_text(
    channel: discord.TextChannel,
    content: str,
) -> bool:
    """
    Use plain text (no roles) to detect kit names and send instructions.
    Returns True if we successfully found at least one kit.
    """
    keys = detect_kit_keys_in_text(content)
    if not keys:
        return False

    lines: List[str] = []
    if len(keys) == 1:
        instr = build_claim_instruction_for_key(keys[0])
        if not instr:
            return False
        lines.append("Here’s how to claim your kit:\n")
        lines.append(instr)
    else:
        lines.append("Here’s how to claim the kits you mentioned:\n")
        for key in keys:
            instr = build_claim_instruction_for_key(key)
            if instr:
                lines.append(instr)
                lines.append("")

    lines.append(
        "\nIf **any of those kits still don’t work**, tell me which one and what happens "
        "when you try to claim it."
    )
    msg = "\n".join(lines).strip()

    embed = discord.Embed(
        description=msg,
        color=0xE74C3C,
    )
    embed.set_author(name="OTIS ‖ AI ADMIN")

    await channel.send(embed=embed)
    return True


def looks_like_kit_question(text: str) -> bool:
    """
    Only treat as a kit question if:
    - The message clearly talks about 'kit' / 'elitekit', AND
    - It matches one of our claim/help patterns.
    """
    lt = text.lower()

    if "kit" not in lt and "elitekit" not in lt and "vip kit" not in lt:
        return False

    return any(k in lt for k in KIT_CLAIM_KEYWORDS)



def looks_like_kit_issue(text: str) -> bool:
    """
    Only treat as a kit *issue* if they mention 'kit' AND a problem keyword.
    """
    lt = text.lower()
    if "kit" not in lt and "elitekit" not in lt and "vip kit" not in lt:
        return False

    return any(k in lt for k in KIT_ISSUE_KEYWORDS)




async def kit_first_help(
    message: discord.Message,
    channel: discord.TextChannel,
    content: str,
) -> bool:

    """
    First-line helper for kit questions.

    Rules:
    - If the message explicitly names one or more kits (e.g. 'ultimate raid kit'),
      we answer ONLY for those kits (text-based).
    - Otherwise, for generic questions like 'how do I claim my kit', we use the
      member's roles and show ALL their kits.
    - If the member has 2x kits, we explain that 2x uses the same commands as 3x.
    - If the member has BOTH 2x and 3x kits, we ask which kits they need help with.
    - We only ever send ONE reply per message.
    """
    lt = content.lower()

    # Only trigger on clearly kit-related messages
    if not (looks_like_kit_question(lt) or looks_like_kit_issue(lt)):
        return False

    # 1) If they explicitly mention kit names/numbers in the text, use that first.
    explicit_keys = detect_kit_keys_in_text(lt)
    if explicit_keys:
        blocks: List[str] = []
        for key in explicit_keys:
            block = build_claim_instruction_for_key(key)
            if block:
                blocks.append(block)

        if not blocks:
            # We detected something kit-like but couldn't map it – fallback message.
            fallback = (
                "I tried to find claim instructions for that kit but couldn't match it "
                "to our kit list. Please double-check the kit name/number or tell me "
                "which **EliteKit number** you’re asking about."
            )
            embed = discord.Embed(
                description=fallback,
                color=0xE74C3C,
            )
            embed.set_author(name="OTIS ‖ AI ADMIN")
            await channel.send(embed=embed)
            return True

        desc = "\n\n".join(blocks)
        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • KIT CLAIM HELP",
            description=desc,
            color=0xE74C3C,
        )
        embed.set_footer(text="Quickchat steps only — kit contents are not shown here.")
        await channel.send(embed=embed)
        return True

    # 2) Generic question: use their roles to show ALL kits they own.
    member = message.author
    if not isinstance(member, discord.Member):
        return False

    owned_keys: List[str] = []
    two_x_roles: List[discord.Role] = []
    three_x_roles: List[discord.Role] = []

    for role in member.roles:
        rn = role.name
        rn_lower = rn.lower()

        # Track 2x vs 3x kit roles
        if "2x-elitekit" in rn_lower:
            two_x_roles.append(role)
        if "3x-elitekit" in rn_lower:
            three_x_roles.append(role)

        # Map role → kit key (elitekit1, elitekit2, etc.)
        key = normalize_role_to_kit_key(rn)
        if key and key not in owned_keys:
            owned_keys.append(key)

    if not owned_keys:
        # No kit roles found – ask them to specify
        no_kits_text = (
            "I don't see any EliteKit roles on your account. "
            "Please tell me which kit name or EliteKit number you need help with."
        )
        embed = discord.Embed(
            description=no_kits_text,
            color=0xE74C3C,
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")
        await channel.send(embed=embed)
        return True

    # Build a short note about 2x vs 3x depending on what they own
    role_note_lines: List[str] = []

    if two_x_roles and not three_x_roles:
        # Only 2x kits
        role_note_lines.append(
            "I see you have **2x kits**. 2x kits use the same Quickchat claim commands as our 3x kits "
            "for the same EliteKit number."
        )
    elif two_x_roles and three_x_roles:
        # Both 2x and 3x
        role_note_lines.append(
            "I see you have **both 2x and 3x kits**. They use the same Quickchat claim commands for the "
            "same EliteKit number. If a specific kit still doesn’t work, tell me which **2x or 3x kit** "
            "you need help with."
        )

    # Build claim instructions for all owned kits
    claim_blocks: List[str] = []
    for key in owned_keys:
        block = build_claim_instruction_for_key(key)
        if block:
            claim_blocks.append(block)

    if not claim_blocks:
        fallback = (
            "You have kit roles, but I couldn't build claim instructions for them. "
            "Please tell me which EliteKit number you’re trying to claim so staff can double-check the config."
        )
        embed = discord.Embed(
            description=fallback,
            color=0xE74C3C,
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")
        await channel.send(embed=embed)
        return True

    description_parts: List[str] = []
    if role_note_lines:
        description_parts.append("\n".join(role_note_lines))
    description_parts.append("\n\n".join(claim_blocks))

    embed = discord.Embed(
        title="OTIS ‖ AI ADMIN • YOUR KIT CLAIM COMMANDS",
        description="\n\n".join(description_parts),
        color=0xE74C3C,
    )
    embed.set_footer(text="Quickchat steps only — kit contents are not shown here.")

    await channel.send(embed=embed)
    return True





# Load once when module is imported
load_kit_claims_text()
