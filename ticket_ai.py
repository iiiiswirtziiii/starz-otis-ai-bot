from __future__ import annotations

from typing import Dict, Any, List, Tuple

from datetime import datetime, timedelta, timezone
import discord

from bans import lookup_ban_status_by_gamertag, describe_next_offense

# ==========================
# SCRAP INFORMATION TEXT
# ==========================
SCRAP_INFO = """
**SCRAP (STARZ Discord Currency)**

- Earn SCRAP from airdrops, giveaways, events, and other Discord activities.
- Spend SCRAP in our KAOS shop / Discord shop to get in-game kits, points, or rewards.
- SCRAP is *not* the same as in-game Rust scrap; it only exists in the STARZ Discord economy.
- If you have questions about SCRAP trades or balance issues, please open a ticket.
"""

# ==========================
# WIPE SCHEDULE TEXT
# ==========================
WIPE_INFO = """
__**STARZ WIPE SCHEDULE**__

**🇺🇸 U.S SERVERS**

**🇺🇸 STARZ S1 QUADS (NA) – weekly**  
Server ID: `0bfebfa`  
Wipes every **Thursday** at <t:1760558430:t>

**🇺🇸 STARZ S2 DUO (NA) – weekly**  
Server ID: `6a942d7`  
Wipes every **Thursday** at <t:1760558430:t>

**🇺🇸 STARZ S3 TRIO (NA) – weekly**  
Server ID: `eb8b488`  
Wipes every **Thursday** at <t:1760558430:t>

**🇪🇺 STARZ S4 QUADS (EU) – weekly**  
Server ID: `f4db6cb`  
Wipes every **Thursday** at <t:1760533230:t>

**🇪🇺 STARZ S5 DUO (EU) – weekly**  
Server ID: `0ba8d47`  
Wipes every **Thursday** at <t:1760533230:t>

**🇺🇸 STARZ S6 6 MAN (NA) – weekly**  
Server ID: `0b7ff4e`  
Wipes every **Thursday** at <t:1760558430:t>

**🇺🇸 STARZ S7 AIMTRAIN (NA)**  
Server ID: `3301ab7`  
(Generally does not follow the weekly wipe pattern.)

**🇺🇸 STARZ S8 2X MAIN (NA)**  
Server ID: `7a5292c`  
Wipes every **Thursday** at <t:1760558430:t>

**🇺🇸 STARZ S9 NO TEAM LIMIT (NA) – weekly**  
Server ID: `5a238c5`  
Wipes every **Thursday** at <t:1760558430:t>

**🇦🇺 STARZ S10 QUADS (OCE) – weekly**  
Server ID: `6c67165`  
Wipes every **Friday** at <t:1760587230:t>

_⏰ Times shown in **your** local timezone in Discord._
"""

# ==========================
# FREE KITS INFORMATION TEXT
# ==========================
FREE_KITS_INFO = """
__**FREE KITS – HOW TO CLAIM**__

Before you start:  
➡️ Make sure your **Discord is linked** to the KAOS shop.

---

**🔫 MP5 ROADSIGN – FREE HOURLY KIT**

- **Servers:** All STARZ servers 🌍  
- **Cooldown:** Claim every **1 hour**

**In-game Quick Chat steps:**
1️⃣ “I'm outta ammo”  
2️⃣ “I have water”

**How to claim on the website:**  
- Go to the **website**  
- Open the **3x store**  
- Go to **Hourly Kits**  
- Press **“Buy”** on the MP5 Roads Sign kit

---

**💥 2X2 RAIDER – FREE 24H RAID KIT**

- **Servers:** All STARZ servers 🌍  
- **Cooldown:** Claim every **24 hours**

**In-game Quick Chat steps:**
1️⃣ “I'm outta ammo”  
2️⃣ “I have hatchet”

**How to claim on the website:**  
- Go to the **website**  
- Open the **3x store**  
- Go to **Raid Kits**  
- Press **“Buy”** on the 2x2 Raider kit

---

**🏗 FREE BUILDER KIT – 24H BUILDER**

- **Servers:** All STARZ servers 🌍  
- **Cooldown:** Claim every **24 hours**

**In-game Quick Chat steps:**
1️⃣ “I'm outta ammo”  
2️⃣ “I have high quality metal”

**How to claim on the website:**  
- Go to the **website**  
- Open the **3x store**  
- Go to **Build Kits**  
- Press **“Buy”** on the Free Builder kit
"""

# Max times OTIS will respond in a single ticket before escalating
MAX_SUPPORT_ASSISTANT_MESSAGES = 5


def _get_session(ticket_sessions: Dict[int, Dict[str, Any]], channel_id: int) -> Dict[str, Any]:
    """
    Return (and create if needed) the session dict for this ticket channel.

    ticket_sessions is the shared dict created in bot.py:
        ticket_sessions: Dict[int, Dict[str, Any]] = {}
    """
    session = ticket_sessions.get(channel_id)
    if session is None:
        session = {
            "assistant_count": 0,
            "history": [],
        }
        ticket_sessions[channel_id] = session
    return session


def _append_history(session: Dict[str, Any], role: str, content: str) -> None:
    """Append a message to the in-memory ticket history."""
    history: List[Dict[str, str]] = session.setdefault("history", [])
    history.append({"role": role, "content": content})


def _extract_gamertags_from_text(text: str) -> List[str]:
    """
    Try to pull gamertags out of text like:
    'in game names X and Y' or 'ign X'.
    Returns a small list of candidate names.
    """
    lt = text.lower()

    markers = [
        "in game names",
        "in-game names",
        "in game name",
        "in-game name",
        "ign:",
        "ign is",
        "ign ",
    ]

    for marker in markers:
        idx = lt.find(marker)
        if idx != -1:
            after = text[idx + len(marker):]
            # Normalize separators
            for sep in [" and ", "&", "/", "|"]:
                after = after.replace(sep, ",")
            parts = [p.strip(" .,:;") for p in after.split(",")]
            names = [p for p in parts if len(p) >= 3 and any(ch.isalnum() for ch in p)]
            # Limit to a few to keep replies readable
            return names[:3]

    return []


async def _handle_ai_limit(
    channel: discord.TextChannel,
    session: Dict[str, Any],
) -> None:
    """
    When OTIS has talked too much, summarize the ticket and ping staff.
    """
    history: List[Dict[str, str]] = session.get("history", [])
    qa_pairs: List[Tuple[str, str]] = []
    pending_q: str | None = None

    for item in history:
        role = item.get("role")
        text = (item.get("content") or "").strip()
        if not text:
            continue

        if role == "assistant" and "?" in text:
            q_raw = text.split("?", 1)[0].strip() + "?"
            if len(q_raw) > 160:
                q_raw = q_raw[:157] + "..."
            pending_q = q_raw
        elif role == "user" and pending_q:
            qa_pairs.append((pending_q, text))
            pending_q = None

    summary_lines: List[str] = ["Here’s a quick summary of the ticket so far:\n"]
    for idx, (q, a) in enumerate(qa_pairs[-5:], start=1):
        summary_lines.append(f"**{idx}. Q:** {q}\n**A:** {a}\n")

    summary_text = "\n".join(summary_lines)

    embed = discord.Embed(
        title="🔔 Ticket Needs Staff Review",
        description=summary_text,
        color=0xE74C3C,
    )
    embed.set_footer(text="OTIS has reached the message limit.")

    try:
        await channel.send(
            content="🔔 **Staff:** This ticket needs human review.",
            embed=embed,
        )
    except Exception as e:
        print(f"[TICKET-AI] Summary send error: {e}")

    session["assistant_count"] = MAX_SUPPORT_ASSISTANT_MESSAGES
def _next_weekly_wipe_ts(target_weekday: int, hour_utc: int, minute_utc: int) -> int:
    """
    Return the next wipe time as a Unix timestamp.

    target_weekday: 0=Monday, 3=Thursday, 4=Friday, etc.
    hour_utc/minute_utc: wipe time in UTC.
    """
    now = datetime.now(timezone.utc)

    # Start from "today at wipe time"
    candidate = now.replace(
        hour=hour_utc, minute=minute_utc, second=0, microsecond=0
    )

    # How many days ahead is the target weekday?
    days_ahead = (target_weekday - now.weekday()) % 7

    # If it's the same weekday but we've already passed the time, jump a week
    if days_ahead == 0 and candidate <= now:
        days_ahead = 7

    target_dt = candidate + timedelta(days=days_ahead)
    return int(target_dt.timestamp())


def _build_staff_summary(session: Dict[str, Any]) -> str:
    """
    Build a short Q/A-style summary of the ticket so far,
    based on the in-memory history.
    """
    history: List[Dict[str, str]] = session.get("history", [])
    qa_pairs: List[Tuple[str, str]] = []
    pending_q: str | None = None

    for item in history:
        role = item.get("role")
        text = (item.get("content") or "").strip()
        if not text:
            continue

        if role == "assistant" and "?" in text:
            # Treat assistant questions as Q
            q_raw = text.split("?", 1)[0].strip() + "?"
            if len(q_raw) > 160:
                q_raw = q_raw[:157] + "..."
            pending_q = q_raw
        elif role == "user" and pending_q:
            # User reply becomes A
            qa_pairs.append((pending_q, text))
            pending_q = None

    # If we have proper Q/A pairs, format them
    if qa_pairs:
        lines: List[str] = ["Here’s a quick summary of the ticket so far:\n"]
        for idx, (q, a) in enumerate(qa_pairs[-5:], start=1):
            lines.append(f"**{idx}. Q:** {q}\n**A:** {a}\n")
        return "\n".join(lines).strip()

    # Fallback: just list recent user messages if no Q/A structure was found
    user_msgs = [
        m.get("content", "").strip()
        for m in history
        if m.get("role") == "user" and m.get("content", "").strip()
    ]

    if not user_msgs:
        return "No previous conversation history recorded in this ticket."

    lines = ["Player messages so far:\n"]
    for idx, msg in enumerate(user_msgs[-8:], start=1):
        # Trim long walls of text
        if len(msg) > 300:
            msg = msg[:297] + "..."
        lines.append(f"**{idx}.** {msg}")
    return "\n".join(lines).strip()


async def maybe_handle_ticket_ai_message(
    bot,
    client_ai,
    message: discord.Message,
    style_text: str,
    rules_text: str,
    zorp_guide_text: str,
    raffle_text: str,
    ticket_sessions: Dict[int, Dict[str, Any]],
    ticket_category_ids,
    ai_control_roles,
) -> None:
    """
    Main OTIS brain for tickets.

    Signature matches the call in bot.py exactly.
    Uses:
      - client_ai        (OpenAI client from bot.py)
      - style_text       (loaded in bot.py)
      - rules_text       (loaded in bot.py)
      - zorp_guide_text  (loaded in bot.py)
      - raffle_text      (loaded in bot.py)
      - ticket_sessions  (shared per-channel state)
    """

    channel = message.channel
    if not isinstance(channel, discord.TextChannel):
        return

    # Bot.py has already checked ticket-ness, but we keep a light guard on category
    category = channel.category
    if category and ticket_category_ids and category.id not in ticket_category_ids:
        return

    content = (message.content or "").strip()
    author = message.author

    # If there's literally no text (e.g. only an image), we can't do anything useful
    if not content:
        return False

    # Get / create the session for this ticket
    session = _get_session(ticket_sessions, channel.id)

    # If AI is already disabled for this ticket (by staff or a previous rule), do nothing
    if session.get("ai_disabled"):
        return

    # Capture previous history state for "first message" checks
    prev_history: List[Dict[str, str]] = session.get("history", [])
    was_empty_history = len(prev_history) == 0

    # Log every user message we process so staff summaries have context
    _append_history(session, "user", content)

    # Re-read history and assistant_count after logging this message
    history: List[Dict[str, str]] = session.get("history", [])
    assistant_count: int = int(session.get("assistant_count", 0))
    lower_content = content.lower()

    # ---------------- STAFF TAKES OVER → DISABLE OTIS IN THIS TICKET ----------------
    # If a staff/support member (any role in ai_control_roles) talks in the ticket,
    # permanently disable OTIS for this ticket.
    if isinstance(author, discord.Member):
        if any(role.id in ai_control_roles for role in author.roles):
            session["ai_disabled"] = True
            return
    # -------------------------------------------------------------------------------

    # ---------------- PLAYER REQUESTS REAL STAFF ----------------
    staff_request_keywords = (
        "real staff",
        "real stuff",
        "real person",
        "real mod",
        "real admin",
        "need staff",
        "need admin",
        "talk to a real",
        "get an admin",
        "can you get an admin",
        "can u get an admin",
        "can u bring staff",
        "can you bring staff",
        "admin here",
        "staff here",
    )

    if any(k in lower_content for k in staff_request_keywords):
        staff_mention = " ".join(f'<@&{rid}>' for rid in ai_control_roles) or "@here"

        summary_text = _build_staff_summary(session)

        embed = discord.Embed(
            title="👤 Real Staff Requested",
            description=summary_text,
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Note for Staff",
            value=f"{author.mention} has requested real assistance. OTIS has now stopped responding in this ticket.",
            inline=False,
        )
        embed.set_footer(text="OTIS ‖ AI ADMIN")

        try:
            await channel.send(content=staff_mention, embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send real-staff handoff: {e}")

        session['ai_disabled'] = True
        return True

    # ----------------------------------------------------------------------

    # ---------------- UNLINK ACCOUNT → STAFF ONLY ----------------
    unlink_keywords = (
        "unlink",
        "un link",
        "un-link",
        "delink",
        "de link",
        "remove my link",
        "remove the link",
        "unlink my account",
        "unlink my kaos",
        "unlink my discord",
    )

    if any(k in lower_content for k in unlink_keywords):
        staff_mention = " ".join(f"<@&{rid}>" for rid in ai_control_roles) or "@here"
        summary_text = _build_staff_summary(session)

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • Unlink Request",
            description=summary_text,
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Note for staff",
            value=(
                "Unlinking this account has to be done by an **admin**. "
                "You may need to confirm the player’s **IGN** and **server** before completing the unlink."
            ),
            inline=False,
        )
        embed.set_footer(text="Unlinks are handled manually by STARZ staff.")

        try:
            await channel.send(content=staff_mention, embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send unlink handoff: {e}")

        session["ai_disabled"] = True
        return
    # ----------------------------------------------------------------------

    # ---------------- GIVEAWAY TICKET → HAND OFF TO STAFF ----------------
    # Only trigger this on the *first* user message in the ticket so we don't
    # hijack unrelated tickets later in the conversation.
    is_first_user_message = (assistant_count == 0 and was_empty_history)

    if is_first_user_message:
        giveaway_keywords = (
            "giveaway",
            "give away",
            "gw",   # some people shorten it
            "gaw",
        )

        if any(k in lower_content for k in giveaway_keywords):
            # Build a staff mention string from ai_control_roles
            staff_mention = " ".join(f"<@&{rid}>" for rid in ai_control_roles) or "@here"

            embed = discord.Embed(
                title="🎉 Giveaway Ticket – Staff Needed",
                description=(
                    f"{author.mention} opened this ticket about a **giveaway win**.\n\n"
                    "I can’t see which giveaway or what the exact reward is, so a staff "
                    "member needs to check giveaway logs and apply the correct prize."
                ),
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="Next steps for staff",
                value=(
                    "• Verify the player’s giveaway win in the appropriate logs/channels.\n"
                    "• Apply the correct reward in-game or via Discord.\n"
                    "• Let the player know once everything is done."
                ),
                inline=False,
            )
            embed.set_footer(text="OTIS ‖ AI ADMIN")

            try:
                await channel.send(content=staff_mention, embed=embed)
            except Exception as e:
                print(f"[TICKET-AI] Failed to send giveaway handoff: {e}")

            # From now on, OTIS should *not* auto-answer in this ticket.
            session["ai_disabled"] = True
            return
    # ----------------------------------------------------------------------

    # ================================
    # BAN STATUS / UNBAN QUESTIONS
    # ================================
    if any(k in lower_content for k in ("ban ", "banned", "unban", "unbanned", "ban appeal", "unban appeal")):
        # Try to pull explicit gamertags from the message
        gamertags = _extract_gamertags_from_text(content)

        # Fallback: use their Discord display name if nothing found
        if not gamertags and isinstance(author, discord.Member):
            gamertags = [author.display_name]

        # Dedupe + safety limit
        if gamertags:
            seen = set()
            clean_tags: List[str] = []
            for gt in gamertags:
                if gt not in seen:
                    seen.add(gt)
                    clean_tags.append(gt)
            gamertags = clean_tags[:3]

        if gamertags:
            lines: List[str] = []

            for gt in gamertags:
                active_row, total_bans = lookup_ban_status_by_gamertag(gt)

                if active_row is None:
                    # No active ban
                    if total_bans == 0:
                        lines.append(f"**`{gt}`** – No bans found on record.")
                    else:
                        next_tier, next_duration = describe_next_offense(total_bans)
                        lines.append(
                            f"**`{gt}`** – Not currently banned.\n"
                            f"• Past offenses: **{total_bans}**\n"
                            f"• Next offense: Tier {next_tier} – {next_duration}"
                        )
                    continue

                # Active ban details
                reason = active_row["reason"] or "No reason recorded"
                offense_tier = int(active_row["offense_tier"])
                banned_ts = active_row["banned_at"]
                expires_ts = active_row["expires_at"]

                banned_at_str = datetime.utcfromtimestamp(banned_ts).strftime("%Y-%m-%d %H:%M UTC")

                if expires_ts is None:
                    unban_str = "Permanent ban (no auto-unban)"
                else:
                    unban_str = datetime.utcfromtimestamp(expires_ts).strftime("%Y-%m-%d %H:%M UTC")

                next_tier, next_duration = describe_next_offense(total_bans)

                lines.append(
                    f"**`{gt}`** – Active ban (Tier {offense_tier})\n"
                    f"• Reason: {reason}\n"
                    f"• Time banned: `{banned_at_str}`\n"
                    f"• Time unbanned: `{unban_str}`\n"
                    f"• Offense count: **{total_bans}** total bans on record\n"
                    f"• Next offense: Tier {next_tier} – {next_duration}"
                )

            if lines:
                embed = discord.Embed(
                    title="OTIS ‖ AI ADMIN • Ban Status",
                    description="\n\n".join(lines),
                    color=0xE74C3C,
                )
                embed.set_footer(text="All bans are managed automatically by OTIS and STARZ staff.")

                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    print(f"[TICKET-AI] Failed to send ban status reply: {e}")

                # We handled this message fully – no OpenAI needed
                return True
    # ----------------------------------------------------------------------

    # ---------- ZORP SETUP SHORTCUT (SHORT VERSION) ----------
    if (
        ("zorp" in lower_content or "orp" in lower_content)
        and "how" in lower_content
        and any(word in lower_content for word in ("set", "activate", "turn on"))
    ):
        description = (
            "To set your **ZORP**:\n"
            "- Make sure you are in a **team** and you are the **team leader**.\n"
            "- Open Quick Chat and use: `Can I build around here?`\n"
            "- Then select **Yes**.\n\n"
            "If done correctly, your bubble will turn **GREEN** and your base will be protected while you're offline."
        )

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • ZORP SETUP",
            description=description,
            color=0xE74C3C,
        )
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send ZORP setup embed: {e}")
        return True
    # ----------------------------------------------------------------------

    # ==============================
    # ZEN / XIM CONTROLLER REPORTS
    # ==============================
    if any(k in lower_content for k in ("xim", "zen", "zim")):
        reply = (
            "Using Zen/XIM-type controllers is not against STARZ rules. "
            "If you still want to report a Zen/XIM player, it must be done through **D11**."
        )

        embed = discord.Embed(
            description=reply,
            color=0xE74C3C,
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send Zen/XIM reply: {e}")

        return True
    # ----------------------------------------------------------------------

    # ================================
    # COMPOUND / CHINA WALL LIMIT
    # ================================
    if "compound" in lower_content or "compounds" in lower_content or "china wall" in lower_content:
        reply = (
            "On STARZ, a compound and a China wall count as the same thing. "
            "You can have a maximum of **2** total per base/team."
        )

        embed = discord.Embed(
            description=reply,
            color=0xE74C3C,
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send compound limit reply: {e}")

        return True
    # ----------------------------------------------------------------------

    # ================================
    # WIPE SCHEDULE SHORTCUT
    # ================================
    if "wipe" in lower_content and (
        "when" in lower_content
        or "time" in lower_content
        or "what time" in lower_content
    ):
        # 🔧 SET YOUR UTC TIMES HERE
        # Example: NA wipes Thursday 21:00 UTC, EU Thursday 19:00 UTC, OCE Friday 08:00 UTC
        na_ts = _next_weekly_wipe_ts(target_weekday=3, hour_utc=21, minute_utc=0)  # Thursday
        eu_ts = _next_weekly_wipe_ts(target_weekday=3, hour_utc=19, minute_utc=0)  # Thursday
        oce_ts = _next_weekly_wipe_ts(target_weekday=4, hour_utc=8, minute_utc=0)  # Friday

        desc = f"""
__**STARZ WIPE SCHEDULE**__

**🇺🇸 U.S SERVERS (Weekly Thursday wipes)**

**🇺🇸 STARZ S1 QUADS (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `0bfebfa`

**🇺🇸 STARZ S2 DUO (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `6a942d7`

**🇺🇸 STARZ S3 TRIO (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `eb8b488`

**🇺🇸 STARZ S6 6 MAN (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `0b7ff4e`

**🇺🇸 STARZ S8 2X MAIN (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `7a5292c`

**🇺🇸 STARZ S9 NO TEAM LIMIT (NA)** – Next wipe: <t:{na_ts}:F> (**<t:{na_ts}:R>**)  
Server ID: `5a238c5`


**🇪🇺 EU SERVERS (Weekly Thursday wipes)**

**🇪🇺 STARZ S4 QUADS (EU)** – Next wipe: <t:{eu_ts}:F> (**<t:{eu_ts}:R>**)  
Server ID: `f4db6cb`

**🇪🇺 STARZ S5 DUO (EU)** – Next wipe: <t:{eu_ts}:F> (**<t:{eu_ts}:R>**)  
Server ID: `0ba8d47`


**🇦🇺 OCE SERVER (Weekly Friday wipe)**

**🇦🇺 STARZ S10 QUADS (OCE)** – Next wipe: <t:{oce_ts}:F> (**<t:{oce_ts}:R>**)  
Server ID: `6c67165`

_⏰ Times show in **your local timezone** in Discord._
""".strip()

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • Wipe Schedule",
            description=desc,
            color=0xE74C3C,
        )
        embed.set_footer(text="Wipes are weekly. Countdown updates automatically.")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send wipe schedule: {e}")
        return True

    # ----------------------------------------------------------------------

    # ==========================
    # INSIDING SHORTCUT (Short Version)
    # ==========================
    if any(
        phrase in lower_content
        for phrase in (
            "i got insider",
            "i got insided",
            "we got insider",
            "we got insided",
            "got insider",
            "got insided",
            "my teammate insided",
            "they insided us",
            "insiding my base",
            "insiding is against the rules",
        )
    ):
        description = (
            "**Insiding is not against the rules on STARZ.** "
            "We recommend choosing trustworthy teammates and being careful with who you give access to. "
            "If you believe something *other* than insiding happened, tell us and staff can review it."
        )

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • Insiding Info",
            description=description,
            color=0xE74C3C,
        )

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send insiding info embed: {e}")

        return True
    # ----------------------------------------------------------------------

    # ==========================
    # RAFFLE / ROLL TICKET SHORTCUT (Short Version)
    # ==========================
    if any(
        phrase in lower_content
        for phrase in (
            "raffle",
            "roll ticket",
            "ticket for roll",
            "get a roll",
            "get a ticket",
            "raffle ticket",
            "how do i get a roll",
            "how do i get tickets",
            "how to get roll",
            "how to get raffle",
            "get roll ticket",
            "/roll",
            "roll",
        )
    ):
        description = (
            "**Raffle tickets are earned automatically on the STARZ webstore — every $5 spent gives you 1 raffle ticket.** "
            "Tickets can also drop from airdrops or be given out during events and giveaways. "
            "You use raffle tickets to enter rolls for prizes, and having more tickets increases your chances."
        )

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • Raffle Tickets",
            description=description,
            color=0xE74C3C,
        )

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send raffle ticket embed: {e}")

        return True
    # ----------------------------------------------------------------------

    # ==========================
    # FREE KITS SHORTCUT
    # ==========================
    free_kit_keywords = (
        "free kit",
        "free kits",
        "hourly kit",
        "hourly kits",
        "free builder",
        "mp5 roadsign",
        "2x2 raider",
    )

    if any(k in lower_content for k in free_kit_keywords):
        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • FREE KITS",
            description=FREE_KITS_INFO.strip(),
            color=0xE74C3C,
        )
        embed.set_footer(text="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send FREE KITS info: {e}")

        return True
    # ----------------------------------------------------------------------

    # ==========================
    # SCRAP FAQ SHORTCUT
    # ==========================
    if "scrap" in lower_content and any(
        phrase in lower_content
        for phrase in (
            "how do i get",
            "how to get",
            "how do i use",
            "how to use",
            "what is",
            "what does it do",
            "how does scrap work",
        )
    ):
        embed = discord.Embed(
            title="💰 STARZ SCRAP GUIDE",
            description=SCRAP_INFO.strip(),
            color=0xE74C3C,  # Red
        )
        embed.set_footer(text="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send SCRAP embed: {e}")

        return True
    # ----------------------------------------------------------------------

    # ---------------- VIP PAYMENT / MONTHLY SHORTCUT ----------------
    if "vip" in lower_content and any(
        phrase in lower_content
        for phrase in (
            "pay monthly",
            "monthly vip",
            "every month",
            "per month",
            "do i have to pay",
            "have to pay monthly",
            "is vip monthly",
            "subscription",
        )
    ):
        member = message.author
        vip_roles_never_pay = {"top supporter", "🤑mega supporter🤑"}
        vip_included_roles = {"knight", "warden", "reaper"}

        has_never_pay = False
        has_vip_included = False
        has_regular_vip = False

        if isinstance(member, discord.Member):
            role_names = {r.name.lower() for r in member.roles}
            if any(name in role_names for name in vip_roles_never_pay):
                has_never_pay = True
            if any(name in role_names for name in vip_included_roles):
                has_vip_included = True
            if "vip" in role_names:
                has_regular_vip = True

        lines: List[str] = []
        lines.append("Here’s how **VIP payment** works on STARZ:\n")

        lines.append(
            "**🔒 You NEVER have to pay monthly if you have:**\n"
            "- **TOP SUPPORTER**\n"
            "- **🤑MEGA SUPPORTER🤑**\n"
            "These ranks include **permanent VIP** — no subscription required.\n"
        )

        lines.append(
            "**🛡️ These ranks already include VIP:**\n"
            "- **KNIGHT**\n"
            "- **WARDEN**\n"
            "- **REAPER**\n"
            "If you have one of these, you don’t pay separately for VIP.\n"
        )

        lines.append(
            "**🟨 Regular `vip` role:**\n"
            "You *may* need a monthly subscription depending on **when you originally purchased VIP** "
            "(older VIP purchases were lifetime; newer ones are subscription-based).\n"
        )

        if isinstance(member, discord.Member):
            hints: List[str] = []
            if has_never_pay:
                hints.append(
                    "Based on your roles, you **do not** need to pay monthly for VIP."
                )
            elif has_vip_included:
                hints.append(
                    "Based on your roles, VIP is **already included** in your rank."
                )
            elif has_regular_vip:
                hints.append(
                    "You have the regular `vip` role — whether it’s lifetime or subscription depends on **when you bought it**. "
                    "If you’re not sure, staff can check your purchase history."
                )

            if hints:
                lines.append("\n".join(hints))

        embed = discord.Embed(
            title="OTIS ‖ AI ADMIN • VIP Payment",
            description="\n\n".join(lines),
            color=0xE74C3C,
        )
        embed.set_footer(
            text="If you still aren't sure, ask staff to check your purchase date."
        )

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send VIP payment embed: {e}")

        return True
    # ----------------------------------------------------------------------

    # ---------------- VIP SHORTCUT ----------------
    if "vip" in lower_content and any(
        phrase in lower_content
        for phrase in (
            "how do i get",
            "how to get",
            "where do i get",
            "how can i get",
            "how do i buy",
            "how to buy",
        )
    ):
        vip_text = (
            "**VIP** on STARZ = **queue skip + VIP kit**.\n\n"
            "You can purchase VIP on our website here:\n"
            "<https://starzempire.tip4serv.com/category/vip>"
        )

        embed = discord.Embed(
            description=vip_text,
            color=0xE74C3C,  # STARZ red
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send VIP embed: {e}")
        return
    # ----------------------------------------------------------------------

    # ---------------- Build system prompt ----------------
    # This pulls in style/rules/ZORP/raffle text loaded in bot.py
    system_parts: List[str] = [
        "You are **OTIS** — STARZ AI ADMIN for the STARZ Rust Console Server Network.",
        "",
        "GENERAL BEHAVIOR:",
        "- You are concise, helpful, and respectful.",
        "- You answer players in tickets inside Discord.",
        "- You NEVER invent kit Quickchat claim commands.",
        "- Kit claim commands are handled by a separate kit helper system.",
        "- If a player asks how to claim kits and they have not received "
        "instructions yet, ask which kit they mean and tell them kit instructions "
        "will be provided separately.",
        "",
        "STYLE / TONE:",
        style_text or "(no extra style text provided).",
        "",
        "SERVER RULES SUMMARY:",
        rules_text or "(no rules text provided).",
        "",
        "ZORP / OFFLINE RAID PROTECTION SUMMARY:",
        zorp_guide_text or "(no ZORP guide provided).",
        "",
        "RAFFLES / GIVEAWAYS / STORE INFO:",
        raffle_text or "(no raffle/store text provided).",
        "",
        "IMPORTANT: Keep your replies short and direct — ideally 2–3 sentences max.",
        "Avoid long paragraphs; give clear, actionable answers.",
    ]
    system_prompt = "\n".join(system_parts)

    messages_payload: List[Dict[str, str]] = []
    messages_payload.append({"role": "system", "content": system_prompt})

    # Add recent history for context
    for item in history[-12:]:
        role = item.get("role") or "user"
        text = item.get("content") or ""
        if not text:
            continue
        messages_payload.append({"role": role, "content": text})

    # Latest user message
    messages_payload.append({"role": "user", "content": content})

    # ---------------- Call OpenAI ----------------
    try:
        completion = client_ai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages_payload,
            max_tokens=300,
        )
        reply_text = completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"[TICKET-AI] OpenAI error: {e}")
        return

    if not reply_text:
        return

    # ---------- Hard sentence limiter (max 3 sentences) ----------
    import re
    sentences = re.split(r'(?<=[.!?])\s+', reply_text.strip())
    reply_text = " ".join(sentences[:3])  # Limit OTIS to 3 sentences max


    # ---------------- Send OTIS reply as embed ----------------
    embed = discord.Embed(
        description=reply_text,
        color=0x3498DB,
    )
    embed.set_author(name="OTIS ‖ AI ADMIN")

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[TICKET-AI] Failed to send AI reply: {e}")
        return

    # ---------------- Track in history/session ----------------
    # User message is already logged at the top
    _append_history(session, "assistant", reply_text)
    session["assistant_count"] = assistant_count + 1

    # If we've hit the assistant limit, summarize + ping staff and disable AI.
    if session["assistant_count"] >= MAX_SUPPORT_ASSISTANT_MESSAGES:
        await _handle_ai_limit(channel, session)
        session["ai_disabled"] = True
