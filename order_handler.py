"""Création de commande WooCommerce + confirmation WhatsApp — extrait de server.py.

Fonctions pures : catalog, conv_mgr, whatsapp injectés en paramètres.
"""

import logging

logger = logging.getLogger(__name__)


async def build_conversation_summary(conv_mgr, conv_id: int) -> str:
    """Résumé de la conversation pour la note de commande."""
    messages = conv_mgr.get_messages(conv_id, limit=20)
    if not messages:
        return "Conversation WhatsApp"
    lines = []
    for m in messages[-6:]:  # Derniers 6 messages
        role = "Client" if m["direction"] == "incoming" else "Zariamall"
        lines.append(f"{role}: {m['content'][:100]}")
    return " | ".join(lines)


async def create_order(catalog, conv_mgr, whatsapp, conv_id: int, wa_phone: str,
                       name: str, phone: str, address: str,
                       product_id: int, product_name: str):
    """Crée une commande WooCommerce (processing) et envoie la confirmation WhatsApp.

    Vérifie le STOCK RÉEL avant de créer : le cache FTS5 peut avoir jusqu'à 1h
    de retard (synchro périodique), un produit peut être vendu entre-temps.
    """
    from conversation_manager import SalesStage
    # Quantité demandée : lue dans le contexte (le LLM peut la stocker quand le
    # client demande "2×" — bug prod 05/08 : Blackview Active 12 Pro promis en
    # 2 exemplaires alors que le stock = 1). Défaut : 1.
    ctx = conv_mgr.get_context(conv_id) if conv_id else {}
    try:
        quantity = int(ctx.get("quantite") or ctx.get("quantity") or 1)
    except (ValueError, TypeError):
        quantity = 1
    quantity = max(1, quantity)
    try:
        # ── 1. Vérification stock temps réel (un appel API ponctuel) ──
        try:
            live = await catalog._get(f"products/{product_id}")
        except Exception as e:
            logger.warning(f"Impossible de vérifier le stock live ({e}) — création quand même")
            live = {}
        stock_status = live.get("stock_status", "instock")
        stock_qty = live.get("stock_quantity", 1)
        out_of_stock = stock_status == "outofstock" or (
            stock_status == "instock" and stock_qty is not None and stock_qty <= 0)

        if out_of_stock:
            msg = (f"😔 Désolé {name}, « {product_name} » vient d'être épuisé. "
                   f"Je vous propose un produit équivalent : dites-moi si vous voulez "
                   f"que je vous montre les alternatives.")
            await whatsapp.send_text(wa_phone, msg)
            conv_mgr.update_context(conv_id, {
                "_order_created": "", "_order_status": "out_of_stock"
            })
            logger.info(f"⛔ Stock épuisé (vérif live): {product_name} (ID {product_id})")
            return {"status": "out_of_stock", "product_id": product_id}

        # Quantité demandée > stock disponible : refuser proprement au lieu de
        # créer une commande impossible. Ex: client demande 2, stock = 1.
        if stock_qty is not None and quantity > int(stock_qty):
            msg = (f"😔 Désolé {name}, il ne reste que {int(stock_qty)} exemplaire(s) "
                   f"de « {product_name} ». Je peux vous en commander {int(stock_qty)} "
                   f"ou vous montrer un produit équivalent. Que préférez-vous ?")
            await whatsapp.send_text(wa_phone, msg)
            conv_mgr.update_context(conv_id, {
                "_order_created": "", "_order_status": "qty_too_high"
            })
            logger.info(f"⛔ Quantité {quantity} > stock {stock_qty}: {product_name} (ID {product_id})")
            return {"status": "qty_too_high", "product_id": product_id}

        # ── 2. Création commande ──
        order = await catalog.create_order(
            customer_name=name, customer_phone=phone, customer_address=address,
            items=[{"product_id": product_id, "quantity": quantity}],
        )
        await catalog._post(f"orders/{order['id']}", {"status": "processing"})

        resume = await build_conversation_summary(conv_mgr, conv_id)
        await catalog._post(f"orders/{order['id']}/notes", {
            "note": f"[BotWhatsApp] {resume}", "customer_note": False,
        })

        conv_mgr.update_context(conv_id, {
            "_order_created": order["id"], "_order_status": "processing"
        })

        msg = (f"✅ Commande confirmée #{order['id']} !\n"
               f"{product_name}\n"
               f"Quantité : {quantity}\n"
               f"Total : {order.get('total', '?')} FCFA\n"
               f"Livraison : {address}\nPaiement à la livraison 📦")
        await whatsapp.send_text(wa_phone, msg)
        conv_mgr.update_sales_stage(conv_id, SalesStage.CLOSED_WON)
        logger.info(f"📦 Commande #{order['id']} pour {name}")
        return {"status": "created", "order_id": order["id"]}
    except Exception as e:
        logger.error(f"Erreur commande: {e}")
