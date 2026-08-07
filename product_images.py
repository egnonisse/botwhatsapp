"""Résolution et envoi d'images produit — extrait de server.py (SOUL.md).

Fonctions pures : les dépendances (cache, conv_mgr, whatsapp, logger, log_metric)
sont injectées en paramètres, pas lues depuis des globals. Facile à tester.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Tokens sans valeur de recherche dans une réponse LLM (ex: "Voici le Galaxy A35...")
_STOP_TOKENS = {
    "voici", "le", "la", "les", "je", "vous", "voulez", "commander",
    "excellent", "choix", "envoie", "lien", "par", "whatsapp", "avec",
    "pour", "produit", "prix", "est", "et", "ou", "une", "un", "des",
    "du", "de", "sur", "que", "qui", "à", "au", "aux", "ce", "cette",
    "fcfa", "disponible", "prendre", "peux", "puis", "fais", "fait",
}


async def resolve_product_for_image(cache, conv_mgr, result: dict,
                                    conv_id: int, user_text: str = ""):
    """Résout LE produit dont le client veut la photo, du plus fiable au moins fiable.

    Bug corrigé : l'ancienne version cherchait un permalink dans les 10 derniers
    messages sortants, sinon retombait sur ctx["besoin"], puis faisait une recherche
    floue limit=1. Résultat en prod : photo des lunettes Blackview BV100 renvoyée
    pour une demande de ZTE Axon, et "ZTE Axon 40 Pro" au lieu de "Axon 60 Ultra".

    Retourne (produit_dict, source) ou (None, raison).
    """
    response = result.get("response", "") or ""
    ctx = conv_mgr.get_context(conv_id) if conv_id else {}

    # 1. Permalink dans la réponse qu'on vient d'envoyer → intention la plus fraîche
    m = re.search(r"zariamall\.com/produit/([^/\s\)\]]+)", response)
    if m and cache:
        p = cache.get_by_slug(m.group(1))
        if p:
            return p, "slug:response"

    # 2. produit_id du contexte — posé par le LLM sur le produit en cours de discussion
    if cache and ctx.get("produit_id"):
        p = cache.get_by_id(ctx["produit_id"])
        if p:
            return p, "id:context"

    # 3. Nom exact en gras dans la réponse (**ZTE Axon 60 Ultra**)
    if cache:
        for name in re.findall(r"\*\*([^*]{4,80})\*\*", response):
            cleaned = re.sub(r"\s*[—–-]\s*\d[\d\s]*FCFA.*$", "", name).strip()
            hits = cache.search(cleaned, limit=1)
            if hits and cleaned.lower() in hits[0]["name"].lower():
                return hits[0], "name:bold"

    # 3b. Nom de produit en TEXTE CLAIR dans la réponse ("Voici le Galaxy A35...").
    #     Manquait : le LLM nomme souvent le produit sans gras ni lien, et la
    #     cascade retombait sur produit_nom du contexte (ancien produit) →
    #     mauvaise image envoyée (Tab A11 pour un Galaxy A35, vu en prod).
    if cache and response:
        tokens = [t for t in re.findall(r"[a-zà-ÿ0-9]+", response.lower())
                  if t not in _STOP_TOKENS and len(t) > 2]
        if tokens:
            query = " ".join(tokens[:8])
            hits = cache.search(query, limit=3)
            if hits:
                best = hits[0]
                # Validation stricte : le meilleur hit doit partager ≥2 tokens
                # avec la réponse, OU 1 token modèle (avec chiffre, ex: a35, v50).
                # Sinon c'est un produit non-catalogue (ex: "Galaxy A35" inconnu →
                # ne pas envoyer l'image d'un autre produit comme Tab A11).
                name_tokens = set(re.findall(r"[a-zà-ÿ0-9]+", best["name"].lower()))
                overlap = name_tokens & set(tokens)
                model_tokens = [t for t in overlap if any(ch.isdigit() for ch in t)]
                if len(overlap) >= 2 or model_tokens:
                    return best, "name:plain"
                # Aucun match fiable : le produit cité n'existe pas au catalogue.
                # Ne PAS envoyer l'image d'un produit différent (règle : pas
                # d'image plutôt qu'une mauvaise image).
                return None, "produit_non_catalogue"

    # 4. Permalink du DERNIER sortant seulement (pas les 10 : c'est ce qui ramenait
    #    l'image d'un produit abandonné plusieurs tours plus tôt)
    if cache and conv_id:
        for msg in reversed(conv_mgr.get_messages(conv_id, limit=6)):
            if msg["direction"] != "outgoing" or msg["content"].startswith("[IMAGE:"):
                continue
            mm = re.search(r"zariamall\.com/produit/([^/\s\)\]]+)", msg["content"])
            if mm:
                p = cache.get_by_slug(mm.group(1))
                return (p, "slug:last_outgoing") if p else (None, "slug_introuvable")
            break  # on ne regarde QUE le dernier vrai message sortant

    # 5. Nom de produit mémorisé dans le contexte
    if cache and ctx.get("produit_nom"):
        hits = cache.search(ctx["produit_nom"], limit=1)
        if hits:
            return hits[0], "name:context"

    # Volontairement : plus de fallback sur ctx["besoin"] ni de recherche floue sur
    # le texte du client. Mieux vaut n'envoyer aucune image que la mauvaise.
    return None, "aucun_produit_identifiable"


async def send_product_image(cache, conv_mgr, whatsapp, log_metric,
                             wa_phone: str, result: dict, conv_id: int,
                             user_text: str = ""):
    """Envoie l'image du produit réellement discuté, ou rien du tout."""
    try:
        product, source = await resolve_product_for_image(cache, conv_mgr, result, conv_id, user_text)
        if not product:
            logger.info(f"📸 Image non envoyée: {source}")
            return

        image_url = product.get("image_url")
        name = product.get("name", "")
        if not image_url:
            logger.info(f"📸 Pas d'image pour '{name[:40]}' (source={source})")
            return

        # Anti-doublon : ne pas renvoyer la même image que la dernière envoyée.
        # En prod, IMG-20260726-WA0020.jpg (lunettes BV100) est partie 3 fois.
        if conv_id:
            for msg in reversed(conv_mgr.get_messages(conv_id, limit=4)):
                if msg["content"].startswith("[IMAGE:"):
                    if image_url in msg["content"]:
                        logger.info(f"📸 Image déjà envoyée récemment, ignorée: {name[:40]}")
                        return
                    break

        await whatsapp.send_image(wa_phone, image_url, caption=name[:100])
        conv_mgr.add_message(conv_id, "outgoing",
                             f"[IMAGE:{image_url}|{name[:80]}]", reviewed=True)
        log_metric("image_sent", source=source, product_id=product.get("id"), conv_id=conv_id)
        logger.info(f"📸 Image envoyée: {name[:50]} (source={source})")
    except Exception as e:
        logger.warning(f"Image échouée: {type(e).__name__}: {e}")
