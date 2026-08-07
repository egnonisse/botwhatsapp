"""Client WhatsApp Cloud API — envoi et réception de messages."""

import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """Client pour l'API WhatsApp Cloud de Meta."""

    def __init__(self, phone_number_id: str, access_token: str, api_version: str = "v21.0"):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.api_version = api_version
        self.base_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, to: str, text: str) -> dict:
        """Envoie un message texte simple."""
        return await self._send_message(to, {"body": text}, "text")

    async def send_interactive_list(self, to: str, header: str, body: str,
                                     button_text: str, sections: list[dict]) -> dict:
        """Envoie un message interactif avec liste de choix."""
        message = {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "footer": {"text": "Choisissez une option"},
            "action": {
                "button": button_text,
                "sections": sections,
            },
        }
        return await self._send_message(to, message, "interactive")

    async def send_interactive_buttons(self, to: str, header: str, body: str,
                                        buttons: list[dict]) -> dict:
        """Envoie un message avec boutons rapides (max 3)."""
        message = {
            "type": "button",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {"buttons": buttons[:3]},  # WhatsApp limite à 3
        }
        return await self._send_message(to, message, "interactive")

    async def send_image(self, to: str, image_url: str, caption: str = "") -> dict:
        """Envoie une image avec légende optionnelle."""
        message = {
            "link": image_url,
            "caption": caption,
        }
        return await self._send_media(to, message, "image")

    async def send_document(self, to: str, doc_url: str,
                            filename: str = "document.pdf", caption: str = "") -> dict:
        """Envoie un document (PDF, etc.)."""
        message = {
            "link": doc_url,
            "filename": filename,
            "caption": caption,
        }
        return await self._send_media(to, message, "document")

    # ─── Catalogue / Produits ─────────────────────────────────

    async def send_product(self, to: str, catalog_id: str, product_retailer_id: str,
                           body: str = "") -> dict:
        """Envoie un produit unique du catalogue WhatsApp (message interactif).

        Args:
            catalog_id: ID du catalogue (ex: '1731270347996653')
            product_retailer_id: SKU/retailer_id du produit dans le catalogue
            body: Texte d'accompagnement optionnel
        """
        interactive = {
            "type": "product",
            "action": {
                "catalog_id": catalog_id,
                "product_retailer_id": product_retailer_id,
            },
        }
        if body:
            interactive["body"] = {"text": body}
        return await self._send_message(to, interactive, "interactive")

    async def send_product_list(self, to: str, catalog_id: str,
                                 header: str, body: str,
                                 sections: list[dict]) -> dict:
        """Envoie une liste de produits du catalogue (max 30 produits par section).

        Args:
            catalog_id: ID du catalogue
            header: Titre du message
            body: Texte descriptif
            sections: [{"title": "Section 1", "product_items": [{"product_retailer_id": "..."}]}, ...]
        """
        interactive = {
            "type": "product_list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {
                "catalog_id": catalog_id,
                "sections": sections,
            },
        }
        return await self._send_message(to, interactive, "interactive")

    async def get_catalog_products(self, catalog_id: str) -> list[dict]:
        """Récupère les produits d'un catalogue WhatsApp.

        Nécessite la permission business_management.
        """
        url = f"https://graph.facebook.com/{self.api_version}/{catalog_id}/products"
        params = {
            "fields": "id,name,description,price,availability,image_url,url,retailer_id",
            "access_token": self.access_token,
            "limit": 100,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", [])
            except Exception as e:
                logger.error(f"Erreur récupération catalogue: {e}")
                return []

    async def search_catalog_products(self, catalog_id: str, query: str) -> list[dict]:
        """Recherche des produits dans le catalogue par mot-clé."""
        products = await self.get_catalog_products(catalog_id)
        query_lower = query.lower()
        return [
            p for p in products
            if query_lower in p.get("name", "").lower()
            or query_lower in p.get("description", "").lower()
        ]

    async def mark_as_read(self, message_id: str) -> dict:
        """Marque un message comme lu."""
        url = f"{self.base_url}/messages"
        data = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        return await self._post(url, data)

    async def send_typing(self, to: str, typing: bool = True) -> dict:
        """Active/désactive l'indicateur 'en train d'écrire...'."""
        status = "typing" if typing else "paused"
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": "."},
            "status": status,
        }
        # L'API WhatsApp ne supporte pas directement typing indicator
        # via l'API Cloud standard — on garde cette méthode pour compatibilité future
        logger.debug(f"Typing indicator {status} for {to}")

    async def _send_message(self, to: str, content: dict, msg_type: str) -> dict:
        """Envoie un message via l'API WhatsApp."""
        url = f"{self.base_url}/messages"
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": msg_type,
        }
        if msg_type == "text":
            data["text"] = content
        elif msg_type == "interactive":
            data["interactive"] = content
        return await self._post(url, data)

    async def _send_media(self, to: str, content: dict, media_type: str) -> dict:
        """Envoie un média via l'API WhatsApp."""
        url = f"{self.base_url}/messages"
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: content,
        }
        return await self._post(url, data)

    async def _post(self, url: str, data: dict) -> dict:
        """POST vers l'API WhatsApp avec gestion d'erreurs."""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(url, json=data, headers=self.headers)
                resp.raise_for_status()
                result = resp.json()
                logger.info(f"Message envoyé avec succès: {result.get('messages', [{}])[0].get('id', 'N/A')}")
                return result
            except httpx.HTTPStatusError as e:
                logger.error(f"Erreur API WhatsApp: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Erreur envoi message: {e}")
                raise

    @staticmethod
    def extract_message(webhook_body: dict) -> Optional[dict]:
        """Extrait les infos du message depuis le webhook entrant.

        Retourne un dict: {
            'from': str,         # numéro expéditeur
            'id': str,           # message_id
            'text': str | None,  # corps du texte
            'type': str,         # text, interactive, image, etc.
            'timestamp': str,
        } ou None si pas de message valide.
        """
        try:
            entry = webhook_body.get("entry", [{}])[0]
            change = entry.get("changes", [{}])[0]
            value = change.get("value", {})
            messages = value.get("messages", [])

            if not messages:
                # Peut être une notification de statut
                statuses = value.get("statuses", [])
                if statuses:
                    return {
                        "from": statuses[0].get("recipient_id"),
                        "id": statuses[0].get("id"),
                        "text": None,
                        "type": "status",
                        "status": statuses[0].get("status"),
                        "timestamp": statuses[0].get("timestamp"),
                    }
                return None

            msg = messages[0]
            msg_type = msg.get("type", "text")

            # Extraire le texte selon le type
            text = None
            product_id = None
            if msg_type == "text":
                text = msg.get("text", {}).get("body", "")
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                if interactive.get("type") == "button_reply":
                    text = interactive.get("button_reply", {}).get("id", "")
                elif interactive.get("type") == "list_reply":
                    text = interactive.get("list_reply", {}).get("id", "")
                elif interactive.get("type") == "product":
                    # Client a envoyé une fiche produit du catalogue WhatsApp.
                    # interactive.product.id = product_retailer_id (ID WooCommerce).
                    product_id = interactive.get("product", {}).get("id")
                    text = f"produit:{product_id}" if product_id else None
            elif msg_type == "image":
                text = msg.get("image", {}).get("caption", "")
            elif msg_type == "button":
                text = msg.get("button", {}).get("text", "")

            # Champ "Répondre" de WhatsApp : le client répond à un message précis.
            # context.id = wamid du message auquel il répond (le plus souvent un
            # message SORTANT du bot — fiche produit envoyée, recommandation...).
            reply_to_id = msg.get("context", {}).get("id")

            # Produit référencé dans le contexte : le client a cliqué sur une
            # fiche produit du CATALOGUE dans la conversation (pas via
            # interactive/product, mais via context.referred_product).
            # Vu en prod 05/08 : "Disponible ?" + context.referred_product
            # (product_retailer_id=25286) — le bot répondait "de quel produit
            # parlez-vous ?" sans détecter le produit. On le traite comme un
            # produit envoyé par le client.
            referred = msg.get("context", {}).get("referred_product", {}) or {}
            referred_product_id = referred.get("product_retailer_id")
            if referred_product_id and not product_id:
                product_id = referred_product_id
                if not text or text.startswith("produit:"):
                    text = f"produit:{referred_product_id}"

            return {
                "from": msg.get("from"),
                "id": msg.get("id"),
                "text": text,
                "product_id": product_id,
                "reply_to_id": reply_to_id,
                "type": msg_type,
                "timestamp": msg.get("timestamp"),
                "name": value.get("contacts", [{}])[0].get("profile", {}).get("name", ""),
            }
        except (KeyError, IndexError, TypeError) as e:
            logger.warning(f"Impossible d'extraire le message du webhook: {e}")
            return None
