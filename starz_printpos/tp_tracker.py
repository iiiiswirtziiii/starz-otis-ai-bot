# starz_printpos/tp_tracker.py
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple, Set

from discord.ext import tasks

from .tp_config import (
    PRINTPOS_BATCH_SIZE,
    PRINTPOS_TICK_INTERVAL,
    PER_COMMAND_DELAY,
    PRINTPOS_ENABLED_DEFAULT,
    FAR_DISTANCE_METERS,
    FAR_SKIP_SECONDS,
    PRINTPOS_STATUS_LOG_SECONDS,
    PRINTPOS_VERBOSE_LOGS,
)
from .tp_zones import check_zones_for_player, get_all_zones


SendRconFunc = Callable[[str, str], Awaitable[str | None]]

PRINTPOS_COORD_RE = re.compile(
    r"\((?P<x>-?\d+\.\d+),\s*(?P<y>-?\d+\.\d+),\s*(?P<z>-?\d+\.\d+)\)"
)

_send_rcon: SendRconFunc | None = None
_enabled: bool = PRINTPOS_ENABLED_DEFAULT

# READY rotation
_poll_queues: Dict[str, Deque[str]] = defaultdict(deque)
_ready_set: Dict[str, Set[str]] = defaultdict(set)

# Cooldowns
_cooldown_until: Dict[Tuple[str, str], float] = {}

# EXPIRED fast lane
_expired_queues: Dict[str, Deque[str]] = defaultdict(deque)
_expired_set: Dict[str, Set[str]] = defaultdict(set)

# ============================
# SCAN lane (startup / unknown)
# ============================
_scan_queues: Dict[str, Deque[str]] = defaultdict(deque)
_scan_set: Dict[str, Set[str]] = defaultdict(set)

# Players confirmed "near" at least once
_near_set: Dict[str, Set[str]] = defaultdict(set)


# Empty-server cooldown
EMPTY_SERVER_COOLDOWN_SECONDS = 300.0
_empty_server_until: Dict[str, float] = {}

# Console-stream support (kept for compatibility)
_pending_positions: Dict[str, Deque[str]] = defaultdict(deque)

# Stats
_stats: Dict[str, Dict[str, float | int]] = defaultdict(
    lambda: {
        "last_log_ts": 0.0,
        "sent": 0,
        "coords": 0,
        "no_coords": 0,
        "far": 0,
        "tp": 0,
        "err": 0,
    }
)


# -------------------------
# Init / enable
# -------------------------

def init_printpos_system(send_rcon: SendRconFunc) -> None:
    global _send_rcon, _enabled
    _send_rcon = send_rcon
    _enabled = PRINTPOS_ENABLED_DEFAULT
    print(
        f"[STARZ-PRINTPOS] Initialized. Enabled={_enabled}, "
        f"batch={PRINTPOS_BATCH_SIZE}, tick={PRINTPOS_TICK_INTERVAL}s, delay={PER_COMMAND_DELAY}s"
    )


def set_enabled(flag: bool) -> None:
    global _enabled
    _enabled = flag
    print(f"[STARZ-PRINTPOS] System {'ENABLED' if flag else 'DISABLED'}.")


def is_enabled() -> bool:
    return _enabled


# -------------------------
# Helpers
# -------------------------

def _min_dist2_to_any_zone(x: float, y: float, z: float) -> Optional[float]:
    zones = list(get_all_zones())
    if not zones:
        return None
    best: Optional[float] = None
    for zone in zones:
        dx = x - zone.zone_x
        dy = y - zone.zone_y
        dz = z - zone.zone_z
        d2 = (dx * dx) + (dy * dy) + (dz * dz)
        if best is None or d2 < best:
            best = d2
    return best


def _wake_expired_for_server(server_key: str, now_ts: float) -> None:
    expq = _expired_queues[server_key]

    for (sk, pname), until in list(_cooldown_until.items()):
        if sk != server_key:
            continue
        if now_ts >= float(until):
            _cooldown_until.pop((sk, pname), None)
            if pname not in _expired_set[server_key] and pname not in _ready_set[server_key]:
                expq.append(pname)
                _expired_set[server_key].add(pname)

def _pick_players(server_key: str) -> List[str]:
    picked: List[str] = []

    expired_q = _expired_queues[server_key]
    ready_q   = _poll_queues[server_key]
    scan_q    = _scan_queues[server_key]

    # 1 from expired (fast lane)
    if expired_q and len(picked) < PRINTPOS_BATCH_SIZE:
        p = expired_q.popleft()
        _expired_set[server_key].discard(p)
        picked.append(p)

    # up to 2 from ready (near confirmed)
    for _ in range(2):
        if len(picked) >= PRINTPOS_BATCH_SIZE or not ready_q:
            break
        p = ready_q.popleft()
        _ready_set[server_key].discard(p)
        picked.append(p)

    # 1 from scan (slow classification)
    if len(picked) < PRINTPOS_BATCH_SIZE and scan_q:
        p = scan_q.popleft()
        _scan_set[server_key].discard(p)
        picked.append(p)

    # if still room, fill from ready
    while len(picked) < PRINTPOS_BATCH_SIZE and ready_q:
        p = ready_q.popleft()
        _ready_set[server_key].discard(p)
        picked.append(p)

    return picked





def _log_status_if_due(server_key: str, working: bool) -> None:
    st = _stats[server_key]
    now_ts = time.time()
    if now_ts - float(st["last_log_ts"]) < PRINTPOS_STATUS_LOG_SECONDS:
        return

    st["last_log_ts"] = now_ts

    ready = len(_poll_queues[server_key])
    expired = len(_expired_queues[server_key])
    cooldown = sum(
        1 for (sk, _), until in _cooldown_until.items()
        if sk == server_key and now_ts < until
    )

    status = "✅ working" if working else "⚠️ no coords"
    print(
        f"[PRINTPOS] {server_key} {status} | "
        f"ready={ready} expired={expired} cooldown={cooldown} | "
        f"sent={st['sent']} coords={st['coords']} noc={st['no_coords']} "
        f"far={st['far']} tp={st['tp']} err={st['err']}"
    )


# -------------------------
# Playerlist updates
# -------------------------

async def process_printpos_response(server_key: str, player_name: str, resp_text: str) -> None:
    if not _enabled or _send_rcon is None:
        return

    st = _stats[server_key]
    m = PRINTPOS_COORD_RE.search(resp_text or "")
    if not m:
        st["no_coords"] += 1
        _log_status_if_due(server_key, False)
        return

    x = float(m.group("x"))
    y = float(m.group("y"))
    z = float(m.group("z"))
    st["coords"] += 1

    if PRINTPOS_VERBOSE_LOGS:
        print(f"[STARZ-PRINTPOS] POS {server_key}/{player_name} = ({x:.2f},{y:.2f},{z:.2f})")

    d2 = _min_dist2_to_any_zone(x, y, z)
    if d2 is not None and d2 > FAR_DISTANCE_METERS ** 2:
        _cooldown_until[(server_key, player_name)] = time.time() + FAR_SKIP_SECONDS
        st["far"] += 1
        return

    cmds = check_zones_for_player(server_key, player_name, x, y, z)
    if not cmds:
        return

    st["tp"] += 1
    for cmd in cmds:
        await _send_rcon(server_key, cmd)
        await asyncio.sleep(PER_COMMAND_DELAY)





async def handle_printpos_console_line(server_key: str, msg_text: str) -> None:
    if not _enabled:
        return

    m = PRINTPOS_COORD_RE.search(msg_text or "")
    if not m:
        return

    q = _pending_positions.get(server_key)
    if not q:
        return

    pname = q.popleft()
    await process_printpos_response(server_key, pname, msg_text)



# -------------------------
# Printpos handling
# -------------------------
async def process_printpos_response(server_key: str, player_name: str, resp_text: str) -> None:
    if not _enabled or _send_rcon is None:
        return

    st = _stats[server_key]
    m = PRINTPOS_COORD_RE.search(resp_text or "")
    if not m:
        st["no_coords"] += 1
        _log_status_if_due(server_key, False)
        return

    x = float(m.group("x"))
    y = float(m.group("y"))
    z = float(m.group("z"))
    st["coords"] += 1

    if PRINTPOS_VERBOSE_LOGS:
        print(f"[STARZ-PRINTPOS] POS {server_key}/{player_name} = ({x:.2f},{y:.2f},{z:.2f})")

    # ---- NEAR / FAR classification ----
    d2 = _min_dist2_to_any_zone(x, y, z)

    # If zones exist and player is FAR from all zone centers -> cooldown + not-near
    if d2 is not None and d2 > (FAR_DISTANCE_METERS ** 2):
        _cooldown_until[(server_key, player_name)] = time.time() + FAR_SKIP_SECONDS
        st["far"] += 1

        # mark as not near (so next playerlist rebuild keeps them in SCAN, not READY)
        _near_set[server_key].discard(player_name)
        return

    # If we get here: they are near enough (or no zones configured yet)
    _near_set[server_key].add(player_name)

    # ---- TP trigger check ----
    cmds = check_zones_for_player(server_key, player_name, x, y, z)
    if not cmds:
        return

    st["tp"] += 1
    for cmd in cmds:
        await _send_rcon(server_key, cmd)
        await asyncio.sleep(PER_COMMAND_DELAY)



# -------------------------
# Poll loop
# -------------------------

@tasks.loop(seconds=PRINTPOS_TICK_INTERVAL)
async def _position_poll_loop() -> None:
    if not _enabled or _send_rcon is None:
        return

    now_ts = time.time()

    for server_key in list(_poll_queues.keys()):
        if _empty_server_until.get(server_key, 0.0) > now_ts:
            continue

        _wake_expired_for_server(server_key, now_ts)
        picked = _pick_players(server_key)
        if not picked:
            continue

        for pname in picked:
            try:
                resp = await _send_rcon(server_key, f'server.printpos "{pname}"')
                _stats[server_key]["sent"] += 1

                if resp:
                    await process_printpos_response(server_key, pname, resp)

                # Re-queue logic:
                # - NEAR players go back to READY
                # - NOT-NEAR players go to SCAN
                if (server_key, pname) not in _cooldown_until:
                    if pname in _near_set[server_key]:
                        if pname not in _ready_set[server_key] and pname not in _expired_set[server_key]:
                            _poll_queues[server_key].append(pname)
                            _ready_set[server_key].add(pname)
                    else:
                        if pname not in _scan_set[server_key]:
                            _scan_queues[server_key].append(pname)
                            _scan_set[server_key].add(pname)

                await asyncio.sleep(PER_COMMAND_DELAY)

            except Exception:
                _stats[server_key]["err"] += 1

        _log_status_if_due(server_key, True)







