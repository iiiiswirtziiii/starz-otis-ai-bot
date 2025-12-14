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
    """
    Fair lane scheduler:
    - Prefer players coming off cooldown (EXPIRED) so they get rechecked quickly.
    - Split picks per tick:
        2 from expired, then fill remaining from ready (for batch size 4).
    """
    picked: List[str] = []

    ready_q = _poll_queues[server_key]
    expired_q = _expired_queues[server_key]

    # how many expired to take this tick (tune here)
    take_expired = min(2, PRINTPOS_BATCH_SIZE)

    # Take from expired lane first
    for _ in range(take_expired):
        if not expired_q or len(picked) >= PRINTPOS_BATCH_SIZE:
            break
        p = expired_q.popleft()
        _expired_set[server_key].discard(p)
        picked.append(p)

    # Fill remaining from ready lane
    while ready_q and len(picked) < PRINTPOS_BATCH_SIZE:
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

def update_connected_players(server_key: str, players: list) -> None:
    names: List[str] = []
    for p in players or []:
        if isinstance(p, dict):
            n = p.get("DisplayName")
            if n:
                names.append(str(n).strip())
        elif isinstance(p, str):
            names.append(p.strip())

    names = list(dict.fromkeys(n for n in names if n))
    now_ts = time.time()

    # Empty server → sleep
    if not names:
        _empty_server_until[server_key] = now_ts + EMPTY_SERVER_COOLDOWN_SECONDS
        _poll_queues[server_key].clear()
        _ready_set[server_key].clear()
        _expired_queues[server_key].clear()
        _expired_set[server_key].clear()
        return
    else:
        _empty_server_until.pop(server_key, None)

    online = set(names)

    # purge offline cooldowns
    for (sk, pname) in list(_cooldown_until.keys()):
        if sk == server_key and pname not in online:
            _cooldown_until.pop((sk, pname), None)

    # rebuild READY
    q = _poll_queues[server_key]
    q.clear()
    _ready_set[server_key].clear()

for n in names:
    # IMPORTANT:
    # If they're in cooldown dict (even if "expired"), do NOT put them back into READY here.
    # _wake_expired_for_server() will move them into the EXPIRED fast lane instead.
    if (server_key, n) in _cooldown_until:
        continue

    q.append(n)
    _ready_set[server_key].add(n)


    # clean expired
    expq = _expired_queues[server_key]
    kept = deque()
    _expired_set[server_key].clear()
    for n in expq:
        if n in online and n not in _ready_set[server_key]:
            kept.append(n)
            _expired_set[server_key].add(n)
    expq.clear()
    expq.extend(kept)


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

                if (server_key, pname) not in _cooldown_until:
                    if pname not in _ready_set[server_key]:
                        _poll_queues[server_key].append(pname)
                        _ready_set[server_key].add(pname)

                await asyncio.sleep(PER_COMMAND_DELAY)

            except Exception:
                _stats[server_key]["err"] += 1

        _log_status_if_due(server_key, True)


def start_printpos_polling() -> None:
    if not _position_poll_loop.is_running():
        _position_poll_loop.start()
        print("[STARZ-PRINTPOS] Position polling loop started.")

