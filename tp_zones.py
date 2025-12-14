# starz_printpos/tp_zones.py
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Dict, List, Tuple, Set, Optional

from .tp_config import TP_ZONES_JSON_PATH, TELEPORT_COMMAND_TEMPLATE, TP_ZONE_COOLDOWN


# ============================
# ENUM: TP Types
# ============================

class TPType(str, Enum):
    LAUNCHSITE = "LAUNCHSITE"
    AIRFIELD = "AIRFIELD"
    JUNKYARD = "JUNKYARD"
    OXUMS_GAS_STATION = "OXUMS GAS STATION"
    WATER_TREATMENT_PLANT = "WATER TREATMENT PLANT"
    BANDIT_CAMP = "BANDIT CAMP"
    OUTPOST = "OUTPOST"
    FISHING_VILLAGE = "FISHING VILLAGE"
    MILLITARY_TUNNELS = "MILLITARY TUNNELS"
    ABANDONED_MILITARY_BASE = "ABANDONED MILITARY BASE"


# ============================
# DEFAULT ZONE COLORS
# ============================

DEFAULT_ZONE_COLORS: Dict[str, str] = {
    TPType.LAUNCHSITE.value: "ORANGE",
    TPType.AIRFIELD.value: "GREEN",
    TPType.JUNKYARD.value: "BROWN",
    TPType.OXUMS_GAS_STATION.value: "YELLOW",
    TPType.WATER_TREATMENT_PLANT.value: "CYAN",
    TPType.BANDIT_CAMP.value: "RED",
    TPType.OUTPOST.value: "BLUE",
    TPType.FISHING_VILLAGE.value: "TEAL",
    TPType.MILLITARY_TUNNELS.value: "PURPLE",
    TPType.ABANDONED_MILITARY_BASE.value: "GRAY",
}


# ============================
# DATA CLASS: TpZone
# ============================

@dataclass
class TpZone:
    tp_type: str
    slot: int

    # Zone center
    zone_x: float
    zone_y: float
    zone_z: float

    # Visible radius (your Rust zones plugin uses 120)
    radius: float = 120.0

    # Default destination (still kept for backward compatibility)
    dest_x: float = 0.0
    dest_y: float = 0.0
    dest_z: float = 0.0

    # Color label
    color: str = "WHITE"

    # Messages
    enter_message: Optional[str] = None
    exit_message: Optional[str] = None

    # Trigger radius for teleport checks (THIS is what matters for detection)
    trigger_radius: float = 1.5


    # List of teleport spawn points
    spawn_points: Optional[List[Tuple[float, float, float]]] = None


# ============================
# INTERNAL STORAGE
# ============================

# tp_type -> slot -> TpZone
_ZONES: Dict[str, Dict[int, TpZone]] = {}

# Cooldown tracking:
# (server_key, player_name, tp_type, slot) -> last teleport time
_last_tp_times: Dict[Tuple[str, str, str, int], float] = {}

# Tracks which zones a player is currently inside:
# (server_key, player_name) -> set of (tp_type, slot)
_last_player_zones: Dict[Tuple[str, str], Set[Tuple[str, int]]] = {}


# ============================
# LOAD / SAVE
# ============================

def _load_zones_from_disk() -> None:
    global _ZONES

    if not os.path.exists(TP_ZONES_JSON_PATH):
        _ZONES = {}
        return

    try:
        with open(TP_ZONES_JSON_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        _ZONES = {}
        return

    zones: Dict[str, Dict[int, TpZone]] = {}

    for tp_type, slots in (raw or {}).items():
        zones[tp_type] = {}

        if not isinstance(slots, dict):
            continue

        for slot_str, data in slots.items():
            try:
                slot = int(slot_str)
                if not isinstance(data, dict):
                    continue

                spawn_points = data.get("spawn_points")
                # Backward compatible: if spawn_points missing, use dest as a single point.
                if not spawn_points:
                    spawn_points = [(
                        float(data.get("dest_x", 0.0)),
                        float(data.get("dest_y", 0.0)),
                        float(data.get("dest_z", 0.0)),
                    )]

                zones[tp_type][slot] = TpZone(
                    tp_type=str(tp_type),
                    slot=slot,
                    zone_x=float(data["zone_x"]),
                    zone_y=float(data["zone_y"]),
                    zone_z=float(data["zone_z"]),
                    radius=float(data.get("radius", 120.0)),
                    dest_x=float(data.get("dest_x", 0.0)),
                    dest_y=float(data.get("dest_y", 0.0)),
                    dest_z=float(data.get("dest_z", 0.0)),
                    color=data.get("color") or DEFAULT_ZONE_COLORS.get(tp_type, "WHITE"),
                    enter_message=data.get("enter_message"),
                    exit_message=data.get("exit_message"),
                    trigger_radius=float(data.get("trigger_radius", 1.5)),
                    spawn_points=[(float(a), float(b), float(c)) for (a, b, c) in spawn_points],
                )
            except Exception:
                continue

    _ZONES = zones


def _save_zones_to_disk() -> None:
    raw: Dict[str, Dict[str, Dict]] = {}

    for tp_type, slots in _ZONES.items():
        raw[tp_type] = {}
        for slot, zone in slots.items():
            raw[tp_type][str(slot)] = {
                "zone_x": zone.zone_x,
                "zone_y": zone.zone_y,
                "zone_z": zone.zone_z,
                "radius": zone.radius,
                "dest_x": zone.dest_x,
                "dest_y": zone.dest_y,
                "dest_z": zone.dest_z,
                "color": zone.color,
                "enter_message": zone.enter_message,
                "exit_message": zone.exit_message,
                "trigger_radius": zone.trigger_radius,
                # ✅ IMPORTANT: save spawn_points too
                "spawn_points": zone.spawn_points or [(zone.dest_x, zone.dest_y, zone.dest_z)],
            }

    try:
        with open(TP_ZONES_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    except Exception as e:
        print(f"[TP-ZONES] Failed to save zones: {e}")


# Initialize on import
_load_zones_from_disk()


# ============================
# CREATE / UPDATE ZONE
# ============================

def set_tp_zone(
    tp_type: TPType,
    slot: int,
    zone_x: float,
    zone_y: float,
    zone_z: float,
    dest_x: float,
    dest_y: float,
    dest_z: float,
    color: Optional[str] = None,
    enter_message: Optional[str] = None,
    exit_message: Optional[str] = None,
    spawn_points: Optional[List[Tuple[float, float, float]]] = None,
) -> TpZone:
    global _ZONES

    if tp_type.value not in _ZONES:
        _ZONES[tp_type.value] = {}

    final_color = color or DEFAULT_ZONE_COLORS.get(tp_type.value, "WHITE")

    z = TpZone(
        tp_type=tp_type.value,
        slot=int(slot),
        zone_x=float(zone_x),
        zone_y=float(zone_y),
        zone_z=float(zone_z),
        radius=120.0,
        dest_x=float(dest_x),
        dest_y=float(dest_y),
        dest_z=float(dest_z),
        color=final_color,
        enter_message=enter_message,
        exit_message=exit_message,
        trigger_radius=1.5,
        spawn_points=spawn_points or [(float(dest_x), float(dest_y), float(dest_z))],
    )

    _ZONES[tp_type.value][int(slot)] = z
    _save_zones_to_disk()
    print(f"[TP-ZONES] Saved {tp_type.value} slot {slot}: {asdict(z)}")
    return z


def get_all_zones() -> List[TpZone]:
    out: List[TpZone] = []
    for slots in _ZONES.values():
        out.extend(slots.values())
    return out
def clear_tp_type(tp_type: TPType | str) -> int:
    """
    Clear ALL slots for a tp_type but keep the tp_type key.
    Returns how many slots were removed.
    """
    key = tp_type.value if isinstance(tp_type, TPType) else str(tp_type).upper().strip()
    removed = len(_ZONES.get(key, {}) or {})
    _ZONES[key] = {}
    _save_zones_to_disk()
    print(f"[TP-ZONES] Cleared tp_type {key} (removed {removed} slots)")
    return removed


def delete_tp_zone(tp_type: TPType, slot: int) -> bool:
    """
    Delete a single TP slot for a tp_type.
    Returns True if something was deleted.
    """
    global _ZONES
    key = tp_type.value
    slot_i = int(slot)

    if key not in _ZONES:
        return False
    if slot_i not in _ZONES[key]:
        return False

    del _ZONES[key][slot_i]

    # if no slots left, keep empty dict (or remove key — your choice)
    if not _ZONES[key]:
        _ZONES[key] = {}

    _save_zones_to_disk()
    return True


def get_configured_tp_types() -> List[str]:
    """
    Returns tp_type strings that currently have at least 1 configured slot.
    Example: ["LAUNCHSITE", "AIRFIELD"]
    """
    out: List[str] = []
    for tp_type, slots in _ZONES.items():
        if slots and len(slots) > 0:
            out.append(tp_type)
    out.sort()
    return out


def get_configured_slots(tp_type: TPType | str) -> List[int]:
    """
    Return configured slot numbers for a tp_type.
    Accepts TPType OR a string key like "AIRFIELD".
    """
    if isinstance(tp_type, str):
        key = tp_type.upper().strip()
    else:
        key = tp_type.value

    slots = _ZONES.get(key, {}) or {}
    out: List[int] = []
    for k in slots.keys():
        try:
            out.append(int(k))
        except Exception:
            continue
    out.sort()
    return out



# ============================
# COOLDOWN
# ============================

def _allowed_to_teleport(server_key: str, player_name: str, zone: TpZone, now_ts: float) -> bool:
    key = (server_key, player_name, zone.tp_type, zone.slot)
    last = _last_tp_times.get(key, 0.0)

    if now_ts - last < TP_ZONE_COOLDOWN:
        return False

    _last_tp_times[key] = now_ts
    return True


# ============================
# BUILD TELEPORT COMMAND
# ============================

def build_teleport_command(player_name: str, zone: TpZone) -> str:
    if zone.spawn_points and len(zone.spawn_points) > 0:
        x, y, z = random.choice(zone.spawn_points)
    else:
        x, y, z = zone.dest_x, zone.dest_y, zone.dest_z

    # NOTE: this uses tp_config.TELEPORT_COMMAND_TEMPLATE
    # You already corrected it to: teleportpos "{x},{y},{z}" "{player_name}"
    return TELEPORT_COMMAND_TEMPLATE.format(
        x=x,
        y=y,
        z=z,
        player_name=player_name,
    )


# ============================
# MAIN ZONE CHECKER
# ============================

def check_zones_for_player(
    server_key: str,
    player_name: str,
    x: float,
    y: float,
    z: float,
) -> List[str]:
    from time import time

    now_ts = time()
    cmds: List[str] = []

    player_key = (server_key, player_name)

    prev_zones: Set[Tuple[str, int]] = _last_player_zones.get(player_key, set())
    current_zones: Set[Tuple[str, int]] = set()

    for zone in get_all_zones():
        r = float(getattr(zone, "trigger_radius", 1.5) or 1.5)

        # ✅ true spherical distance check (not a box check)
        dx = x - zone.zone_x
        dy = y - zone.zone_y
        dz = z - zone.zone_z
        dist2 = (dx * dx) + (dy * dy) + (dz * dz)

        in_zone = dist2 <= (r * r)
        if not in_zone:
            continue

        current_zones.add((zone.tp_type, zone.slot))

        # only trigger on enter
        if (zone.tp_type, zone.slot) not in prev_zones:
            if _allowed_to_teleport(server_key, player_name, zone, now_ts):
                cmds.append(build_teleport_command(player_name, zone))

    _last_player_zones[player_key] = current_zones

   # print(f"[TP-CHECK-END] server={server_key} player={player_name} cmds={cmds}")
    return cmds


# ============================
# CLEAR ZONES FOR A TP TYPE
# ============================

def delete_tp_type(tp_type: TPType | str) -> int:
    """
    Delete ALL slots for a tp_type.
    Returns how many slots were removed.
    """
    key = tp_type.value if isinstance(tp_type, TPType) else str(tp_type)
    removed = len(_ZONES.get(key, {}) or {})
    _ZONES[key] = {}
    _save_zones_to_disk()
    print(f"[TP-ZONES] Deleted tp_type {key} (removed {removed} slots)")
    return removed


def delete_tp_type(tp_type: TPType | str) -> int:
    """
    Delete ALL slots for a tp_type.
    Returns how many slots were removed.
    """
    return clear_tp_type(tp_type)


