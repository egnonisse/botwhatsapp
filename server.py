"""Serveur FastAPI — Webhook WhatsApp + API Dashboard.

Reçoit les messages WhatsApp, les traite via l'agent de vente,
et expose une API pour le dashboard de supervision.
"""

import json
import re
import asyncio
import yaml
from pathlib import Path
from typing import Optional
import time

# Remplacer le logging standard par notre logger structuré
import logger as logmod
logging = logmod.logging
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from whatsapp_client import WhatsAppClient
from conversation_manager import ConversationManager, ConversationStatus
from sales_agent import SalesAgent, SalesStage


# ─── Configuration ─────────────────────────────────────────

# Le logger est configuré par logger.py (déjà importé ci-dessus)
from logger import log_webhook, log_metric, get_conv_logger
from product_images import send_product_image as _send_product_image
from order_handler import create_order as _create_order, build_conversation_summary as _build_conversation_summary
from recommendations import send_product_recommendations as _send_product_recommendations
from team import load_team as _load_team, add_member as _add_member, \
    remove_member as _remove_member, toggle_member as _toggle_member
from shop_settings import load_settings as _load_shop_settings, \
    save_settings as _save_shop_settings, build_shop_block as _build_shop_block

BASE_DIR = Path(__file__).parent

DASHBOARD_URL = "https://bot.zariamall.com"


async def _notify_team(wa_name: str, wa_phone: str) -> None:
    """Envoie une alerte WhatsApp à chaque membre actif de l'équipe.

    Appelé quand le bot escalade (passe la main à un humain). Le membre
    reçoit le nom du client, son numéro et le lien du dashboard.
    """
    phones = [m.get("phone") for m in _load_team() if m.get("active")]
    if not phones:
        logger.info("🚨 Escalade sans équipe configurée (aucun destinataire)")
        return
    alert = (
        f"🚨 Escalade !\n"
        f"👤 Client : {wa_name}\n"
        f"📞 {wa_phone}\n"
        f"Le client demande un humain. Traitez sur le dashboard :\n"
        f"{DASHBOARD_URL}"
    )
    for phone in phones:
        try:
            await whatsapp.send_text(phone, alert)
            logger.info(f"🚨 Alerte escalade envoyée à {phone}")
        except Exception as e:
            logger.error(f"Échec alerte escalade vers {phone}: {e}")


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


config = load_config()
conv_mgr = ConversationManager(
    db_path=config.get("database", {}).get("path", "data/conversations.db")
)
sales_agent = SalesAgent(config, conv_mgr)

# ─── Mutex par conversation (évite les traitements parallèles) ──
_conversation_locks: dict[str, asyncio.Lock] = {}
_pending_messages: dict[str, list[str]] = {}  # Messages reçus pendant traitement
_seen_message_ids: dict[str, set] = {}  # Déduplication par msg_id (Meta retry)


def _get_lock(wa_phone: str) -> asyncio.Lock:
    """Retourne le lock pour un numéro WhatsApp (crée si nécessaire)."""
    if wa_phone not in _conversation_locks:
        _conversation_locks[wa_phone] = asyncio.Lock()
    return _conversation_locks[wa_phone]


def _get_pending_messages(wa_phone: str) -> list[str]:
    """Retourne et vide la file des messages en attente pour un numéro."""
    msgs = _pending_messages.pop(wa_phone, [])
    return msgs

whatsapp = WhatsAppClient(
    phone_number_id=config["whatsapp"]["phone_number_id"],
    access_token=config["whatsapp"]["access_token"],
    api_version=config["whatsapp"].get("api_version", "v21.0"),
)

app = FastAPI(title="BotWhatsApp - Vente Conversationnelle", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mode du bot
BOT_MODE = config.get("bot", {}).get("mode", "hybrid")
VERIFY_TOKEN = config["whatsapp"]["verify_token"]


# ─── Webhook WhatsApp ──────────────────────────────────────

@app.get("/webhook/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    """Vérification du webhook par Meta."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook vérifié avec succès")
        # Meta attend le challenge en texte brut (pas de JSON, pas de cast int)
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=hub_challenge)
    logger.warning(f"Vérification webhook refusée (token reçu: {hub_verify_token[:20]}...)")
    raise HTTPException(status_code=403, detail="Token de vérification invalide")


@app.post("/webhook/whatsapp")
async def receive_whatsapp(request: Request):
    """Réception des messages WhatsApp avec mutex par conversation."""
    raw = await request.body()
    # Tolérer les caractères mal encodés (copier-coller Word/Windows-1252)
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        body = json.loads(raw.decode("latin-1"))
    msg = WhatsAppClient.extract_message(body)

    if not msg or msg["type"] == "status":
        return JSONResponse({"status": "ignored"})

    wa_phone = msg["from"]
    text = msg.get("text", "")
    msg_id = msg.get("id", "")
    product_id = msg.get("product_id")
    reply_to_id = msg.get("reply_to_id")

    # Fonctionnalité "Répondre" de WhatsApp : le client répond à un message
    # PRÉCIS du bot (context.id = wamid de ce message). On retrouve le message
    # d'origine : s'il contenait une fiche produit, on cible CE produit au lieu
    # de laisser le LLM deviner ("celui-là" après 3 produits envoyés).
    if reply_to_id:
        replied = conv_mgr.get_message_by_wamid(reply_to_id) if reply_to_id else None
        if replied and replied.get("direction") == "outgoing":
            try:
                meta = json.loads(replied.get("metadata_json") or "{}")
            except (ValueError, TypeError):
                meta = {}
            replied_pid = meta.get("product_id")
            replied_name = meta.get("product_name")
            if replied_pid and replied_name:
                text = (f"{text}\n\n[Réponse à « {replied_name} » "
                        f"(produit ID {replied_pid}) — le client réagit à CE produit]")
                logger.info(f"↩️ Répondre détecté: client réagit à « {replied_name} » (ID {replied_pid})")
            elif replied_name:
                text = f"{text}\n\n[Réponse à « {replied_name} »]"
                logger.info(f"↩️ Répondre détecté: client réagit à « {replied_name} »")
        elif replied:
            logger.info(f"↩️ Répondre vers message entrant ignoré (pas sortant)")

    # Client a envoyé une fiche produit du catalogue WhatsApp Commerce
    # (interactive type=product OU context.referred_product). Le texte brut
    # "produit:<id>" ne suffit pas : on l'enrichit avec les infos réelles du
    # catalogue pour que le LLM réponde sur le bon produit.
    if product_id:
        p = None
        if product_id.isdigit() and sales_agent.cache:
            p = sales_agent.cache.get_by_id(int(product_id))
        if p:
            price = p.get("price_display", "?")
            stock = p.get("stock_status", "?")
            name = p.get("name", "?")
            logger.info(f"🛒 Produit catalogue WhatsApp: {name} ({price})")
            if not text or text.startswith("produit:"):
                # Aucun texte client : on construit le message d'intérêt
                text = (
                    f"[Produit envoyé par le client : {name} - {price} "
                    f"- Stock: {stock} - ID: {product_id}]\n"
                    f"Je suis intéressé par ce produit"
                )
            else:
                # Texte client + produit référencé (ex: "Disponible ?" avec
                # context.referred_product) : on préfixe pour que le LLM
                # sache QUEL produit est concerné.
                text = (
                    f"[Produit référencé : {name} - {price} "
                    f"- Stock: {stock} - ID: {product_id}]\n"
                    f"{text}"
                )
        else:
            if not text or text.startswith("produit:"):
                text = f"[Produit inconnu du catalogue, ID: {product_id}]\nJe suis intéressé par ce produit"
            else:
                text = f"[Produit référencé inconnu du catalogue, ID: {product_id}]\n{text}"
            logger.warning(f"🛒 Produit WhatsApp inconnu du cache: {product_id}")

    if not text:
        return JSONResponse({"status": "no_text"})

    # Log webhook brut (debug)
    log_webhook(body)

    # Logger conversation — résoudre le VRAI conv_id AVANT de logger, pour que
    # le message entrant et la réponse soient dans le MÊME fichier conv_<id>.
    # Avant : get_conv_logger(0, ...) créait conv_0_<phone>.log séparé de la
    # réponse (conv_3_<phone>.log) — logs éclatés pour une même conversation.
    conv_name = msg.get("name", wa_phone)
    resolved = conv_mgr.get_or_create_conversation(wa_phone, conv_name)
    resolved_conv_id = resolved.get("id", 0)
    conv_log = get_conv_logger(resolved_conv_id, wa_phone)
    conv_log.info(f"📥 [{conv_name}] {text}")

    # Déduplication : ignorer les messages déjà traités (Meta retry)
    if msg_id:
        if wa_phone not in _seen_message_ids:
            _seen_message_ids[wa_phone] = set()
        if msg_id in _seen_message_ids[wa_phone]:
            logger.info(f"🔄 Message dupliqué ignoré: {msg_id}")
            return JSONResponse({"status": "duplicate"})
        _seen_message_ids[wa_phone].add(msg_id)
        # Nettoyer les IDs > 100 pour éviter fuite mémoire
        if len(_seen_message_ids[wa_phone]) > 100:
            _seen_message_ids[wa_phone] = set(list(_seen_message_ids[wa_phone])[-50:])

    # Acquérir le lock pour cette conversation
    lock = _get_lock(wa_phone)

    # Si le lock est déjà pris, mettre le message en attente
    if lock.locked():
        if wa_phone not in _pending_messages:
            _pending_messages[wa_phone] = []
        _pending_messages[wa_phone].append(text)
        logger.info(f"⏳ Message mis en attente pour {wa_phone}: {text[:50]}")
        return JSONResponse({"status": "queued", "pending_count": len(_pending_messages[wa_phone])})

    async with lock:
        # Fusionner les messages reçus pendant l'attente du lock
        merged_text = text
        pending = _get_pending_messages(wa_phone)
        if pending:
            merged_text = " | ".join([text] + pending)
            logger.info(f"🔗 Messages fusionnés pour {wa_phone}: {merged_text[:100]}")

        return await _process_single_message(wa_phone, msg, merged_text, body)


async def _process_single_message(wa_phone: str, msg: dict, text: str, body: dict):
    """Traite un message (potentiellement fusionné) et gère les messages en attente."""
    wa_name = msg.get("name", "")
    msg_id = msg.get("id", "")

    logger.info(f"Message reçu de {wa_name} ({wa_phone}): {text[:80]}")

    # Marquer comme lu
    if msg_id:
        try:
            await whatsapp.mark_as_read(msg_id)
        except Exception:
            pass

    # Détecter lien produit — lookup EXACT par slug dans le cache FTS5 (<1ms).
    # Avant : recherche floue WooCommerce, lente, pouvait retourner un mauvais produit.
    enriched_text = text
    product_link = re.search(r'zariamall\.com/produit/([^/\s\)\]]+)', text)
    if product_link:
        try:
            product_slug = product_link.group(1)
            p = None
            if sales_agent.cache:
                p = sales_agent.cache.get_by_slug(product_slug)
                if not p:
                    # Fallback: le slug peut avoir changé, chercher par nom
                    slug_as_query = product_slug.replace("-", " ")
                    hits = sales_agent.cache.search(slug_as_query, limit=1)
                    p = hits[0] if hits else None
            else:
                products = await sales_agent.catalog.search(product_slug.replace("-", " "), limit=1)
                p = sales_agent.catalog.extract_product_info(products[0]) if products else None

            if p:
                price = p.get("price_display", "?")
                stock = p.get("stock_status", "?")
                name = p.get("name", "?")
                pid = p.get("id", "?")
                enriched_text = (
                    f"{text}\n\n[Produit détecté : {name} - {price} "
                    f"- Stock: {stock} - ID: {pid}]"
                )
                # Stocker pour mémorisation après résolution du conv_id
                logger.info(f"🔗 Lien produit: {name} ({price})")
            else:
                logger.info(f"🔗 Lien non trouvé dans le catalogue: {product_slug}")
        except Exception as e:
            logger.warning(f"Erreur lien: {e}")

    # Traiter via l'agent
    result = await sales_agent.process_message(wa_phone, wa_name, enriched_text)

    # Recréer le logger avec le vrai conv_id
    conv_id = result.get("conv_id", 0)
    conv_log = get_conv_logger(conv_id, wa_phone)
    response = await _send_response(wa_phone, wa_name, text, result)

    # Après avoir répondu, traiter les messages en attente
    pending = _get_pending_messages(wa_phone)
    if pending:
        merged = " | ".join(pending)
        logger.info(f"📨 Traitement des {len(pending)} messages en attente pour {wa_phone}")
        new_msg = {"from": wa_phone, "name": wa_name, "id": "", "text": merged, "type": "text"}
        return await _process_single_message(wa_phone, new_msg, merged, body)

    return response


async def _send_response(wa_phone: str, wa_name: str, text: str, result: dict):
    """Envoie la réponse (texte + image si demandée) et crée la commande si possible."""
    conv_id = result.get("conv_id")
    # Logger de conversation local — ne dépend pas du scope de l'appelant
    conv_log = get_conv_logger(conv_id or 0, wa_phone)

    # Vérifier création commande auto (nom + tel + adresse requis)
    if conv_id:
        ctx = conv_mgr.get_context(conv_id)
        produit_id = ctx.get("produit_id")
        if produit_id and not ctx.get("_order_created"):
            # Normaliser les clés : le LLM écrit parfois client_nom / tel /
            # adresse_livraison au lieu de nom / telephone / adresse. Bug prod
            # 05/08 : "client_nom: Yves Serges" + adresse jamais stockée ->
            # commande jamais créée (vente perdue de 265 000 FCFA).
            telephone = (ctx.get("telephone") or ctx.get("tel")
                         or ctx.get("phone") or wa_phone)
            nom = (ctx.get("nom") or ctx.get("client_nom")
                   or ctx.get("name") or wa_name)
            adresse = (ctx.get("adresse") or ctx.get("adresse_livraison")
                       or ctx.get("livraison") or ctx.get("address") or "")
            produit_nom = ctx.get("produit_nom", "")

            if nom and telephone and adresse:
                await _create_order(sales_agent.catalog, conv_mgr, whatsapp,
                                    conv_id, wa_phone, nom, telephone, adresse,
                                    int(produit_id), produit_nom)

    # Escalade
    if result.get("should_escalate"):
        await whatsapp.send_text(wa_phone, result["response"])
        # Notifier l'équipe : chaque membre actif reçoit une alerte WhatsApp
        # avec le nom du client et le lien du dashboard.
        await _notify_team(wa_name, wa_phone)
        return JSONResponse({"status": "escalated"})

    # Mode review
    if BOT_MODE != "auto" and result.get("needs_review"):
        conv_mgr.set_needs_review(conv_id)
        return JSONResponse({"status": "pending_review", "conv_id": conv_id})

    # Envoi réponse
    resp_sent = await whatsapp.send_text(wa_phone, result["response"])
    # Stocker le wamid retourné par Meta (pour la fonctionnalité "Répondre").
    wamid = ""
    try:
        wamid = resp_sent["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        pass
    if wamid and result.get("message_id"):
        # Associer le produit du contexte si recommandation (permet de cibler
        # le produit quand le client répond à ce message).
        ctx = conv_mgr.get_context(conv_id) if conv_id else {}
        conv_mgr.update_message_wamid(
            result["message_id"], wamid,
            metadata={
                "product_id": str(ctx.get("produit_id", "") or ""),
                "product_name": ctx.get("produit_nom", "") or "",
            })

    origin = result.get("origin", "?")
    logger.info(f"Réponse envoyée à {wa_name} [{origin}] (stage={result['sales_stage']}, conf={result['confidence']:.2f})")

    # Métriques structurées
    log_metric("response_sent", origin=origin, stage=result["sales_stage"],
               confidence=result["confidence"], conv_id=result.get("conv_id", 0))
    conv_log.info(f"📤 [{origin}] {result['response'][:100]}")

    # Envoi image si demandée — détection renforcée
    photo_keywords = ["photo", "image", "montre", "voir", "couleur"]
    user_wants_photo = any(kw in text.lower() for kw in photo_keywords)
    llm_refused_photo = any(phrase in result["response"].lower() for phrase in [
        "je ne peux pas envoyer", "je ne peux pas montrer", "pas de photo", "pas d'image",
        "pas de photo ici", "pas d'image ici"
    ])

    # Recommandation produit : le CODE envoie les produits (nom+prix+lien réels),
    # le LLM ne fait que l'intro. Zéro troncature, zéro lien halluciné.
    if result.get("sales_stage") == "recommandation" and not result.get("fast_path"):
        await _send_product_recommendations(
            sales_agent.cache, conv_mgr, whatsapp, log_metric,
            wa_phone, conv_id, text=result["response"], user_text=text)

    if user_wants_photo:
        if llm_refused_photo:
            logger.info("Forçage envoi image: le LLM a dit qu'il ne pouvait pas")
        await _send_product_image(sales_agent.cache, conv_mgr, whatsapp, log_metric,
                                  wa_phone, result, conv_id, user_text=text)

    return JSONResponse({
        "status": "sent",
        "confidence": result["confidence"],
        "stage": result["sales_stage"],
    })


# ─── API Dashboard ─────────────────────────────────────────

@app.get("/api/conversations")
async def get_conversations(status: Optional[str] = None):
    """Liste toutes les conversations (avec order_id extrait du contexte)."""
    if status == "pending":
        conversations = conv_mgr.get_pending_reviews()
    elif status == "active":
        conversations = conv_mgr.get_active_conversations()
    else:
        conversations = conv_mgr.get_all_conversations()
    return [_with_order_id(c, conv_mgr) for c in conversations]


def _with_order_id(conv: dict, mgr) -> dict:
    """Ajoute order_id et order_status (extraits du contexte) à une conversation."""
    out = dict(conv)
    out["order_id"] = ""
    out["order_status"] = ""
    try:
        ctx = mgr.get_context(conv["id"])
        if ctx.get("_order_created"):
            out["order_id"] = ctx.get("_order_created")
            out["order_status"] = ctx.get("_order_status", "processing")
    except Exception:
        pass
    return out


@app.get("/api/conversations/search")
async def search_conversations(q: str = ""):
    """Recherche conversations par nom/téléphone."""
    if not q: return []
    all_c = conv_mgr.get_all_conversations(limit=500)
    ql = q.lower()
    return [
        {"id": c["id"], "wa_name": c.get("wa_name",""), "wa_phone": c["wa_phone"],
         "status": c["status"], "sales_stage": c.get("sales_stage",""),
         "needs_review": c.get("needs_review",0)}
        for c in all_c if ql in c.get("wa_name","").lower() or ql in c["wa_phone"]
    ][:20]


@app.get("/api/conversations/{conv_id}")
async def get_conversation_detail(conv_id: int):
    """Détail d'une conversation avec ses messages."""
    conv = conv_mgr.get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    messages = conv_mgr.get_messages(conv_id, limit=100)
    context = conv_mgr.get_context(conv_id)
    return {"conversation": conv, "messages": messages, "context": context}


@app.post("/api/conversations/{conv_id}/clear")
async def clear_conversation(conv_id: int):
    """Vide tous les messages et le contexte d'une conversation (dashboard)."""
    conv = conv_mgr.get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    conv_mgr.clear_conversation(conv_id)
    return {"status": "cleared", "conv_id": conv_id}

@app.get("/api/base-rules")
async def get_base_rules():
    """Lit les règles de base du bot (édition via dashboard)."""
    try:
        with open(BASE_DIR / "data" / "base_rules.json", encoding="utf-8") as f:
            import json as _json
            return _json.load(f)
    except Exception:
        return {"rules": ""}

@app.post("/api/base-rules")
async def update_base_rules(request: Request):
    """Met à jour les règles de base du bot (depuis le dashboard)."""
    import json as _json
    raw = await request.body()
    # Tolérer les caractères mal encodés (copier-coller Word/Windows-1252)
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        body = json.loads(raw.decode("latin-1"))
    rules = body.get("rules", "")
    if not rules or not rules.strip():
        raise HTTPException(status_code=400, detail="Règles vides")
    (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    with open(BASE_DIR / "data" / "base_rules.json", "w", encoding="utf-8") as f:
        _json.dump({"rules": rules}, f, ensure_ascii=False, indent=2)
    # Recharger dans base_rules (force re-import des modules dependants)
    import importlib, base_rules, sales_agent, fast_router
    base_rules.refresh_base_rules()
    sales_agent.BASE_RULES = base_rules.BASE_RULES
    fast_router.BASE_RULES = base_rules.BASE_RULES
    logger.info(f"BASE_RULES mises a jour ({len(rules)} chars)")
    return {"status": "ok", "length": len(rules)}

@app.post("/api/conversations/{conv_id}/approve")
async def approve_response(conv_id: int):
    """Approuve et envoie la réponse en attente."""
    # Récupérer le dernier message sortant non validé
    unreviewed = conv_mgr.get_unreviewed_messages(conv_id)
    if not unreviewed:
        raise HTTPException(status_code=404, detail="Aucun message en attente")

    msg = unreviewed[-1]  # le plus récent
    conv = conv_mgr.get_conversation_by_id(conv_id)

    if conv:
        # Envoyer via WhatsApp
        await whatsapp.send_text(conv["wa_phone"], msg["content"])
        # Marquer comme validé
        conv_mgr.approve_message(msg["id"])
        conv_mgr.clear_review(conv_id)
        conv_mgr.update_status(conv_id, ConversationStatus.ACTIVE)
        logger.info(f"Message approuvé et envoyé à {conv['wa_name']}")

    return {"status": "sent", "message_id": msg["id"]}


@app.post("/api/conversations/{conv_id}/edit-send")
async def edit_and_send(conv_id: int, request: Request):
    """Modifie la réponse puis l'envoie."""
    raw = await request.body()
    # Tolérer les caractères mal encodés (copier-coller Word/Windows-1252)
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        body = json.loads(raw.decode("latin-1"))
    new_text = body.get("text", "")

    if not new_text:
        raise HTTPException(status_code=400, detail="Texte requis")

    conv = conv_mgr.get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    # Envoyer le message modifié
    await whatsapp.send_text(conv["wa_phone"], new_text)

    # Enregistrer le message modifié
    conv_mgr.add_message(conv_id, "outgoing", new_text, reviewed=True)

    # Nettoyer les messages non validés
    for msg in conv_mgr.get_unreviewed_messages(conv_id):
        conv_mgr.approve_message(msg["id"])
    conv_mgr.clear_review(conv_id)
    conv_mgr.update_status(conv_id, ConversationStatus.ACTIVE)

    return {"status": "sent"}


@app.post("/api/conversations/{conv_id}/reject")
async def reject_response(conv_id: int):
    """Rejette la réponse générée et repasse en mode humain."""
    unreviewed = conv_mgr.get_unreviewed_messages(conv_id)
    for msg in unreviewed:
        conv_mgr.approve_message(msg["id"])  # Marque comme traité sans envoyer
    conv_mgr.update_status(conv_id, ConversationStatus.HUMAN_HANDLED)
    conv_mgr.clear_review(conv_id)
    return {"status": "rejected", "mode": "human"}


@app.post("/api/conversations/{conv_id}/send")
async def send_custom_message(conv_id: int, request: Request):
    """Envoie un message personnalisé (mode humain)."""
    raw = await request.body()
    # Tolérer les caractères mal encodés (copier-coller Word/Windows-1252)
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        body = json.loads(raw.decode("latin-1"))
    text = body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Texte requis")

    conv = conv_mgr.get_conversation_by_id(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    await whatsapp.send_text(conv["wa_phone"], text)
    conv_mgr.add_message(conv_id, "outgoing", text, reviewed=True)
    return {"status": "sent"}


@app.post("/api/conversations/{conv_id}/resume-bot")
async def resume_bot(conv_id: int):
    """Redonne la main au bot après une intervention humaine."""
    conv_mgr.update_status(conv_id, ConversationStatus.ACTIVE)
    return {"status": "resumed"}


@app.post("/api/orders/create")
async def create_order(request: Request):
    """Crée une commande WooCommerce depuis WhatsApp.

    Body: {
        "conv_id": 1,
        "product_id": 37507,
        "quantity": 1,
        "customer_name": "Leonard",
        "customer_phone": "22556091657",
        "customer_address": "Koumassi"
    }
    """
    raw = await request.body()
    # Tolérer les caractères mal encodés (copier-coller Word/Windows-1252)
    try:
        body = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        body = json.loads(raw.decode("latin-1"))
    conv_id = body.get("conv_id")
    product_id = body.get("product_id")
    quantity = body.get("quantity", 1)

    conv = conv_mgr.get_conversation_by_id(conv_id) if conv_id else None
    customer_name = body.get("customer_name") or (conv["wa_name"] if conv else "Client WhatsApp")
    customer_phone = body.get("customer_phone") or (conv["wa_phone"] if conv else "")
    customer_address = body.get("customer_address", "")

    try:
        order = await sales_agent.catalog.create_order(
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_address=customer_address,
            items=[{"product_id": product_id, "quantity": quantity}],
        )
        order_id = order.get("id", 0)

        # Envoyer confirmation WhatsApp
        if conv:
            await whatsapp.send_text(
                conv["wa_phone"],
                f"✅ Commande confirmée !\n"
                f"N° {order_id}\n"
                f"Total : {order.get('total', '0')} FCFA\n"
                f"Paiement à la livraison\n"
                f"Livraison : {customer_address or 'Abidjan'}"
            )
            conv_mgr.add_message(conv_id, "outgoing",
                f"✅ Commande #{order_id} créée - {order.get('total', '0')} FCFA", reviewed=True)
            conv_mgr.update_sales_stage(conv_id, SalesStage.CLOSED_WON)

        return {"status": "created", "order_id": order_id, "total": order.get("total")}
    except Exception as e:
        logger.error(f"Erreur création commande: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/team")
async def get_team():
    """Liste les membres de l'équipe (destinataires des alertes d'escalade)."""
    return {"team": _load_team()}


@app.post("/api/team")
async def post_team(body: dict):
    """Ajoute un membre : {"name": "...", "phone": "+225..."}"""
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    if not name or not phone:
        raise HTTPException(status_code=400, detail="name et phone requis")
    member = _add_member(name, phone)
    return {"status": "ok", "member": member}


@app.delete("/api/team")
async def delete_team(body: dict):
    """Retire un membre : {"phone": "+225..."}"""
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone requis")
    ok = _remove_member(phone)
    return {"status": "ok" if ok else "not_found"}


@app.post("/api/team/toggle")
async def toggle_team(body: dict):
    """Active/désactive un membre : {"phone": "+225..."}"""
    phone = (body.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone requis")
    active = _toggle_member(phone)
    return {"status": "ok", "active": active}


@app.get("/api/shop-settings")
async def get_shop_settings():
    """Paramètres boutique (horaires, livraison, paiement, FAQ)."""
    return _load_shop_settings()


@app.post("/api/shop-settings")
async def post_shop_settings(body: dict):
    """Met à jour les paramètres boutique (fusion avec les défauts)."""
    settings = _load_shop_settings()
    for k, v in body.items():
        if k in settings and v is not None:
            settings[k] = v
    _save_shop_settings(settings)
    return {"status": "ok", "settings": settings}


@app.get("/api/stats")
async def get_stats():
    """Statistiques globales."""
    conversations = conv_mgr.get_all_conversations(limit=1000)
    total = len(conversations)
    active = sum(1 for c in conversations if c["status"] == "active")
    pending = sum(1 for c in conversations if c["needs_review"] == 1)
    human = sum(1 for c in conversations if c["status"] == "human")
    closed = sum(1 for c in conversations if c["status"] == "closed")

    stages = {}
    for c in conversations:
        stage = c.get("sales_stage", "unknown")
        stages[stage] = stages.get(stage, 0) + 1

    return {
        "total": total,
        "active": active,
        "pending_review": pending,
        "human_handled": human,
        "closed": closed,
        "by_stage": stages,
    }


@app.get("/api/config")
async def get_config():
    """Retourne la config (sans les tokens sensibles)."""
    safe = config.copy()
    safe["whatsapp"]["access_token"] = "***"
    safe["verify_token"] = "***"
    return safe


@app.get("/api/orders")
async def get_orders():
    """Commandes WooCommerce créées via WhatsApp."""
    conversations = conv_mgr.get_all_conversations(limit=1000)
    orders = []
    for c in conversations:
        ctx = conv_mgr.get_context(c["id"])
        oid = ctx.get("_order_created")
        if oid:
            orders.append({
                "order_id": oid, "status": ctx.get("_order_status", "pending"),
                "name": ctx.get("nom", c.get("wa_name", "")),
                "phone": c["wa_phone"], "product": ctx.get("produit_nom", ""),
                "address": ctx.get("adresse", ""), "conv_id": c["id"],
                "date": c.get("updated_at", ""),
            })
    return sorted(orders, key=lambda o: str(o["order_id"]), reverse=True)


# ─── Dashboard HTML ────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Dashboard de supervision."""
    return (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")


# ─── Lancement ─────────────────────────────────────────────

if __name__ == "__main__":
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8000)

    logger.info(f"BotWhatsApp démarré sur http://{host}:{port}")
    logger.info(f"Mode: {BOT_MODE}")
    logger.info(f"Webhook: {server_cfg.get('webhook_path', '/webhook/whatsapp')}")

    uvicorn.run(app, host=host, port=port, log_level="info")
