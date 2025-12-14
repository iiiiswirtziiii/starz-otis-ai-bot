# starz_printpos/tp_config.py

import os

# -------- Master switch default (can be toggled at runtime) --------
PRINTPOS_ENABLED_DEFAULT: bool = True  # change to False if you want it off by default

# -------- Polling / rate limits --------
# -------- Far-distance throttle --------
# If player is farther than this from ALL TP zone centers, skip polling them
FAR_DISTANCE_METERS: float = 200.0
FAR_SKIP_SECONDS: float = 60.0

# -------- Logging --------
# One summary line every N seconds (per server) instead of spam per printpos
PRINTPOS_STATUS_LOG_SECONDS: float = 20.0
# Shut off per-player spam logs
PRINTPOS_VERBOSE_LOGS: bool = False

# -------- Smart polling throttle --------
# If player is farther than this from ALL TP zone centers, skip polling them for FAR_SKIP_SECONDS.
FAR_DISTANCE_METERS: float = 200.0
FAR_SKIP_SECONDS: float = 60.0

# -------- Logging --------
PRINTPOS_STATUS_LOG_SECONDS: float = 20.0   # one summary line every N seconds
PRINTPOS_VERBOSE_LOGS: bool = False         # shuts off per-player spam

PRINTPOS_BATCH_SIZE: int = 4
PRINTPOS_TICK_INTERVAL: float = 2.0
PER_COMMAND_DELAY: float = 0.35        # small delay between printpos commands
# ----------------------------------------
# Smart printpos throttling (STARZ)
# ----------------------------------------

# If player is farther than this from ALL TP zone centers,
# skip polling them for FAR_SKIP_SECONDS
FAR_DISTANCE_METERS: float = 200.0
FAR_SKIP_SECONDS: float = 60.0

# ----------------------------------------
# Printpos logging
# ----------------------------------------

# One summary line every N seconds per server
PRINTPOS_STATUS_LOG_SECONDS: float = 20.0

# Set True only when debugging printpos behavior
PRINTPOS_VERBOSE_LOGS: bool = False

# -------- TP zones / teleports --------
TP_ZONE_COOLDOWN: float = 3.0          # seconds before same player can re-trigger same zone

# Teleport command template.
# Replace with your actual teleport command (from the server that already does it).
# {entity_id}, {x}, {y}, {z} will be formatted.
TELEPORT_COMMAND_TEMPLATE = 'teleportpos "{x},{y},{z}" "{player_name}"'
# Where to store zone configs (JSON)
TP_ZONES_JSON_PATH: str = os.path.join(
    os.path.dirname(__file__),
    "tp_zones.json",
)
