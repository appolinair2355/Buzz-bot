# config.py
"""
╔══════════════════════════════════════════════════════════════════╗
║              BUZZ INFLUENCE — Configuration Bot                  ║
╚══════════════════════════════════════════════════════════════════╝

Seules 3 variables DOIVENT être définies dans l'environnement :
  - API_HASH         : Hash API Telegram  (my.telegram.org)
  - BOT_TOKEN        : Token du bot       (@BotFather)
  - TELEGRAM_SESSION : Session Telethon   (StringSession — optionnel)

Tout le reste est déjà configuré directement ici.
"""

import os
import sys

# ============================================================================
# HELPERS DE PARSING
# ============================================================================

def _int(v):
    try:
        return int(v)
    except Exception:
        return 0

def _bool(v):
    return str(v).lower() in ("1", "true", "yes", "on")

# ============================================================================
# CREDENTIALS TELEGRAM
# ── API_ID et ADMIN_ID sont fixes (non-secrets)
# ── API_HASH et BOT_TOKEN restent en variables d'environnement (secrets)
# ============================================================================

API_ID           = 30696801
ADMIN_ID         = 8649780855
API_HASH         = os.getenv("API_HASH", "")  or ""
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")  or ""
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "") or ""

# ============================================================================
# CANAUX DE PRÉDICTION — valeurs fixes
# ============================================================================

#  BOT1 (𝐁𝐎𝐓𝟏) — Canal principal : toutes les prédictions arrivent ici
PREDICTION_CHANNEL_ID = -1003774449498

#  BOT2 (𝐁𝐎𝐓𝟐) — Redirect Compteur1 / Manque (B=8)
CHANNEL_COMPTEUR1_ID  = -1003773458877

#  BOT3 (𝐁𝐎𝐓𝟑) — Redirect Compteur2 / Dogon 2 (B=5)
CHANNEL_INVERSE_ID    = -1003800711004

#  BOT1 (𝐁𝐎𝐓𝟏) — Redirect Compteur3 / Miroir (B=5) — même canal que principal
CHANNEL_COMPTEUR3_ID  = -1003774449498

# ============================================================================
# PARAMÈTRES DU BOT — valeurs fixes
# ============================================================================

PORT              = int(os.getenv("PORT", "8000"))   # 8000 local | 10000 Render (défini via render.yaml)
API_POLL_INTERVAL = 3                                # Intervalle de polling API en secondes

# Compteur1 — BOT2 / Manque
COMPTEUR1_ACTIVE  = True
COMPTEUR1_B       = 8       # Seuil d'absences consécutives

# Compteur2 — BOT3 / Dogon 2
COMPTEUR2_ACTIVE  = True
COMPTEUR2_B       = 5       # Seuil d'absences consécutives

# Compteur3 — BOT1 / Miroir
COMPTEUR3_ACTIVE  = True
COMPTEUR3_B       = 5       # Seuil d'absences consécutives

# ============================================================================
# VALIDATION AU DÉMARRAGE
# ============================================================================

def validate_config() -> bool:
    """Vérifie que les secrets obligatoires sont définis.
    Retourne True si OK, affiche les erreurs et retourne False sinon.
    """
    errors = []
    if not API_HASH:
        errors.append("  ❌ API_HASH manquant — définir la variable d'environnement API_HASH")
    if not BOT_TOKEN:
        errors.append("  ❌ BOT_TOKEN manquant — définir la variable d'environnement BOT_TOKEN")

    if errors:
        print("╔══ ERREUR DE CONFIGURATION ══════════════════════════════════╗")
        for e in errors:
            print(e)
        print("╚═════════════════════════════════════════════════════════════╝")
        return False

    if not TELEGRAM_SESSION:
        print("⚠️  TELEGRAM_SESSION absent — mode bot-token standard activé.")

    return True

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

# Miroirs Compteur3/BOT1 : ❤️ ↔ ♦️  |  ♠️ ↔ ♣️
SUIT_INVERSE = {
    "♥": "♦",
    "♦": "♥",
    "♠": "♣",
    "♣": "♠",
}

# Prédiction BOT3/Compteur2 (Dogon 2)
SUIT_INVERSE_C2 = {
    "♣": "♦",
    "♠": "♥",
    "♦": "♣",
    "♥": "♠",
}

# Prédiction BOT2/Compteur1 (Manque)
SUIT_INVERSE_C1 = {
    "♥": "♣",
    "♠": "♦",
    "♦": "♠",
    "♣": "♥",
}
