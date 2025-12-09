
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

AI_CONTROL_ROLES = [
    TRIAL_ADMIN_ID,
    SERVER_ADMIN_ID,
    HEAD_ADMIN_ID,
    ADMIN_MANAGEMENT_ID,
    KAOS_MOD_ID,
]

# ========= BAN / KAOS / SHOP =========

ACTIVE_BANS_CHANNEL_ID   = 1447290713070112951
BAN_LOG_CHANNEL_ID       = 1407552376721641522
KAOS_COMMAND_CHANNEL_ID  = 1326749074174513243
SHOP_LOG_CHANNEL_ID      = 1326749074174513243
UNBAN_SHOP_PREFIX        = "UNBAN_PURCHASEIGN="

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

ADMIN_MONITOR_LOG_CHANNEL_ID = 1447090579350618122

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
    return os.getenv(ZORP_GUIDE_TEXT_ENV_KEY, "") or ""

def load_raffle_text() -> str:
    return os.getenv(RAFFLE_TEXT_ENV_KEY, "") or ""
