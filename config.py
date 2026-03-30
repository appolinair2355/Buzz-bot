# config.py
"""
Configuration BACCARAT PRO

Variables OBLIGATOIRES (doivent etre configurees) :
  - API_ID              : ID API Telegram (my.telegram.org)
  - API_HASH            : Hash API Telegram (my.telegram.org)
  - BOT_TOKEN           : Token du bot (@BotFather)
  - ADMIN_ID            : Votre ID Telegram utilisateur

Variables optionnelles (valeurs par defaut deja configurees) :
  - PREDICTION_CHANNEL_ID   (defaut : -1003572542646)
  - CHANNEL_INVERSE_ID      (defaut : -1003800711004)
  - CHANNEL_MANQUE_ID       (defaut : -1003773458877)
  - CHANNEL_COMPTEUR3_ID    (defaut : -1003572542646)
  - CHANNEL_COMPTEUR1_ID    (defaut : -1003572542646)
  - PORT               (defaut : 10000 — injecte automatiquement par la plateforme)
  - API_POLL_INTERVAL  (defaut : 5)
  - COMPTEUR2_ACTIVE   (defaut : true)
  - COMPTEUR2_B        (defaut : 5)
  - COMPTEUR3_ACTIVE   (defaut : true)
  - COMPTEUR3_B        (defaut : 5)
  - COMPTEUR1_ACTIVE   (defaut : true)
  - COMPTEUR1_B        (defaut : 8)
  - TELEGRAM_SESSION
"""

import os

def parse_channel_id(value: str) -> int:
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        raise ValueError(f"ID de canal invalide : {value}")

def parse_optional_channel_id(value: str) -> int:
    if not value or value.strip() == "":
        return 0
    try:
        return parse_channel_id(value.strip())
    except:
        return 0

# ============================================================================
# IDS PAR DEFAUT — modifiez ici si vous utilisez vos propres canaux
# ============================================================================

_DEFAULT_PREDICTION_CHANNEL_ID = "-1003774449498"
_DEFAULT_CHANNEL_INVERSE_ID    = "-1003800711004"
_DEFAULT_CHANNEL_MANQUE_ID     = "-1003773458877"
_DEFAULT_CHANNEL_COMPTEUR3_ID  = "-1003572542646"
_DEFAULT_CHANNEL_COMPTEUR1_ID  = "-1003572542646"

# ============================================================================
# VARIABLES D ENVIRONNEMENT - OBLIGATOIRES
# ============================================================================

ADMIN_ID         = int(os.getenv("ADMIN_ID", "0") or "0")
API_ID           = int(os.getenv("API_ID", "0") or "0")
API_HASH         = os.getenv("API_HASH", "") or ""
BOT_TOKEN        = os.getenv("BOT_TOKEN", "") or ""
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "") or ""

# ============================================================================
# CANAUX DE PREDICTION (valeurs par defaut pre-configurees)
# ============================================================================

# Canal principal : toutes les predictions partent de ce canal
PREDICTION_CHANNEL_ID = parse_optional_channel_id(
    os.getenv("PREDICTION_CHANNEL_ID", _DEFAULT_PREDICTION_CHANNEL_ID) or _DEFAULT_PREDICTION_CHANNEL_ID
)

# Compteur2 : Dogon 2 (inverse)
CHANNEL_INVERSE_ID = parse_optional_channel_id(
    os.getenv("CHANNEL_INVERSE_ID", _DEFAULT_CHANNEL_INVERSE_ID) or _DEFAULT_CHANNEL_INVERSE_ID
)

# Compteur2 : Dogon 1 (manquant)
CHANNEL_MANQUE_ID = parse_optional_channel_id(
    os.getenv("CHANNEL_MANQUE_ID", _DEFAULT_CHANNEL_MANQUE_ID) or _DEFAULT_CHANNEL_MANQUE_ID
)

# Compteur3 : Bot3 — canal dedie
CHANNEL_COMPTEUR3_ID = parse_optional_channel_id(
    os.getenv("CHANNEL_COMPTEUR3_ID", _DEFAULT_CHANNEL_COMPTEUR3_ID) or _DEFAULT_CHANNEL_COMPTEUR3_ID
)

# Compteur1 : Bot1 — canal dedie
CHANNEL_COMPTEUR1_ID = parse_optional_channel_id(
    os.getenv("CHANNEL_COMPTEUR1_ID", _DEFAULT_CHANNEL_COMPTEUR1_ID) or _DEFAULT_CHANNEL_COMPTEUR1_ID
)

# ============================================================================
# PARAMETRES DU BOT
# ============================================================================

PORT             = int(os.getenv("PORT", "10000") or "10000")
API_POLL_INTERVAL = int(os.getenv("API_POLL_INTERVAL", "5") or "5")

COMPTEUR2_ACTIVE = (os.getenv("COMPTEUR2_ACTIVE", "true") or "true").lower() == "true"
COMPTEUR2_B      = int(os.getenv("COMPTEUR2_B", "5") or "5")

COMPTEUR3_ACTIVE = (os.getenv("COMPTEUR3_ACTIVE", "true") or "true").lower() == "true"
COMPTEUR3_B      = int(os.getenv("COMPTEUR3_B", "5") or "5")

COMPTEUR1_ACTIVE = (os.getenv("COMPTEUR1_ACTIVE", "true") or "true").lower() == "true"
COMPTEUR1_B      = int(os.getenv("COMPTEUR1_B", "8") or "8")

# ============================================================================
# CONSTANTES — NE PAS MODIFIER
# ============================================================================

ALL_SUITS = ["♠", "♥", "♦", "♣"]

SUIT_DISPLAY = {
    "♠": "♠️",
    "♥": "❤️",
    "♦": "♦️",
    "♣": "♣️"
}

# Miroirs Compteur3 : ❤️ ↔ ♦️  |  ♠️ ↔ ♣️
SUIT_INVERSE = {
    "♥": "♦",
    "♦": "♥",
    "♠": "♣",
    "♣": "♠",
}

# Prediction Bot2 Compteur2 (Dogon 2) :
#   ♣️ manque → ♦️  |  ♠️ manque → ❤️
#   ♦️ manque → ♣️  |  ❤️ manque → ♠️
SUIT_INVERSE_C2 = {
    "♣": "♦",
    "♠": "♥",
    "♦": "♣",
    "♥": "♠",
}

# Prediction Compteur1 (Bot1) :
#   ❤️ manque → ♣️  |  ♠️ manque → ♦️
#   ♦️ manque → ♠️  |  ♣️ manque → ❤️
SUIT_INVERSE_C1 = {
    "♥": "♣",
    "♠": "♦",
    "♦": "♠",
    "♣": "♥",
}
