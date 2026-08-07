"""Envoi des recommandations produit par le CODE — pas le LLM (option C).

Le LLM produit une courte intro conversationnelle (< 150 car). Le code envoie
ensuite UN message par produit : nom, prix, lien RÉEL du catalogue.

Résout définitivement :
- la troncature 150 car qui coupait les listes de produits au milieu
- les liens hallucinés (le code n'envoie que des liens du catalogue)
- les promesses non tenues ("je vous envoie 3 produits" -> rien)

Anti-spam : un hash des produits déjà montrés évite de renvoyer la même liste
si le client répond "ok" sans changer de demande.
"""

import re
import logging

logger = logging.getLogger(__name__)


def _products_hash(products: list) -> str:
    """Hash des IDs produits — pour ne renvoyer que si la liste change."""
    return ",".join(str(p.get("id", "")) for p in products)


def _build_product_message(product: dict, index: int) -> str:
    """Formate UN produit en message WhatsApp court et complet."""
    name = product.get("name", "Produit")
    price = product.get("price_display") or product.get("price", "?")
    link = product.get("permalink", "")
    emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"][index % 6]
    msg = f"{emoji} {name[:80]}\n💰 {price}"
    if link:
        msg += f"\n🔗 {link}"
    return msg


async def send_product_recommendations(cache, conv_mgr, whatsapp, log_metric,
                                       wa_phone: str, conv_id: int, text: str,
                                       user_text: str = ""):
    """Envoie les produits recommandés, un par un, avec liens réels du catalogue.

    Retourne le nombre de produits envoyés (0 = rien, déjà montrés ou pas de
    demande de produits).
    """
    try:
        from category_tracker import (resolve_search_query, wants_to_see_products,
                                      is_no_preference, detect_category)

        ctx = conv_mgr.get_context(conv_id) if conv_id else {}
        category = ctx.get("_category") or detect_category(user_text or text)

        # Déterminer les produits à montrer (même logique que le prompt)
        # Déclencheurs :
        #  1. Le client demande à voir ("montre moi", "propose moi autre chose")
        #  2. Le client n'a pas de préférence ("peu importe")
        #  3. La réponse LLM PROMET des produits ("Voici 3 congélateurs...") —
        #     sinon le client reçoit une intro sans les produits promis (bug prod
        #     04/08 : "Je ne vois rien 😅").
        llm_promises_products = bool(
            re.search(r"voici|je vous (envoie|présente)|je t'envoie|voilà", text, re.I)
            and re.search(r"\d", text)
        )
        if wants_to_see_products(user_text or text) or llm_promises_products:
            query = resolve_search_query(user_text or text, category)
            products = cache.search(query, limit=5) if query else cache.search("", limit=5)
        elif is_no_preference(user_text or text) and category:
            query = resolve_search_query(user_text or text, category)
            products = cache.top_products(query, limit=5, order="desc")
        else:
            # Pas une demande de produits : le LLM a répondu à autre chose
            return 0

        if not products:
            return 0

        # Anti-spam : ne pas renvoyer la même liste
        h = _products_hash(products)
        if ctx.get("_products_hash") == h:
            return 0
        if conv_id:
            conv_mgr.update_context(conv_id, {"_products_hash": h})

        # Envoyer chaque produit séparément
        sent = 0
        for i, p in enumerate(products):
            msg = _build_product_message(p, i)
            resp = await whatsapp.send_text(wa_phone, msg)
            wamid = ""
            try:
                wamid = resp["messages"][0]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            if conv_id:
                conv_mgr.add_message(
                    conv_id, "outgoing", msg, reviewed=True,
                    wa_message_id=wamid,
                    metadata={"product_id": str(p.get("id", "")),
                              "product_name": p.get("name", "")})
            sent += 1

        try:
            log_metric("products_sent", count=sent, category=category or "?",
                       conv_id=conv_id)
        except Exception:
            pass  # une métrique ne doit jamais casser l'envoi
        logger.info(f"📦 {sent} produits envoyés ({category or '?'})")
        return sent
    except Exception as e:
        logger.warning(f"Envoi produits échoué: {type(e).__name__}: {e}")
        return 0
