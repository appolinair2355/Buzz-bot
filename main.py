import re
import asyncio
import logging
import sys
import traceback
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError, FloodWaitError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, CHANNEL_INVERSE_ID,
    CHANNEL_COMPTEUR3_ID, CHANNEL_COMPTEUR1_ID,
    PORT, API_POLL_INTERVAL,
    ALL_SUITS, SUIT_DISPLAY, SUIT_INVERSE, SUIT_INVERSE_C2, SUIT_INVERSE_C1,
    COMPTEUR2_ACTIVE, COMPTEUR2_B,
    COMPTEUR3_ACTIVE, COMPTEUR3_B,
    COMPTEUR1_ACTIVE, COMPTEUR1_B,
    TELEGRAM_SESSION,
    validate_config,
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not validate_config():
    logger.error("❌ Configuration invalide — arrêt du bot.")
    sys.exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

# Prédictions en attente de vérification
# {game_number: {suit, triggered_by, message_id, status, awaiting_rattrapage, ...}}
pending_inverse: Dict[int, dict] = {}    # Compteur2 — Dogon 2 (inverse/miroir)
pending_manque: Dict[int, dict] = {}     # Compteur2 — Dogon 1 (manquant)
pending_compteur3: Dict[int, dict] = {}  # Compteur3 — inverse seulement
pending_compteur1: Dict[int, dict] = {}  # Compteur1 — prédiction unique

# Compteur2 - absences consécutives par couleur (costumes du joueur)
compteur2_active = COMPTEUR2_ACTIVE
compteur2_b = COMPTEUR2_B
compteur2_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur2_last_game = 0
compteur2_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur2_processed_games: set = set()

# Compteur3 - absences consécutives par couleur (costumes du joueur)
compteur3_active = COMPTEUR3_ACTIVE
compteur3_b = COMPTEUR3_B
compteur3_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur3_last_game = 0
compteur3_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur3_processed_games: set = set()
last_prediction_game_c3: int = 0

# Compteur1 - absences consécutives par couleur (costumes du joueur)
compteur1_active = COMPTEUR1_ACTIVE
compteur1_b = COMPTEUR1_B
compteur1_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur1_last_game = 0
compteur1_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur1_processed_games: set = set()
last_prediction_game_c1: int = 0

# Mode Attente - attend PERDU avant de prédire à nouveau
attente_mode = False
attente_locked = False

# Historique des prédictions
prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# Jeux pour lesquels la main du joueur a déjà été traitée
player_processed_games: set = set()

# Cache des derniers résultats API {game_number: result_dict}
api_results_cache: Dict[int, dict] = {}

# Dernier numéro de jeu pour lequel une prédiction a été envoyée
last_prediction_game: int = 0

# Pour éviter de déclencher le reset plusieurs fois pour la partie 1440
reset_done_for_cycle: bool = False

# ============================================================================
# INTERVALLES HORAIRES - Prédictions autorisées (heure du Bénin = UTC+1)
# ============================================================================

BENIN_TZ = timezone(timedelta(hours=1))

prediction_intervals: List[Dict[str, int]] = []
intervals_enabled: bool = False

def is_prediction_allowed_now() -> bool:
    if not intervals_enabled or not prediction_intervals:
        return True
    now_benin = datetime.now(BENIN_TZ)
    current_total = now_benin.hour * 60 + now_benin.minute
    for interval in prediction_intervals:
        start_total = interval["start"] * 60
        end_total = interval["end"] * 60
        if start_total <= end_total:
            if start_total <= current_total < end_total:
                return True
        else:
            if current_total >= start_total or current_total < end_total:
                return True
    return False

def get_intervals_status_text() -> str:
    now_benin = datetime.now(BENIN_TZ)
    status = "✅ ON" if intervals_enabled else "❌ OFF"
    allowed = "✅ OUI" if is_prediction_allowed_now() else "🚫 NON"
    lines = [
        "⏰ **Intervalles de prédiction**",
        f"Mode restriction: {status}",
        f"Heure Bénin actuelle: {now_benin.strftime('%H:%M')}",
        f"Prédiction autorisée: {allowed}",
        "",
    ]
    if prediction_intervals:
        lines.append("Intervalles configurés:")
        for i, iv in enumerate(prediction_intervals, 1):
            lines.append(f"  {i}. {iv['start']:02d}h00 → {iv['end']:02d}h00")
    else:
        lines.append("Aucun intervalle défini (prédictions toujours autorisées si mode OFF)")
    return "\n".join(lines)

# ============================================================================
# UTILITAIRES - Costumes
# ============================================================================

def normalize_suit(suit_emoji: str) -> str:
    return suit_emoji.replace('\ufe0f', '').replace('❤', '♥')

def player_suits_from_cards(player_cards: list) -> List[str]:
    suits = set()
    for card in player_cards:
        raw = card.get('S', '')
        normalized = normalize_suit(raw)
        if normalized in ALL_SUITS:
            suits.add(normalized)
    return list(suits)

def has_player_cards(result: dict) -> bool:
    return len(result.get('player_cards', [])) >= 2

# ============================================================================
# UTILITAIRES - Canaux
# ============================================================================

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        normalized = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized)
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

async def send_to_redirect_channel(channel_id: int, text: str):
    """Envoie un message propre (sans mention Bot) vers un canal de redirection.
    Retourne le message envoyé (ou None en cas d'échec)."""
    if not channel_id:
        return None
    try:
        dest_entity = await resolve_channel(channel_id)
        if not dest_entity:
            logger.warning(f"⚠️ Canal de redirection inaccessible: {channel_id}")
            return None
        sent = await client.send_message(dest_entity, text)
        logger.info(f"📤 Message envoyé vers canal {channel_id}")
        return sent
    except Exception as e:
        logger.error(f"❌ Erreur envoi vers {channel_id}: {e}")
        return None

# ============================================================================
# MESSAGES DE PRÉDICTION
# ============================================================================

def _result_icon(status: str) -> str:
    return status if status.startswith('✅') else "❌"

# ── Canal principal : Compteur2 Bot2 ───────────────────────────────────────

def build_prediction_msg_inverse(game_number: int, suit: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Dogon 2"
    )

def build_prediction_msg_manque(game_number: int, suit: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Dogon 1"
    )

def build_result_msg_inverse(game_number: int, suit: str, status: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{_result_icon(status)}\n"
        f"Mode: Dogon 2"
    )

def build_result_msg_manque(game_number: int, suit: str, status: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{_result_icon(status)}\n"
        f"Mode: Dogon 1"
    )

# ── Canaux de redirection Compteur2 : sans Bot ─────────────────────────────

def build_redirect_msg(game_number: int, suit: str, status: str = '⌛') -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    icon = status if status in ('⌛',) or status.startswith('✅') else "❌"
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{icon}\n"
        f"Mode: Dogon 2"
    )

# ── Compteur3 : canal BOT1 ─────────────────────────────────────────────────

def build_prediction_msg_compteur3(game_number: int, suit: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Miroir"
    )

def build_result_msg_compteur3(game_number: int, suit: str, status: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{_result_icon(status)}\n"
        f"Mode: Miroir"
    )

def build_redirect_msg_compteur3(game_number: int, suit: str, status: str = '⌛') -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    icon = status if status in ('⌛',) or status.startswith('✅') else "❌"
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{icon}\n"
        f"Mode: Miroir"
    )

# ── Compteur1 : canal BOT2 ─────────────────────────────────────────────────

def build_prediction_msg_compteur1(game_number: int, suit: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:⌛\n"
        f"Mode: Manque"
    )

def build_result_msg_compteur1(game_number: int, suit: str, status: str) -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{_result_icon(status)}\n"
        f"Mode: Manque"
    )

def build_redirect_msg_compteur1(game_number: int, suit: str, status: str = '⌛') -> str:
    suit_display = SUIT_DISPLAY.get(suit, suit)
    icon = status if status in ('⌛',) or status.startswith('✅') else "❌"
    return (
        f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨\n"
        f"🎮GAME: #N{game_number}\n"
        f"🃏Carte {suit_display}:{icon}\n"
        f"Mode: Manque"
    )

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(game_number: int, suit_inverse: str, suit_manque: str, triggered_by_suit: str):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit_inverse': suit_inverse,
        'suit_manque': suit_manque,
        'triggered_by': triggered_by_suit,
        'predicted_at': datetime.now(),
        'status_inverse': 'en_cours',
        'status_manque': 'en_cours',
        'silent': attente_mode,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_history_status(game_number: int, pred_type: str, status: str):
    for pred in prediction_history:
        if pred['predicted_game'] == game_number:
            key = f'status_{pred_type}'
            if key in pred:
                pred[key] = status
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS
# ============================================================================

async def send_compteur2_prediction(game_number: int, missing_suit: str) -> bool:
    """Envoie UNE prédiction Compteur2 (Dogon2 B=5) directement vers BOT3 (CHANNEL_INVERSE_ID)."""
    global last_prediction_time, attente_locked, last_prediction_game

    if game_number in pending_inverse:
        logger.info(f"⏸ Compteur2 #{game_number} ignoré: déjà en attente de vérification")
        return False

    if not is_prediction_allowed_now():
        now_benin = datetime.now(BENIN_TZ)
        logger.info(
            f"⏰ Compteur2 #{game_number} bloqué: hors intervalle "
            f"(heure Bénin: {now_benin.strftime('%H:%M')})"
        )
        return False

    if not CHANNEL_INVERSE_ID:
        logger.error("❌ CHANNEL_INVERSE_ID (BOT3) non configuré")
        return False

    dest_entity = await resolve_channel(CHANNEL_INVERSE_ID)
    if not dest_entity:
        logger.error(f"❌ Canal BOT3 inaccessible: {CHANNEL_INVERSE_ID}")
        return False

    predicted_suit = SUIT_INVERSE_C2.get(missing_suit, missing_suit)

    try:
        msg = build_prediction_msg_inverse(game_number, predicted_suit)
        sent = await client.send_message(dest_entity, msg)

        last_prediction_time = datetime.now()
        last_prediction_game = game_number

        pending_inverse[game_number] = {
            'suit': predicted_suit,
            'message_id': sent.id,
            'redirect_message_id': None,
            'status': '⌛',
            'awaiting_rattrapage': 0,
            'triggered_by': missing_suit,
        }

        add_prediction_to_history(game_number, predicted_suit, "", missing_suit)

        logger.info(
            f"✅ Compteur2 (Dogon2) prédiction → BOT3 #{game_number} "
            f"{predicted_suit} (déclenché par {missing_suit} absent {compteur2_b}x)"
        )
        return True

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {CHANNEL_INVERSE_ID}")
        return False
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {CHANNEL_INVERSE_ID}")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction Compteur2: {e}")
        return False

async def send_compteur3_prediction(game_number: int, missing_suit: str) -> bool:
    """Envoie UNE prédiction Compteur3 (Miroir B=5) directement vers BOT1 (PREDICTION_CHANNEL_ID)."""
    global last_prediction_game_c3

    if game_number in pending_compteur3:
        logger.info(f"⏸ Compteur3 #{game_number} ignoré: déjà en attente de vérification")
        return False

    if not is_prediction_allowed_now():
        logger.info(f"⏰ Compteur3 #{game_number} bloqué: hors intervalle")
        return False

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID (BOT1) non configuré")
        return False

    dest_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not dest_entity:
        logger.error(f"❌ Canal BOT1 inaccessible: {PREDICTION_CHANNEL_ID}")
        return False

    inverse_suit = SUIT_INVERSE.get(missing_suit, missing_suit)

    try:
        msg = build_prediction_msg_compteur3(game_number, inverse_suit)
        sent = await client.send_message(dest_entity, msg)

        last_prediction_game_c3 = game_number

        pending_compteur3[game_number] = {
            'suit': inverse_suit,
            'message_id': sent.id,
            'redirect_message_id': None,
            'status': '⌛',
            'awaiting_rattrapage': 0,
            'triggered_by': missing_suit,
        }

        logger.info(
            f"✅ Compteur3 (Miroir) prédiction → BOT1 #{game_number} "
            f"Miroir={inverse_suit} (déclenché par {missing_suit} absent {compteur3_b}x)"
        )
        return True

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        return False
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction Compteur3: {e}")
        return False

async def send_compteur1_prediction(game_number: int, missing_suit: str) -> bool:
    """Envoie UNE prédiction Compteur1 (Manque B=8) directement vers BOT2 (CHANNEL_COMPTEUR1_ID)."""
    global last_prediction_game_c1

    if game_number in pending_compteur1:
        logger.info(f"⏸ Compteur1 #{game_number} ignoré: déjà en attente de vérification")
        return False

    if not is_prediction_allowed_now():
        logger.info(f"⏰ Compteur1 #{game_number} bloqué: hors intervalle")
        return False

    if not CHANNEL_COMPTEUR1_ID:
        logger.error("❌ CHANNEL_COMPTEUR1_ID (BOT2) non configuré")
        return False

    dest_entity = await resolve_channel(CHANNEL_COMPTEUR1_ID)
    if not dest_entity:
        logger.error(f"❌ Canal BOT2 inaccessible: {CHANNEL_COMPTEUR1_ID}")
        return False

    predicted_suit = SUIT_INVERSE_C1.get(missing_suit, missing_suit)

    try:
        msg = build_prediction_msg_compteur1(game_number, predicted_suit)
        sent = await client.send_message(dest_entity, msg)

        last_prediction_game_c1 = game_number

        pending_compteur1[game_number] = {
            'suit': predicted_suit,
            'message_id': sent.id,
            'redirect_message_id': None,
            'status': '⌛',
            'awaiting_rattrapage': 0,
            'triggered_by': missing_suit,
        }

        logger.info(
            f"✅ Compteur1 (Manque) prédiction → BOT2 #{game_number} "
            f"{predicted_suit} (déclenché par {missing_suit} absent {compteur1_b}x)"
        )
        return True

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {CHANNEL_COMPTEUR1_ID}")
        return False
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {CHANNEL_COMPTEUR1_ID}")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction Compteur1: {e}")
        return False

async def update_prediction_message(game_number: int, pred_type: str, status: str, trouve: bool):
    """Met à jour le message de prédiction dans le canal propre à chaque compteur.
      compteur3  → BOT1 (PREDICTION_CHANNEL_ID)
      inverse    → BOT3 (CHANNEL_INVERSE_ID)
      compteur1  → BOT2 (CHANNEL_COMPTEUR1_ID)
    """
    global attente_locked

    if pred_type == 'inverse':
        pending = pending_inverse
        main_channel_id = CHANNEL_INVERSE_ID
        new_msg_fn = lambda g, s, st: build_result_msg_inverse(g, s, st)
    elif pred_type == 'manque':
        pending = pending_manque
        main_channel_id = CHANNEL_INVERSE_ID
        new_msg_fn = lambda g, s, st: build_result_msg_manque(g, s, st)
    elif pred_type == 'compteur3':
        pending = pending_compteur3
        main_channel_id = PREDICTION_CHANNEL_ID
        new_msg_fn = lambda g, s, st: build_result_msg_compteur3(g, s, st)
    elif pred_type == 'compteur1':
        pending = pending_compteur1
        main_channel_id = CHANNEL_COMPTEUR1_ID
        new_msg_fn = lambda g, s, st: build_result_msg_compteur1(g, s, st)
    else:
        logger.error(f"❌ update_prediction_message: pred_type inconnu '{pred_type}'")
        return

    if game_number not in pending:
        return

    pred = pending[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    new_msg = new_msg_fn(game_number, suit, status)

    try:
        dest_entity = await resolve_channel(main_channel_id)
        if not dest_entity:
            logger.error(f"❌ Canal [{pred_type.upper()}] inaccessible pour mise à jour: {main_channel_id}")
            return

        await client.edit_message(dest_entity, msg_id, new_msg)

        pred['status'] = status
        if pred_type in ('inverse', 'manque'):
            update_history_status(game_number, pred_type, 'gagne' if trouve else 'perdu')

        if trouve:
            logger.info(f"✅ [{pred_type.upper()}] Gagné: #{game_number} {suit} ({status})")
        else:
            logger.info(f"❌ [{pred_type.upper()}] Perdu: #{game_number} {suit}")

        if game_number in pending:
            del pending[game_number]

        # Mode Attente: déverrouille quand Compteur2 est résolue
        if attente_mode and not trouve and pred_type != 'compteur3':
            if game_number not in pending_inverse and game_number not in pending_manque:
                attente_locked = False
                logger.info("🔓 Mode Attente: prédiction perdue → prêt")

    except Exception as e:
        logger.error(f"❌ Erreur update message [{pred_type}]: {e}")

# ============================================================================
# VÉRIFICATION DYNAMIQUE (dès que les cartes du joueur apparaissent)
# ============================================================================

MAX_RATTRAPAGE = 2

async def check_one_pending(game_number: int, player_suits: List[str], is_finished: bool,
                             pending: dict, pred_type: str):
    """Vérifie les prédictions d'un type pour un jeu donné.

    - Vérification directe : si le jeu prédit est celui en cours (awaiting_rattrapage == 0)
    - Rattrapages : vérifie TOUS les rattrapages en attente dont le tour correspond à ce jeu
    Les deux blocs s'exécutent toujours : un return prématuré ferait rater les rattrapages
    d'anciennes prédictions qui tombent sur le même numéro de partie.
    """

    # --- Vérification directe (awaiting_rattrapage == 0) ---
    if game_number in pending:
        pred = pending[game_number]
        if pred.get('awaiting_rattrapage', 0) == 0:
            target_suit = pred['suit']
            if target_suit in player_suits:
                logger.info(f"🔍 [{pred_type.upper()}] #{game_number}: {target_suit} ✅0️⃣")
                await update_prediction_message(game_number, pred_type, '✅0️⃣', True)
            elif is_finished:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"🔍 [{pred_type.upper()}] #{game_number}: {target_suit} ❌ → R1 #{game_number+1}")
            # PAS de return ici : continuer pour vérifier aussi les rattrapages d'anciennes prédictions

    # --- Vérifications rattrapages (toutes les entrées en attente de rattrapage sur ce jeu) ---
    for original_game, pred in list(pending.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting <= 0:
            continue
        if game_number != original_game + awaiting:
            continue

        target_suit = pred['suit']
        if target_suit in player_suits:
            status = f'✅{awaiting}️⃣'
            logger.info(f"🔍 [{pred_type.upper()}] R{awaiting} #{game_number}: {target_suit} ✅")
            await update_prediction_message(original_game, pred_type, status, True)
        elif is_finished:
            if awaiting < MAX_RATTRAPAGE:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(
                    f"🔍 [{pred_type.upper()}] R{awaiting} #{game_number}: "
                    f"{target_suit} ❌ → R{awaiting+1} #{original_game+awaiting+1}"
                )
            else:
                logger.info(f"🔍 [{pred_type.upper()}] R{MAX_RATTRAPAGE} #{game_number}: perdu")
                await update_prediction_message(original_game, pred_type, '❌', False)
        # Continuer la boucle : plusieurs rattrapages peuvent attendre le même jeu

async def check_prediction_result_dynamic(game_number: int, player_suits: List[str], is_finished: bool):
    await check_one_pending(game_number, player_suits, is_finished, pending_inverse, 'inverse')
    await check_one_pending(game_number, player_suits, is_finished, pending_manque, 'manque')
    await check_one_pending(game_number, player_suits, is_finished, pending_compteur3, 'compteur3')
    await check_one_pending(game_number, player_suits, is_finished, pending_compteur1, 'compteur1')

# ============================================================================
# COMPTEUR2 - Logique principale (costumes du joueur)
# ============================================================================

def get_compteur2_status_text() -> str:
    status = "✅ ON" if compteur2_active else "❌ OFF"
    last_game_str = f"#{compteur2_last_game}" if compteur2_last_game else "Aucun"

    lines = [
        f"📊 Compteur2: {status} | B={compteur2_b}",
        f"🎮 Dernier jeu reçu: {last_game_str}",
        f"🎯 Dernière prédiction: #{last_prediction_game}" if last_prediction_game else "🎯 Dernière prédiction: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]

    for suit in ALL_SUITS:
        count = compteur2_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur2_b - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur2_b}")

    if attente_mode:
        attente_status = "🔒 Verrouillé (attend PERDU)" if attente_locked else "🔓 Prêt"
        lines.append(f"\n🕐 Mode Attente: ✅ ON | {attente_status}")
    else:
        lines.append("\n🕐 Mode Attente: ❌ OFF")

    inv_ch = f"`{CHANNEL_INVERSE_ID}`" if CHANNEL_INVERSE_ID else "Non configuré"
    lines.append(f"\n📡 Canal Compteur2 (Bot2): {inv_ch}")

    return "\n".join(lines)

async def process_compteur2(game_number: int, player_suits: List[str]):
    """Traite le Compteur2 dès que le joueur a ses cartes.

    Quand B absences consécutives d'un costume sont atteintes, envoie
    UNE prédiction (Bot2) pour le jeu suivant selon SUIT_INVERSE_C2.
    La prédiction est transférée vers CHANNEL_INVERSE_ID.
    """
    global compteur2_absences, compteur2_last_game, compteur2_last_seen, compteur2_processed_games

    if not compteur2_active:
        return

    if game_number in compteur2_processed_games:
        return

    compteur2_processed_games.add(game_number)
    if len(compteur2_processed_games) > 200:
        oldest = min(compteur2_processed_games)
        compteur2_processed_games.discard(oldest)

    compteur2_last_game = game_number

    for suit in ALL_SUITS:
        last_seen = compteur2_last_seen.get(suit, 0)

        if suit in player_suits:
            if compteur2_absences[suit] > 0:
                logger.info(f"📊 Compteur2 {suit}: trouvé #{game_number} → reset (était {compteur2_absences[suit]})")
            compteur2_absences[suit] = 0
            compteur2_last_seen[suit] = game_number
        else:
            if last_seen == 0 or game_number == last_seen + 1:
                compteur2_absences[suit] += 1
            else:
                logger.info(
                    f"📊 Compteur2 {suit}: jeu #{game_number} non-consécutif "
                    f"(précédent #{last_seen}) → compteur remis à 1"
                )
                compteur2_absences[suit] = 1

            compteur2_last_seen[suit] = game_number
            count = compteur2_absences[suit]
            logger.info(f"📊 Compteur2 {suit}: absence consécutive {count}/{compteur2_b} (jeu #{game_number})")

            if count >= compteur2_b:
                pred_game = game_number + 1

                # ── Règle 1 : Écart minimum de 2 entre prédictions ──────────────
                if last_prediction_game > 0 and pred_game < last_prediction_game + 2:
                    logger.info(
                        f"⏸ Prédiction #{pred_game} ignorée: "
                        f"écart insuffisant (dernier: #{last_prediction_game})"
                    )
                    compteur2_absences[suit] = 0
                    continue

                # ── Règle 4 : Pas de prédiction pour le même numéro deux fois ──
                if pred_game == last_prediction_game:
                    logger.info(f"⏸ Prédiction #{pred_game} ignorée: déjà prédit")
                    compteur2_absences[suit] = 0
                    continue

                sent = await send_compteur2_prediction(pred_game, suit)
                if sent:
                    compteur2_absences[suit] = 0
                else:
                    logger.info(
                        f"⏰ Compteur2 {suit}: prédiction non envoyée "
                        f"→ compteur conservé à {compteur2_absences[suit]}"
                    )

# ============================================================================
# COMPTEUR3 - Logique principale (8 absences → prédit le miroir)
# ============================================================================

def get_compteur3_status_text() -> str:
    status = "✅ ON" if compteur3_active else "❌ OFF"
    last_game_str = f"#{compteur3_last_game}" if compteur3_last_game else "Aucun"
    lines = [
        f"📊 Compteur3: {status} | B={compteur3_b}",
        f"🎮 Dernier jeu reçu: {last_game_str}",
        f"🎯 Dernière prédiction: #{last_prediction_game_c3}" if last_prediction_game_c3 else "🎯 Dernière prédiction: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = compteur3_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur3_b - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur3_b}")
    c3_ch = f"`{CHANNEL_COMPTEUR3_ID}`" if CHANNEL_COMPTEUR3_ID else "Non configuré"
    lines.append(f"\n📡 Canal Compteur3: {c3_ch}")
    return "\n".join(lines)

async def process_compteur3(game_number: int, player_suits: List[str]):
    """Traite le Compteur3 dès que le joueur a ses cartes.

    Quand B=5 absences consécutives d'un costume sont atteintes, envoie
    UNE prédiction pour le jeu suivant :
    - Miroir (inverse) du costume manquant
    """
    global compteur3_absences, compteur3_last_game, compteur3_last_seen, compteur3_processed_games

    if not compteur3_active:
        return

    if game_number in compteur3_processed_games:
        return

    compteur3_processed_games.add(game_number)
    if len(compteur3_processed_games) > 200:
        oldest = min(compteur3_processed_games)
        compteur3_processed_games.discard(oldest)

    compteur3_last_game = game_number

    for suit in ALL_SUITS:
        last_seen = compteur3_last_seen.get(suit, 0)

        if suit in player_suits:
            if compteur3_absences[suit] > 0:
                logger.info(f"📊 Compteur3 {suit}: trouvé #{game_number} → reset (était {compteur3_absences[suit]})")
            compteur3_absences[suit] = 0
            compteur3_last_seen[suit] = game_number
        else:
            if last_seen == 0 or game_number == last_seen + 1:
                compteur3_absences[suit] += 1
            else:
                logger.info(
                    f"📊 Compteur3 {suit}: jeu #{game_number} non-consécutif "
                    f"(précédent #{last_seen}) → compteur remis à 1"
                )
                compteur3_absences[suit] = 1

            compteur3_last_seen[suit] = game_number
            count = compteur3_absences[suit]
            logger.info(f"📊 Compteur3 {suit}: absence consécutive {count}/{compteur3_b} (jeu #{game_number})")

            if count >= compteur3_b:
                pred_game = game_number + 1

                # ── Règle 1 : Pas de doublon sur le même numéro ─────────────────
                if pred_game == last_prediction_game_c3:
                    logger.info(f"⏸ Compteur3 #{pred_game} ignoré: déjà prédit")
                    compteur3_absences[suit] = 0
                    continue

                sent = await send_compteur3_prediction(pred_game, suit)
                if sent:
                    compteur3_absences[suit] = 0
                else:
                    logger.info(
                        f"⏰ Compteur3 {suit}: prédiction non envoyée "
                        f"→ compteur conservé à {compteur3_absences[suit]}"
                    )

# ============================================================================
# COMPTEUR1 - Logique principale (B absences → prédit selon table C1)
# ============================================================================

def get_compteur1_status_text() -> str:
    status = "✅ ON" if compteur1_active else "❌ OFF"
    last_game_str = f"#{compteur1_last_game}" if compteur1_last_game else "Aucun"
    lines = [
        f"📊 Compteur1: {status} | B={compteur1_b}",
        f"🎮 Dernier jeu reçu: {last_game_str}",
        f"🎯 Dernière prédiction: #{last_prediction_game_c1}" if last_prediction_game_c1 else "🎯 Dernière prédiction: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = compteur1_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur1_b - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur1_b}")
    c1_ch = f"`{CHANNEL_COMPTEUR1_ID}`" if CHANNEL_COMPTEUR1_ID else "Non configuré"
    lines.append(f"\n📡 Canal Compteur1: {c1_ch}")
    return "\n".join(lines)

async def process_compteur1(game_number: int, player_suits: List[str]):
    """Traite le Compteur1 dès que le joueur a ses cartes.

    Quand B absences consécutives d'un costume sont atteintes, envoie
    UNE prédiction pour le jeu suivant selon la table SUIT_INVERSE_C1.
    """
    global compteur1_absences, compteur1_last_game, compteur1_last_seen, compteur1_processed_games

    if not compteur1_active:
        return

    if game_number in compteur1_processed_games:
        return

    compteur1_processed_games.add(game_number)
    if len(compteur1_processed_games) > 200:
        oldest = min(compteur1_processed_games)
        compteur1_processed_games.discard(oldest)

    compteur1_last_game = game_number

    for suit in ALL_SUITS:
        last_seen = compteur1_last_seen.get(suit, 0)

        if suit in player_suits:
            if compteur1_absences[suit] > 0:
                logger.info(f"📊 Compteur1 {suit}: trouvé #{game_number} → reset (était {compteur1_absences[suit]})")
            compteur1_absences[suit] = 0
            compteur1_last_seen[suit] = game_number
        else:
            if last_seen == 0 or game_number == last_seen + 1:
                compteur1_absences[suit] += 1
            else:
                logger.info(
                    f"📊 Compteur1 {suit}: jeu #{game_number} non-consécutif "
                    f"(précédent #{last_seen}) → compteur remis à 1"
                )
                compteur1_absences[suit] = 1

            compteur1_last_seen[suit] = game_number
            count = compteur1_absences[suit]
            logger.info(f"📊 Compteur1 {suit}: absence consécutive {count}/{compteur1_b} (jeu #{game_number})")

            if count >= compteur1_b:
                pred_game = game_number + 1

                # ── Règle 1 : Pas de doublon sur le même numéro ─────────────────
                if pred_game == last_prediction_game_c1:
                    logger.info(f"⏸ Compteur1 #{pred_game} ignoré: déjà prédit")
                    compteur1_absences[suit] = 0
                    continue

                sent = await send_compteur1_prediction(pred_game, suit)
                if sent:
                    compteur1_absences[suit] = 0
                else:
                    logger.info(
                        f"⏰ Compteur1 {suit}: prédiction non envoyée "
                        f"→ compteur conservé à {compteur1_absences[suit]}"
                    )

# ============================================================================
# BOUCLE DE POLLING API - DYNAMIQUE
# ============================================================================

async def api_polling_loop():
    global current_game_number, api_results_cache, player_processed_games
    global reset_done_for_cycle

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API dynamique démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number = result["game_number"]
                    is_finished = result["is_finished"]
                    player_cards = result.get("player_cards", [])

                    api_results_cache[game_number] = result

                    player_suits = player_suits_from_cards(player_cards)
                    ready = len(player_cards) >= 2

                    if not ready:
                        continue

                    current_game_number = game_number

                    p_display = " ".join(SUIT_DISPLAY.get(s, s) for s in player_suits) or "—"

                    # ── 0. VÉRIFICATION des prédictions en attente ─────────────
                    if player_suits:
                        await check_prediction_result_dynamic(game_number, player_suits, is_finished)

                    # ── 1. COMPTEUR2 ───────────────────────────────────────────
                    if game_number not in player_processed_games and ready:
                        player_processed_games.add(game_number)
                        if len(player_processed_games) > 500:
                            oldest = min(player_processed_games)
                            player_processed_games.discard(oldest)

                        logger.info(
                            f"🃏 Jeu #{game_number} | Joueur: {p_display} "
                            f"| Terminé: {is_finished}"
                        )
                        await process_compteur2(game_number, player_suits)
                        await process_compteur3(game_number, player_suits)
                        await process_compteur1(game_number, player_suits)

                    # ── 3. RESET AUTOMATIQUE sur la partie #1440 ────────────────
                    if game_number == 1440 and is_finished and not reset_done_for_cycle:
                        reset_done_for_cycle = True
                        logger.info("🔄 Reset automatique: partie #1440 terminée")
                        await perform_full_reset("Reset automatique (partie #1440 terminée)")

                    if game_number < 100 and reset_done_for_cycle:
                        reset_done_for_cycle = False
                        logger.info("🔄 Nouveau cycle détecté → flag reset remis à zéro")

                if len(api_results_cache) > 300:
                    oldest = min(api_results_cache.keys())
                    del api_results_cache[oldest]

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET COMPLET
# ============================================================================

async def perform_full_reset(reason: str):
    global pending_inverse, pending_manque, pending_compteur3, pending_compteur1, last_prediction_time
    global compteur2_absences, compteur2_last_game, attente_locked
    global compteur2_last_seen, compteur2_processed_games
    global compteur3_absences, compteur3_last_game
    global compteur3_last_seen, compteur3_processed_games, last_prediction_game_c3
    global compteur1_absences, compteur1_last_game
    global compteur1_last_seen, compteur1_processed_games, last_prediction_game_c1
    global player_processed_games, api_results_cache
    global last_prediction_game, reset_done_for_cycle

    stats = len(pending_inverse) + len(pending_manque) + len(pending_compteur3) + len(pending_compteur1)
    pending_inverse.clear()
    pending_manque.clear()
    pending_compteur3.clear()
    pending_compteur1.clear()
    last_prediction_time = None
    last_prediction_game = 0
    last_prediction_game_c3 = 0
    last_prediction_game_c1 = 0
    compteur2_absences = {suit: 0 for suit in ALL_SUITS}
    compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
    compteur2_processed_games = set()
    compteur2_last_game = 0
    compteur3_absences = {suit: 0 for suit in ALL_SUITS}
    compteur3_last_seen = {suit: 0 for suit in ALL_SUITS}
    compteur3_processed_games = set()
    compteur3_last_game = 0
    compteur1_absences = {suit: 0 for suit in ALL_SUITS}
    compteur1_last_seen = {suit: 0 for suit in ALL_SUITS}
    compteur1_processed_games = set()
    compteur1_last_game = 0
    attente_locked = False
    player_processed_games = set()
    api_results_cache = {}

    logger.info(f"🔄 {reason} - {stats} prédictions effacées")

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"🔄 **RESET SYSTÈME**\n\n{reason}\n\n"
                f"✅ Compteurs remis à zéro\n"
                f"✅ {stats} prédictions effacées\n\n"
                f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨"
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur2(event):
    global compteur2_active, compteur2_b, compteur2_absences, compteur2_last_game
    global compteur2_last_seen, compteur2_processed_games, player_processed_games

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur2_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur2_active = True
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur2_processed_games = set()
        player_processed_games = set()
        await event.respond(
            f"✅ Compteur2 ACTIVÉ | B={compteur2_b}\n\n" + get_compteur2_status_text()
        )

    elif arg == 'off':
        compteur2_active = False
        await event.respond("❌ Compteur2 DÉSACTIVÉ")

    elif arg == 'reset':
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur2_processed_games = set()
        player_processed_games = set()
        compteur2_last_game = 0
        await event.respond("🔄 Compteur2 remis à zéro\n\n" + get_compteur2_status_text())

    elif arg == 'b':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 b <valeur>` (ex: `/compteur2 b 4`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ B doit être entre 1 et 20")
                return
            old_b = compteur2_b
            compteur2_b = val
            compteur2_absences = {suit: 0 for suit in ALL_SUITS}
            compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
            compteur2_processed_games = set()
            player_processed_games = set()
            await event.respond(
                f"✅ Compteur2 B: {old_b} → {compteur2_b} | Compteurs remis à zéro\n\n"
                + get_compteur2_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur2 b 4`")
    else:
        await event.respond(
            "📊 **COMPTEUR2 - Aide**\n\n"
            "`/compteur2` — Afficher l'état\n"
            "`/compteur2 on` — Activer\n"
            "`/compteur2 off` — Désactiver\n"
            "`/compteur2 b <val>` — Changer le seuil B\n"
            "`/compteur2 reset` — Remettre les compteurs à zéro"
        )

async def cmd_compteur3(event):
    global compteur3_active, compteur3_b, compteur3_absences, compteur3_last_game
    global compteur3_last_seen, compteur3_processed_games

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur3_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur3_active = True
        compteur3_absences = {suit: 0 for suit in ALL_SUITS}
        compteur3_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur3_processed_games = set()
        await event.respond(
            f"✅ Compteur3 ACTIVÉ | B={compteur3_b}\n\n" + get_compteur3_status_text()
        )

    elif arg == 'off':
        compteur3_active = False
        await event.respond("❌ Compteur3 DÉSACTIVÉ")

    elif arg == 'reset':
        compteur3_absences = {suit: 0 for suit in ALL_SUITS}
        compteur3_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur3_processed_games = set()
        compteur3_last_game = 0
        await event.respond("🔄 Compteur3 remis à zéro\n\n" + get_compteur3_status_text())

    elif arg == 'b':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur3 b <valeur>` (ex: `/compteur3 b 8`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 30:
                await event.respond("❌ B doit être entre 1 et 30")
                return
            old_b = compteur3_b
            compteur3_b = val
            compteur3_absences = {suit: 0 for suit in ALL_SUITS}
            compteur3_last_seen = {suit: 0 for suit in ALL_SUITS}
            compteur3_processed_games = set()
            await event.respond(
                f"✅ Compteur3 B: {old_b} → {compteur3_b} | Compteurs remis à zéro\n\n"
                + get_compteur3_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur3 b 8`")
    else:
        await event.respond(
            "📊 **COMPTEUR3 - Aide**\n\n"
            "`/compteur3` — Afficher l'état\n"
            "`/compteur3 on` — Activer\n"
            "`/compteur3 off` — Désactiver\n"
            "`/compteur3 b <val>` — Changer le seuil B\n"
            "`/compteur3 reset` — Remettre les compteurs à zéro"
        )

async def cmd_compteur1(event):
    global compteur1_active, compteur1_b, compteur1_absences, compteur1_last_game
    global compteur1_last_seen, compteur1_processed_games

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur1_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur1_active = True
        compteur1_absences = {suit: 0 for suit in ALL_SUITS}
        compteur1_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur1_processed_games = set()
        await event.respond(
            f"✅ Compteur1 ACTIVÉ | B={compteur1_b}\n\n" + get_compteur1_status_text()
        )

    elif arg == 'off':
        compteur1_active = False
        await event.respond("❌ Compteur1 DÉSACTIVÉ")

    elif arg == 'reset':
        compteur1_absences = {suit: 0 for suit in ALL_SUITS}
        compteur1_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur1_processed_games = set()
        compteur1_last_game = 0
        await event.respond("🔄 Compteur1 remis à zéro\n\n" + get_compteur1_status_text())

    elif arg == 'b':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur1 b <valeur>` (ex: `/compteur1 b 8`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 30:
                await event.respond("❌ B doit être entre 1 et 30")
                return
            old_b = compteur1_b
            compteur1_b = val
            compteur1_absences = {suit: 0 for suit in ALL_SUITS}
            compteur1_last_seen = {suit: 0 for suit in ALL_SUITS}
            compteur1_processed_games = set()
            await event.respond(
                f"✅ Compteur1 B: {old_b} → {compteur1_b} | Compteurs remis à zéro\n\n"
                + get_compteur1_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur1 b 8`")
    else:
        await event.respond(
            "📊 **COMPTEUR1 - Aide**\n\n"
            "`/compteur1` — Afficher l'état\n"
            "`/compteur1 on` — Activer\n"
            "`/compteur1 off` — Désactiver\n"
            "`/compteur1 b <val>` — Changer le seuil B\n"
            "`/compteur1 reset` — Remettre les compteurs à zéro"
        )

async def cmd_attente(event):
    global attente_mode, attente_locked

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        mode_str = "✅ ON" if attente_mode else "❌ OFF"
        lock_str = "🔒 Verrouillé (attend PERDU)" if (attente_mode and attente_locked) else "🔓 Prêt"
        await event.respond(
            f"🕐 **MODE ATTENTE**\n\n"
            f"Statut: {mode_str}\n"
            f"État: {lock_str}\n\n"
            f"`/attente on` — Activer\n"
            f"`/attente off` — Désactiver\n"
            f"`/attente reset` — Déverrouiller manuellement"
        )
        return

    arg = parts[1].lower()

    if arg == 'on':
        attente_mode = True
        attente_locked = False
        await event.respond("✅ **Mode Attente ACTIVÉ**\n\nÉtat actuel: 🔓 Prêt.")
    elif arg == 'off':
        attente_mode = False
        attente_locked = False
        await event.respond("❌ **Mode Attente DÉSACTIVÉ**")
    elif arg == 'reset':
        attente_locked = False
        status = "✅ ON" if attente_mode else "❌ OFF"
        await event.respond(
            f"🔓 **Mode Attente déverrouillé manuellement**\n\nMode Attente: {status}"
        )
    else:
        await event.respond(
            "🕐 **MODE ATTENTE - Aide**\n\n"
            "`/attente on/off` — Activer/désactiver\n"
            "`/attente reset` — Déverrouiller manuellement"
        )

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = [
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]

    for i, pred in enumerate(prediction_history[:20], 1):
        pred_game = pred['predicted_game']
        s_inv = SUIT_DISPLAY.get(pred.get('suit_inverse', ''), pred.get('suit_inverse', '?'))
        s_man = SUIT_DISPLAY.get(pred.get('suit_manque', ''), pred.get('suit_manque', '?'))
        trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
        time_str = pred['predicted_at'].strftime('%H:%M:%S')
        silent_tag = " [Attente]" if pred.get('silent') else ""

        def status_str(s):
            if s == 'en_cours': return "⏳ En cours..."
            if s == 'gagne': return "✅ GAGNÉ"
            if s == 'perdu': return "❌ PERDU"
            return f"❓ {s}"

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #{pred_game}**{silent_tag}\n"
            f"   📉 Déclenché par: {trig} absent {compteur2_b}x\n"
            f"   🔁 Dogon 2 (Inverse) {s_inv}: {status_str(pred.get('status_inverse','?'))}\n"
            f"   🔁 Dogon 1 (Manque) {s_man}: {status_str(pred.get('status_manque','?'))}"
        )
        lines.append("")

    if pending_inverse or pending_manque:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        all_keys = set(pending_inverse.keys()) | set(pending_manque.keys())
        for num in sorted(all_keys):
            if num in pending_inverse:
                pred = pending_inverse[num]
                suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
                ar = pred.get('awaiting_rattrapage', 0)
                st = f"R{ar} (#{num+ar})" if ar > 0 else "Vérif. directe"
                lines.append(f"• Game #{num} Dogon2 {suit}: {st}")
            if num in pending_manque:
                pred = pending_manque[num]
                suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
                ar = pred.get('awaiting_rattrapage', 0)
                st = f"R{ar} (#{num+ar})" if ar > 0 else "Vérif. directe"
                lines.append(f"• Game #{num} Dogon1 {suit}: {st}")
        lines.append("")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    async def check(channel_id):
        if not channel_id:
            return "❌", "Non configuré"
        try:
            entity = await resolve_channel(channel_id)
            if entity:
                return "✅", getattr(entity, 'title', 'Sans titre')
        except Exception:
            pass
        return "❌", "Inaccessible"

    pred_status, pred_name = await check(PREDICTION_CHANNEL_ID)
    inv_status,  inv_name  = await check(CHANNEL_INVERSE_ID)
    c3_status,   c3_name   = await check(CHANNEL_COMPTEUR3_ID)
    c1_status,   c1_name   = await check(CHANNEL_COMPTEUR1_ID)

    await event.respond(
        f"📡 **CONFIGURATION**\n\n"
        f"**Source:** API 1xBet (polling {API_POLL_INTERVAL}s)\n"
        f"**Jeux en cache:** {len(api_results_cache)}\n"
        f"**Jeux traités:** {len(player_processed_games)}\n\n"
        f"**Canal Principal:**\n"
        f"ID: `{PREDICTION_CHANNEL_ID}` | {pred_status} {pred_name}\n\n"
        f"**Bot 1 — Compteur1 (Manque B={compteur1_b}):**\n"
        f"ID: `{CHANNEL_COMPTEUR1_ID or 'Non défini'}` | {c1_status} {c1_name}\n\n"
        f"**Bot 2 — Compteur2 (Dogon 2 B={compteur2_b}):**\n"
        f"ID: `{CHANNEL_INVERSE_ID or 'Non défini'}` | {inv_status} {inv_name}\n\n"
        f"**Bot 3 — Compteur3 (Miroir B={compteur3_b}):**\n"
        f"ID: `{CHANNEL_COMPTEUR3_ID or 'Non défini'}` | {c3_status} {c3_name}\n\n"
        f"**Paramètres:**\n"
        f"Compteur2 B={compteur2_b} | Actif: {'✅' if compteur2_active else '❌'}\n"
        f"Compteur3 B={compteur3_b} | Actif: {'✅' if compteur3_active else '❌'}\n"
        f"Compteur1 B={compteur1_b} | Actif: {'✅' if compteur1_active else '❌'}\n"
        f"Rattrapage max: {MAX_RATTRAPAGE}\n"
        f"Mode Attente: {'✅ ON' if attente_mode else '❌ OFF'}\n"
        f"Admin ID: `{ADMIN_ID}`"
    )

async def get_bot_channels():
    """Retourne la liste des canaux où le bot est présent (via get_dialogs)."""
    channels = []
    try:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            entity_type = type(entity).__name__
            if entity_type in ('Channel', 'Chat'):
                channel_id = getattr(entity, 'id', None)
                title = getattr(entity, 'title', 'Sans titre')
                if channel_id:
                    # Normaliser en format -100...
                    full_id = int(f"-100{channel_id}") if channel_id > 0 else channel_id
                    channels.append({'id': full_id, 'title': title})
    except Exception as e:
        logger.error(f"❌ Erreur get_dialogs: {e}")
    return channels

async def cmd_canal(event):
    """Configurer les canaux de redirection via liste automatique.

    Commandes:
      /canal                     — Lister les canaux disponibles + config actuelle
      /canal compteur1 <N>       — Canal N pour Bot 1 (Manque B=8)
      /canal inverse <N>         — Canal N pour Bot 2 (Dogon 2 B=5)
      /canal compteur3 <N>       — Canal N pour Bot 3 (Miroir B=5)
      /canal compteur1 off / inverse off / compteur3 off — Retirer
    """
    global CHANNEL_INVERSE_ID, CHANNEL_COMPTEUR3_ID, CHANNEL_COMPTEUR1_ID

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    def config_lines():
        c1_label  = f"✅ `{CHANNEL_COMPTEUR1_ID}`" if CHANNEL_COMPTEUR1_ID else "❌ Non défini"
        inv_label = f"✅ `{CHANNEL_INVERSE_ID}`"   if CHANNEL_INVERSE_ID   else "❌ Non défini"
        c3_label  = f"✅ `{CHANNEL_COMPTEUR3_ID}`" if CHANNEL_COMPTEUR3_ID else "❌ Non défini"
        return (
            f"🔁 **Bot 1** (Manque B=8)   → {c1_label}\n"
            f"🔁 **Bot 2** (Dogon 2 B=5)  → {inv_label}\n"
            f"🔁 **Bot 3** (Miroir B=5)   → {c3_label}"
        )

    # ── /canal seul : afficher la liste des canaux disponibles ────────────
    if len(parts) == 1:
        await event.respond("🔍 Récupération des canaux disponibles...")
        channels = await get_bot_channels()

        lines = ["📡 **CANAUX DE REDIRECTION**\n", config_lines(), ""]

        if channels:
            lines.append("📋 **Canaux où le bot est présent :**")
            for i, ch in enumerate(channels, 1):
                lines.append(f"  {i}. {ch['title']}  (`{ch['id']}`)")
            lines.append("")
            lines.append("**Choisir par numéro :**")
            lines.append("`/canal compteur1 <N>` — Bot 1 (Manque) = canal N")
            lines.append("`/canal inverse <N>`   — Bot 2 (Dogon 2) = canal N")
            lines.append("`/canal compteur3 <N>` — Bot 3 (Miroir) = canal N")
            lines.append("`/canal <type> off` — Retirer (compteur1/inverse/compteur3)")
        else:
            lines.append("⚠️ Aucun canal trouvé.")
            lines.append("Ajoutez le bot comme administrateur dans vos canaux,")
            lines.append("puis retapez `/canal`.")

        await event.respond("\n".join(lines))
        return

    if len(parts) < 3:
        await event.respond(
            "❌ Usage invalide.\n\n"
            "`/canal` — Voir la liste\n"
            "`/canal compteur1 <N>` — Bot 1 (Manque)\n"
            "`/canal inverse <N>` — Bot 2 (Dogon 2)\n"
            "`/canal compteur3 <N>` — Bot 3 (Miroir)\n"
            "`/canal <type> off` — Retirer"
        )
        return

    direction = parts[1].lower()
    value = parts[2].lower()

    if direction not in ('inverse', 'compteur3', 'compteur1'):
        await event.respond("❌ Type invalide. Utilisez `compteur1`, `inverse` ou `compteur3`.")
        return

    label_map = {
        'compteur1': "Bot 1 (Manque B=8)",
        'inverse':   "Bot 2 (Dogon 2 B=5)",
        'compteur3': "Bot 3 (Miroir B=5)",
    }
    label = label_map[direction]

    # ── Suppression du canal ───────────────────────────────────────────────
    if value == 'off':
        if direction == 'inverse':
            CHANNEL_INVERSE_ID = 0
        elif direction == 'compteur3':
            CHANNEL_COMPTEUR3_ID = 0
        else:
            CHANNEL_COMPTEUR1_ID = 0
        await event.respond(f"✅ Canal **{label}** retiré.\n\n" + config_lines())
        return

    # ── Sélection par numéro ───────────────────────────────────────────────
    try:
        n = int(parts[2])
    except ValueError:
        await event.respond(
            "❌ Indiquez le **numéro** du canal dans la liste.\n\n"
            "Tapez `/canal` pour voir la liste numérotée."
        )
        return

    await event.respond("🔍 Récupération de la liste des canaux...")
    channels = await get_bot_channels()

    if not channels:
        await event.respond(
            "⚠️ Aucun canal trouvé.\n\n"
            "Ajoutez le bot comme administrateur dans vos canaux puis retapez `/canal`."
        )
        return

    if n < 1 or n > len(channels):
        await event.respond(
            f"❌ Numéro invalide. Choisissez entre 1 et {len(channels)}.\n\n"
            "Tapez `/canal` pour voir la liste."
        )
        return

    chosen = channels[n - 1]
    channel_id = chosen['id']
    canal_name = chosen['title']

    if direction == 'inverse':
        CHANNEL_INVERSE_ID = channel_id
    elif direction == 'compteur3':
        CHANNEL_COMPTEUR3_ID = channel_id
    else:
        CHANNEL_COMPTEUR1_ID = channel_id

    await event.respond(
        f"✅ **{label}** configuré !\n\n"
        f"📛 Canal : **{canal_name}**\n"
        f"🆔 ID    : `{channel_id}`\n\n"
        + config_lines()
    )

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion aux canaux...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal principal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return

        test_inv = build_prediction_msg_inverse(9999, "♦")

        sent_inv = await client.send_message(prediction_entity, f"{test_inv}\n[TEST]")

        await asyncio.sleep(2)

        await client.edit_message(
            prediction_entity, sent_inv.id,
            build_result_msg_inverse(9999, "♦", "✅0️⃣") + "\n[TEST]"
        )

        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent_inv.id])

        # Test canaux de redirection
        if CHANNEL_COMPTEUR1_ID and CHANNEL_COMPTEUR1_ID != PREDICTION_CHANNEL_ID:
            await send_to_redirect_channel(
                CHANNEL_COMPTEUR1_ID,
                build_redirect_msg_compteur1(9999, "♣") + "\n[TEST]"
            )
        if CHANNEL_INVERSE_ID and CHANNEL_INVERSE_ID != PREDICTION_CHANNEL_ID:
            await send_to_redirect_channel(
                CHANNEL_INVERSE_ID,
                build_redirect_msg(9999, "♦") + "\n[TEST]"
            )
        if CHANNEL_COMPTEUR3_ID and CHANNEL_COMPTEUR3_ID != PREDICTION_CHANNEL_ID:
            await send_to_redirect_channel(
                CHANNEL_COMPTEUR3_ID,
                build_redirect_msg_compteur3(9999, "♠") + "\n[TEST]"
            )

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal principal: `{pred_name_display}`\n"
            f"Envoi OK\n\n"
            f"Bot 1 redirect: `{CHANNEL_COMPTEUR1_ID if CHANNEL_COMPTEUR1_ID else 'Non configuré'}`\n"
            f"Bot 2 redirect: `{CHANNEL_INVERSE_ID if CHANNEL_INVERSE_ID else 'Non configuré'}`\n"
            f"Bot 3 redirect: `{CHANNEL_COMPTEUR3_ID if CHANNEL_COMPTEUR3_ID else 'Non configuré'}`"
        )

    except ChatWriteForbiddenError:
        await event.respond("❌ **Permission refusée** — Ajoutez le bot comme administrateur.")
    except Exception as e:
        await event.respond(f"❌ Échec du test: {e}")

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_compteur2_status_text(),
        "",
        f"🔮 Prédictions actives: Dogon2={len(pending_inverse)} | Dogon1={len(pending_manque)}",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
        f"📦 Jeux en cache: {len(api_results_cache)}",
        "🔄 Reset automatique: partie #1440 terminée",
        f"🎯 Rattrapage max: {MAX_RATTRAPAGE}",
    ]

    if pending_inverse or pending_manque:
        lines.append("")
        all_keys = set(pending_inverse.keys()) | set(pending_manque.keys())
        for num in sorted(all_keys):
            if num in pending_inverse:
                pred = pending_inverse[num]
                suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
                ar = pred.get('awaiting_rattrapage', 0)
                st = f"R{ar} (#{num+ar})" if ar > 0 else "Vérif. directe"
                lines.append(f"• Game #{num} Dogon2 {suit}: {st}")
            if num in pending_manque:
                pred = pending_manque[num]
                suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
                ar = pred.get('awaiting_rattrapage', 0)
                st = f"R{ar} (#{num+ar})" if ar > 0 else "Vérif. directe"
                lines.append(f"• Game #{num} Dogon1 {suit}: {st}")

    await event.respond("\n".join(lines))

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return

        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐎 ✨"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_predi(event):
    """Gestion des intervalles horaires de prédiction (heure du Bénin = UTC+1)."""
    global prediction_intervals, intervals_enabled

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    raw = event.message.message.strip()

    add_match = re.match(r'^/predi\+(\d{1,2})-(\d{1,2})$', raw)
    if add_match:
        start_h = int(add_match.group(1))
        end_h = int(add_match.group(2))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            await event.respond("❌ Heures invalides (0-23).")
            return
        if start_h == end_h:
            await event.respond("❌ Début et fin identiques.")
            return
        for iv in prediction_intervals:
            if iv["start"] == start_h and iv["end"] == end_h:
                await event.respond(f"⚠️ L'intervalle {start_h:02d}h→{end_h:02d}h existe déjà.")
                return
        prediction_intervals.append({"start": start_h, "end": end_h})
        await event.respond(
            f"✅ Intervalle ajouté: {start_h:02d}h → {end_h:02d}h (heure Bénin)\n\n"
            + get_intervals_status_text()
        )
        return

    parts = raw.split()

    if len(parts) == 1:
        await event.respond(
            get_intervals_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/predi+HH-HH` — Ajouter un intervalle\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction\n"
            "`/predi off` — Désactiver la restriction"
        )
        return

    arg = parts[1].lower()

    if arg == "on":
        intervals_enabled = True
        await event.respond("✅ **Restriction horaire ACTIVÉE**\n\n" + get_intervals_status_text())
    elif arg == "off":
        intervals_enabled = False
        await event.respond("❌ **Restriction horaire DÉSACTIVÉE**\n\n" + get_intervals_status_text())
    elif arg == "clear":
        prediction_intervals = []
        await event.respond("🗑️ Tous les intervalles supprimés.\n\n" + get_intervals_status_text())
    elif arg == "del":
        if len(parts) < 3:
            await event.respond("Usage: `/predi del <N>`")
            return
        try:
            idx = int(parts[2]) - 1
            if not (0 <= idx < len(prediction_intervals)):
                await event.respond(f"❌ Index invalide. {len(prediction_intervals)} intervalle(s).")
                return
            removed = prediction_intervals.pop(idx)
            await event.respond(
                f"🗑️ Intervalle {removed['start']:02d}h→{removed['end']:02d}h supprimé.\n\n"
                + get_intervals_status_text()
            )
        except ValueError:
            await event.respond("❌ Numéro invalide.")
    else:
        await event.respond(
            "⏰ **INTERVALLES - Aide**\n\n"
            "`/predi` — Afficher l'état\n"
            "`/predi+HH-HH` — Ajouter un intervalle (ex: `/predi+12-15`)\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction horaire\n"
            "`/predi off` — Désactiver la restriction horaire\n\n"
            "Toutes les heures sont en heure du Bénin (UTC+1)"
        )

async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "🎰 **Bienvenue sur BACCARAT PRO !**\n\n"
        "✨ **Bot de Prédiction Baccarat — Fiable & Précis**\n\n"
        "Ce bot analyse les absences consécutives de costumes\n"
        "et génère automatiquement deux signaux :\n\n"
        "🔁 **Dogon 2** — prédit l'inverse du costume manquant\n"
        "🔁 **Dogon 1** — prédit le costume manquant lui-même\n\n"
        "Chaque signal est posté dans le canal principal,\n"
        "puis redirigé vers son canal dédié.\n\n"
        "⚡ Vérification dynamique (rattrapage jusqu'à +2)\n"
        "⏰ Gestion des intervalles horaires (heure Bénin)\n"
        "🔄 Reset automatique à la partie #1440\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Tapez /help pour voir toutes les commandes.\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PRO - AIDE**\n\n"
        "**🎮 Compteur1 (B=8) :**\n"
        "• ❤️→♣️ | ♠️→♦️ | ♦️→♠️ | ♣️→❤️  (Bot1)\n"
        "• Redirigé vers Canal Compteur1\n\n"
        "**🎮 Compteur2 (B=5) :**\n"
        "• ♣️→♦️ | ♠️→❤️ | ♦️→♣️ | ❤️→♠️  (Bot2)\n"
        "• Redirigé vers Canal Compteur2\n\n"
        "**🎮 Compteur3 (B=5) :**\n"
        "• ♠️→♣️ | ♣️→♠️ | ❤️→♦️ | ♦️→❤️  (Bot3)\n"
        "• Redirigé vers Canal Compteur3\n\n"
        "**🔍 Vérification (rattrapage max +2) :**\n"
        "• Costume trouvé → ✅0️⃣  |  Rattrapage → ✅1️⃣ / ✅2️⃣  |  Raté → ❌\n\n"
        "**🔧 Commandes Admin :**\n"
        "`/compteur1` — État Compteur1\n"
        "`/compteur1 on/off/b <val>/reset`\n"
        "`/compteur2` — État Compteur2\n"
        "`/compteur2 on/off/b <val>/reset`\n"
        "`/compteur3` — État Compteur3\n"
        "`/compteur3 on/off/b <val>/reset`\n"
        "`/canal` — Voir et configurer les 4 canaux\n"
        "`/canal inverse <N>` / `/canal manque <N>`\n"
        "`/canal compteur3 <N>` / `/canal compteur1 <N>`\n"
        "`/attente on/off/reset` — Mode Attente\n"
        "`/predi` — Intervalles horaires\n"
        "`/status` — État complet\n"
        "`/channels` — État connexion canaux\n"
        "`/history` — Historique\n"
        "`/test` — Tester les canaux\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Annonce\n"
        "`/strategie` — Documentation des 3 stratégies\n"
        "`/help` — Cette aide"
    )

# ============================================================================
# DOCUMENTATION DES STRATÉGIES
# ============================================================================

async def cmd_strategie(event):
    """Envoie la documentation complète des 3 stratégies, avec le nom réel de chaque canal."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    async def get_channel_name(channel_id: int) -> str:
        try:
            entity = await client.get_entity(channel_id)
            return getattr(entity, 'title', str(channel_id))
        except Exception:
            return str(channel_id)

    name_bot1 = await get_channel_name(PREDICTION_CHANNEL_ID)
    name_bot2 = await get_channel_name(CHANNEL_COMPTEUR1_ID)
    name_bot3 = await get_channel_name(CHANNEL_INVERSE_ID)

    doc = (
        "📖 **DOCUMENTATION DES STRATÉGIES**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "**Principe commun aux 3 canaux**\n"
        "Le bot analyse les cartes du joueur à chaque partie de Baccarat. "
        "Il compte combien de fois de suite chaque couleur (♠️ ♥️ ♦️ ♣️) est ABSENTE. "
        "Quand ce compteur dépasse le seuil **B**, une prédiction est envoyée. "
        "Le bot vérifie ensuite si la carte prédite apparaît dans les 2 parties suivantes "
        "(rattrapage R1 et R2).\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 **Canal : {name_bot1}**\n"
        f"📌 Stratégie : Miroir — Seuil B=5\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Dans ce canal, le bot prédit la couleur **symétrique** (miroir) "
        f"de la couleur absente depuis 5 parties consécutives.\n\n"
        f"❤️ absent 5x → prédit **♦️**\n"
        f"♦️ absent 5x → prédit **❤️**\n"
        f"♠️ absent 5x → prédit **♣️**\n"
        f"♣️ absent 5x → prédit **♠️**\n\n"
        f"**Exemple :** ❤️ n'apparaît pas pendant 5 parties → le canal **{name_bot1}** "
        f"reçoit une prédiction ♦️.\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔵 **Canal : {name_bot2}**\n"
        f"📌 Stratégie : Manque — Seuil B=8\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Dans ce canal, le bot prédit la couleur **croisée opposée** "
        f"de la couleur absente depuis 8 parties consécutives.\n\n"
        f"❤️ absent 8x → prédit **♣️**\n"
        f"♣️ absent 8x → prédit **❤️**\n"
        f"♠️ absent 8x → prédit **♦️**\n"
        f"♦️ absent 8x → prédit **♠️**\n\n"
        f"**Exemple :** ♠️ n'apparaît pas pendant 8 parties → le canal **{name_bot2}** "
        f"reçoit une prédiction ♦️.\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 **Canal : {name_bot3}**\n"
        f"📌 Stratégie : Dogon 2 — Seuil B=5\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Dans ce canal, le bot prédit la couleur selon la **table Dogon 2** "
        f"dès que la couleur est absente depuis 5 parties consécutives.\n\n"
        f"♣️ absent 5x → prédit **♦️**\n"
        f"♦️ absent 5x → prédit **♣️**\n"
        f"♠️ absent 5x → prédit **❤️**\n"
        f"❤️ absent 5x → prédit **♠️**\n\n"
        f"**Exemple :** ♣️ n'apparaît pas pendant 5 parties → le canal **{name_bot3}** "
        f"reçoit une prédiction ♦️.\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔄 **Système de Rattrapage (commun aux 3 canaux)**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Si la carte prédite n'apparaît pas à la partie visée :\n"
        "• **R1** — 2ᵉ chance sur la partie suivante\n"
        "• **R2** — 3ᵉ et dernière chance\n"
        "• Au-delà → ❌ prédiction perdue\n\n"
        "✅0️⃣ Gagné dès la 1ʳᵉ partie\n"
        "✅1️⃣ Gagné au rattrapage R1\n"
        "✅2️⃣ Gagné au rattrapage R2\n"
        "❌ Perdu après 3 essais"
    )

    await event.respond(doc)

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur1, events.NewMessage(pattern=r'^/compteur1'))
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_compteur3, events.NewMessage(pattern=r'^/compteur3'))
    client.add_event_handler(cmd_attente, events.NewMessage(pattern=r'^/attente'))
    client.add_event_handler(cmd_predi, events.NewMessage(pattern=r'^/predi'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r'^/start$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_canal, events.NewMessage(pattern=r'^/canal'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))
    client.add_event_handler(cmd_strategie, events.NewMessage(pattern=r'^/strategie$'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal principal OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal principal inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        if CHANNEL_INVERSE_ID:
            try:
                inv_entity = await resolve_channel(CHANNEL_INVERSE_ID)
                if inv_entity:
                    logger.info(f"✅ Canal Dogon 2 OK: {getattr(inv_entity, 'title', 'Unknown')}")
                else:
                    logger.warning(f"⚠️ Canal Dogon 2 inaccessible: {CHANNEL_INVERSE_ID}")
            except Exception as e:
                logger.warning(f"⚠️ Canal Dogon 2: {e}")

        if CHANNEL_COMPTEUR3_ID:
            try:
                c3_entity = await resolve_channel(CHANNEL_COMPTEUR3_ID)
                if c3_entity:
                    logger.info(f"✅ Canal Compteur3 OK: {getattr(c3_entity, 'title', 'Unknown')}")
                else:
                    logger.warning(f"⚠️ Canal Compteur3 inaccessible: {CHANNEL_COMPTEUR3_ID}")
            except Exception as e:
                logger.warning(f"⚠️ Canal Compteur3: {e}")

        if CHANNEL_COMPTEUR1_ID:
            try:
                c1_entity = await resolve_channel(CHANNEL_COMPTEUR1_ID)
                if c1_entity:
                    logger.info(f"✅ Canal Compteur1 OK: {getattr(c1_entity, 'title', 'Unknown')}")
                else:
                    logger.warning(f"⚠️ Canal Compteur1 inaccessible: {CHANNEL_COMPTEUR1_ID}")
            except Exception as e:
                logger.warning(f"⚠️ Canal Compteur1: {e}")

        logger.info(
            f"🤖 Bot démarré | C1 B={compteur1_b} | C2 B={compteur2_b} | C3 B={compteur3_b} | "
            f"Rattrapage max={MAX_RATTRAPAGE} | Attente={'ON' if attente_mode else 'OFF'}"
        )
        logger.info("🔄 Reset automatique configuré: fin de la partie #1440")
        return True

    except FloodWaitError as e:
        logger.warning(f"⏳ Telegram FloodWait: attente de {e.seconds}s avant reconnexion...")
        await asyncio.sleep(e.seconds + 5)
        return None  # Signal pour réessayer
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    # ── 1. Serveur web démarré EN PREMIER (port ouvert avant tout) ─────────
    app = web.Application()
    app.router.add_get('/health', lambda r: web.Response(text="OK"))
    app.router.add_get('/', lambda r: web.Response(text="BACCARAT PRO ✨ Running"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Serveur web démarré sur port {PORT}")

    # ── 2. Connexion Telegram avec retry automatique sur FloodWait ─────────
    while True:
        result = await start_bot()
        if result is None:
            logger.info("🔄 Nouvelle tentative de connexion Telegram...")
            continue
        if not result:
            logger.error("❌ Impossible de démarrer le bot — arrêt")
            return
        break

    try:
        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Polling API dynamique démarré")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
