from __future__ import annotations

from typing import Dict, Any, List, Tuple

import discord


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
    if not content:
        return
            lower_content = content.lower()
    
    # ---------- SCRAP FAQ SHORTCUT ----------
    if "scrap" in lower_content and any(
        phrase in lower_content
        for phrase in (
            "how do i get",
            "how to get",
            "how do i use",
            "how to use",
            "what is",
            "what does it do",
        )
    ):
        # Use raffle_text (where you put your scrap rules)
        scrap_text = raffle_text or (
            "**SCRAP** is our Discord currency used for shop purchases and rewards.\n\n"
            "If this message shows, ask SWIRTZ to update the RAFFLE_TEXT env var "
            "with the full scrap rules."
        )

        embed = discord.Embed(
            description=scrap_text,
            color=0xE74C3C,  # STARZ red
        )
        embed.set_author(name="OTIS ‖ AI ADMIN")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[TICKET-AI] Failed to send scrap FAQ embed: {e}")
        return True



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


    # Prepare / get session for this ticket
    session = _get_session(ticket_sessions, channel.id)
    assistant_count: int = int(session.get("assistant_count", 0))

    # If we've already talked too much: summarize + ping staff
    if assistant_count >= MAX_SUPPORT_ASSISTANT_MESSAGES:
        await _handle_ai_limit(channel, session)
        return

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
    ]
    system_prompt = "\n".join(system_parts)

    messages_payload: List[Dict[str, str]] = []
    messages_payload.append({"role": "system", "content": system_prompt})

    # Add recent history for context
    history = session.get("history", [])
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
            max_tokens=450,
        )
        reply_text = completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"[TICKET-AI] OpenAI error: {e}")
        return

    if not reply_text:
        return

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
    _append_history(session, "user", content)
    _append_history(session, "assistant", reply_text)
    session["assistant_count"] = assistant_count + 1


