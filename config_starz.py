
# config_starz.py
import os

# ========= TOKENS / API =========

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# ========= GLOBAL CHANNELS / CATEGORIES =========

STAFF_ALERT_CHANNEL_ID = 1329658793054965770

# Ticket categories (STARZ tickets)
TICKET_CATEGORY_IDS = [1433322424644010035]

# If you’re actually using different ones for support/zorp, you can
# keep them too; otherwise we’ll just rely on TICKET_CATEGORY_IDS.
SUPPORT_CATEGORY_ID = 123      # <- update if you really use it
ZORP_CATEGORY_ID    = 456      # <- update if you really use it

# ========= STAFF ROLES =========

TRIAL_ADMIN_ID      = 1328549446237028362
SERVER_ADMIN_ID     = 1330294494402314322
HEAD_ADMIN_ID       = 1329989557512568932
ADMIN_MANAGEMENT_ID = 1345469982485516339
KAOS_MOD_ID         = 1345606282513485824
OWNER_ID = 1325965960083148802   # or your actual owner role ID


AI_CONTROL_ROLES = [
    TRIAL_ADMIN_ID,
    SERVER_ADMIN_ID,
    HEAD_ADMIN_ID,
    ADMIN_MANAGEMENT_ID,
    KAOS_MOD_ID,
]

# ID of the channel where promoter spawn alerts go
PROMOTER_ALERT_CHANNEL_ID = 1345465950174052432  # <- replace with real channel ID

# Role IDs that count as "promoters"
PROMOTER_ROLE_IDS = [
    1439781103232749668,  # e.g. streamer program
    1332539486281662505,  # e.g. starz streamer
]


# ========= BAN / KAOS / SHOP =========

ACTIVE_BANS_CHANNEL_ID   = 1447290713070112951
BAN_LOG_CHANNEL_ID       = 1407552376721641522
KAOS_COMMAND_CHANNEL_ID  = 1326749074174513243
SHOP_LOG_CHANNEL_ID      = 1326749074174513243
UNBAN_SHOP_PREFIX        = "UNBAN_PURCHASEIGN="

# ========= KAOS NUKE / POINT REWARD CONFIG =========
KAOS_LOG_CHANNEL_ID = 1326749074174513243   # your KAOS logs parser channel
KAOS_NUKE_ANNOUNCE_CHANNEL_ID = 1325671240198787144  # channel to send the NUKE announcement embed
KAOS_COMMAND_CHANNEL_ID = 1326749074174513243       # channel where KAOS listens to commands

# Image used for NUKE announcement
NUKE_IMAGE_URL = "https://cdn.discordapp.com/attachments/1325974275504738415/1448571239999209655/scrap_nuke.webp?ex=693bbe98&is=693a6d18&hm=16660e66d7c47c81562516ccba4beecb9c49eecb284208febd3dde2d8b0cda99&"
# ========= ZORP FEEDS =========

ZORP_FEED_CHANNEL_IDS = [
    1330240046460043296,
    1341926732197924884,
    1341926835713216603,
    1341926932031344781,
    1341926903535370261,
    1384251860898152538,
    1384251888199012534,
    1384251908612558858,
    1386137295123386580,
    1386576836305092700,
]

# ========= ADMIN MONITOR FEEDS =========

PLAYER_FEED_CHANNEL_IDS = [
    1351965195395928105,  # server 1
    1351965257681338519,  # server 2
    1351965286617579631,  # server 3
    1351965377697153095,  # server 4
    1351965349075091456,  # server 5
    1384251939482501150,  # server 6
    1384251959225094359,  # server 7
    1384251979169009745,  # server 8
    1386137324504617021,  # server 9
    1386576907163926670,  # server 10
]

ADMIN_FEED_CHANNEL_IDS = [
    1325974344358301752,  # server 1
    1340739830384038089,  # server 2
    1340740030900994150,  # server 3
    1341922496223383704,  # server 4
    1341922468113158205,  # server 5
    1384251796268257362,  # server 6
    1384251815499141300,  # server 7
    1384251834692272208,  # server 8
    1386137257798275183,  # server 9
    1386576777547088035,  # server 10
]

# ========= ADMIN MONITOR / ENFORCEMENT =========

# Per-admin activity embeds live here
ADMIN_MONITOR_LOG_CHANNEL_ID = 1447090579350618122

# Spawn-abuse alerts (MLRS / rockets / C4) go here (Head Admin channel)
ADMIN_ENFORCEMENT_CHANNEL_ID = 1345465950174052432
HEAD_ADMIN_CHANNEL_ID = ADMIN_ENFORCEMENT_CHANNEL_ID

# Roles allowed to use Ban / Unban buttons
ADMIN_ENFORCEMENT_ROLE_IDS = [
    HEAD_ADMIN_ID,
    ADMIN_MANAGEMENT_ID,
]



# ========= TEXT SNIPPETS FROM ENV (used by AI) =========

STYLE_TEXT_ENV_KEY      = "STYLE_TEXT"
RULES_TEXT_ENV_KEY      = "RULES_TEXT"
ZORP_GUIDE_TEXT_ENV_KEY = "ZORP_GUIDE_TEXT"
RAFFLE_TEXT_ENV_KEY     = "RAFFLE_TEXT"


def load_style_text() -> str:
    return os.getenv(STYLE_TEXT_ENV_KEY, "") or ""


def load_rules_text() -> str:
    return os.getenv(RULES_TEXT_ENV_KEY, "") or ""


def load_zorp_guide_text() -> str:
    """
    Prefer loading the ZORP guide from configzorp_guide.txt
    in the same folder as this file. If that fails, fall back
    to the ZORP_GUIDE_TEXT env var.
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        guide_path = os.path.join(base_dir, "configzorp_guide.txt")
        print(f"[ZORP] Looking for guide at: {guide_path}")

        if os.path.isfile(guide_path):
            print("[ZORP] Guide file exists, reading...")
            with open(guide_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
                print(f"[ZORP] Guide file length: {len(text)} characters")
                if text:
                    print(f"[ZORP] Loaded guide from file.")
                    return text
                else:
                    print("[ZORP] Guide file is empty.")
        else:
            print("[ZORP] Guide file does NOT exist at that path.")
    except Exception as e:
        print(f"[ZORP] Error loading configzorp_guide.txt: {e}")

    # Fallback: env var
    text = os.getenv(ZORP_GUIDE_TEXT_ENV_KEY, "") or ""
    if text:
        print("[ZORP] Loaded guide from ZORP_GUIDE_TEXT env var.")
    else:
        print("[ZORP] No ZORP guide text found; using generic fallback.")
    return text


def load_raffle_text() -> str:
    """
    Prefer loading the raffle guide from configraffle_guide.txt
    in the same folder as this file. If that fails, fall back
    to the RAFFLE_TEXT env var or a generic message.
    """
    import os

    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        guide_path = os.path.join(base_dir, "configraffle_guide.txt")
        print(f"[RAFFLE] Looking for guide at: {guide_path}")

        if os.path.isfile(guide_path):
            print("[RAFFLE] Guide file exists, reading...")
            with open(guide_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
                print(f"[RAFFLE] Guide file length: {len(text)} characters")
                if text:
                    print("[RAFFLE] Loaded raffle guide from file.")
                    return text
                else:
                    print("[RAFFLE] Guide file is empty, falling back to env.")
        else:
            print("[RAFFLE] No raffle guide file found, falling back to env.")
    except Exception as e:
        print(f"[RAFFLE] Error reading raffle guide file: {e}. Falling back to env.")

    text = os.getenv(RAFFLE_TEXT_ENV_KEY, "").strip()
    if text:
        print("[RAFFLE] Loaded raffle text from RAFFLE_TEXT env var.")
        return text

    print("[RAFFLE] No raffle text found; using generic fallback.")
    return (
        "STARZ raffles use tickets earned from airdrops, Discord events, and "
        "special promotions. More tickets = more chances, but no guaranteed win."
    )


# ========= ADMIN MONITOR / ENFORCEMENT =========

# High-risk items that trigger auto-enforcement
HIGH_RISK_SPAWN_ITEMS = {
    # Shortnames (if logs ever include item IDs)
    "ammo.rocket.basic",
    "ammo.rocket.hv",
    "ammo.rocket.mlrs",
    "explosive.timed",

    # Plain English console log matches
    "rocket",
    "hv rocket",
    "incendiary rocket",
    "mlrs rocket",
    "timed explosive",
    "timed explosive charge",
    "c4",
}



RCON_ENABLED = True  # False if you want it off / True if you want it on

