# starz_printpos/tp_config.py
import os

# ===============================
# MASTER SWITCH
# ===============================
PRINTPOS_ENABLED_DEFAULT: bool = True

# ===============================
# SMART PRINTPOS THROTTLING
# ===============================
FAR_DISTANCE_METERS: float = 200.0
FAR_SKIP_SECONDS: float = 60.0

PRINTPOS_BATCH_SIZE: int = 4
PRINTPOS_TICK_INTERVAL: float = 2.0
PER_COMMAND_DELAY: float = 0.35

# ===============================
# LOGGING
# ===============================
# One summary line every N seconds per server
PRINTPOS_STATUS_LOG_SECONDS: float = 5

# Set True ONLY when debugging
PRINTPOS_VERBOSE_LOGS: bool = False

# ===============================
# TP / TELEPORT
# ===============================
TP_ZONE_COOLDOWN: float = 3.0

TELEPORT_COMMAND_TEMPLATE = 'teleportpos "{x},{y},{z}" "{player_name}"'

TP_ZONES_JSON_PATH: str = os.path.join(
    os.path.dirname(__file__),
    "tp_zones.json",
)
