"""Module WooCommerce — recherche catalogue en temps réel via API REST.

Remplace le products.json statique par une connexion directe à WooCommerce.
"""

import logging
import httpx
import re
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class WooCommerceCatalog:
    """Catalogue WooCommerce connecté en temps réel."""

    def __init__(self, base_url: str, consumer_key: str, consumer_secret: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (consumer_key, consumer_secret)
        self._products_count = None

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Appel API WooCommerce avec gestion SSL + redirections."""
        url = f"{self.base_url}/wp-json/wc/v3/{endpoint}"
        if params is None:
            params = {}
        params.update({
            "consumer_key": self.auth[0],
            "consumer_secret": self.auth[1],
            "_fields": "id,name,slug,price,regular_price,stock_status,stock_quantity,sku,short_description,permalink,images,categories,tags",
        })
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_product_count(self) -> int:
        """Nombre total de produits."""
        if self._products_count is None:
            # Récupérer le header X-WP-Total
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
                params = {
                    "consumer_key": self.auth[0],
                    "consumer_secret": self.auth[1],
                    "per_page": 1,
                }
                resp = await client.get(f"{self.base_url}/wp-json/wc/v3/products", params=params)
                self._products_count = int(resp.headers.get("X-WP-Total", 0))
        return self._products_count

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Recherche full-text dans nom, description, catégories, tags."""
        # WooCommerce API supporte le paramètre 'search'
        results = await self._get("products", {"search": query, "per_page": limit})
        return results if isinstance(results, list) else []

    async def filter_by_category(self, category_id: int, limit: int = 20) -> list[dict]:
        """Produits d'une catégorie donnée."""
        return await self._get("products", {"category": category_id, "per_page": limit, "status": "publish"})

    async def filter_by_price(self, max_price: int, limit: int = 10) -> list[dict]:
        """Produits dans le budget."""
        return await self._get("products", {"max_price": str(max_price), "per_page": limit, "orderby": "price", "order": "desc"})

    async def get_by_id(self, product_id: int) -> Optional[dict]:
        """Récupère un produit par son ID WooCommerce."""
        try:
            return await self._get(f"products/{product_id}")
        except Exception:
            return None

    async def get_by_sku(self, sku: str) -> Optional[dict]:
        """Récupère un produit par son SKU."""
        results = await self._get("products", {"sku": sku, "per_page": 1})
        return results[0] if results else None

    async def create_order(self, customer_name: str, customer_phone: str,
                           customer_address: str = "",
                           items: list[dict] = None,
                           payment_method: str = "cod") -> dict:
        """Crée une commande dans WooCommerce.

        Args:
            customer_name: Nom du client
            customer_phone: Téléphone
            customer_address: Adresse de livraison
            items: [{"product_id": 123, "quantity": 1}, ...]
            payment_method: "cod" (paiement à la livraison), "bacs", etc.

        Returns:
            La commande créée avec son ID et statut
        """
        # Séparer prénom/nom si possible
        name_parts = customer_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        order_data = {
            "payment_method": payment_method,
            "payment_method_title": "Paiement à la livraison" if payment_method == "cod" else payment_method,
            "set_paid": False,
            "billing": {
                "first_name": first_name,
                "last_name": last_name,
                "address_1": customer_address or "Abidjan",
                "phone": customer_phone,
            },
            "shipping": {
                "first_name": first_name,
                "last_name": last_name,
                "address_1": customer_address or "Abidjan",
            },
            "line_items": items or [],
            "meta_data": [
                {"key": "_whatsapp_order", "value": "yes"},
                {"key": "_customer_whatsapp", "value": customer_phone},
            ],
        }

        result = await self._post("orders", order_data)
        order_id = result.get("id", 0)
        status = result.get("status", "pending")
        total = result.get("total", "0")
        logger.info(f"Commande #{order_id} créée: {status} — {total} FCFA pour {customer_name}")
        return result

    async def _post(self, endpoint: str, data: dict) -> dict:
        """POST vers l'API WooCommerce."""
        url = f"{self.base_url}/wp-json/wc/v3/{endpoint}"
        params = {
            "consumer_key": self.auth[0],
            "consumer_secret": self.auth[1],
        }
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=False) as client:
            resp = await client.post(url, params=params, json=data)
            resp.raise_for_status()
            return resp.json()

    async def recommend(self, query: str = "", budget: int = None,
                        category: str = None, limit: int = 6) -> list[dict]:
        """Recommande des produits selon critères combinés."""
        params = {"per_page": limit, "status": "publish", "orderby": "popularity", "order": "desc"}

        if query:
            params["search"] = query

        if budget:
            params["max_price"] = str(budget)

        if category:
            params["category"] = category  # WooCommerce accepte le slug

        results = await self._get("products", params)
        return results if isinstance(results, list) else []

    async def search_by_intent(self, text: str, limit: int = 8) -> list[dict]:
        """Recherche intelligente basée sur le message client.

        Extrait mot-clé + budget, puis interroge WooCommerce.
        """
        intent = self._extract_intent(text)
        params = {"per_page": limit, "status": "publish"}

        # Mot-clé principal (nettoie les mots vides)
        keywords = intent.get("query", text)
        # Enlever les mots "budget", "prix", "max", "fcfa", etc.
        cleaned = re.sub(r'\b(budget|prix|max|fcfa|f\s*cfa|francs?)\b', '', keywords, flags=re.I)
        cleaned = cleaned.strip()
        if cleaned:
            params["search"] = cleaned
        else:
            # Fallback: chercher dans le texte original
            params["search"] = text[:100]

        if intent.get("budget"):
            params["max_price"] = str(intent["budget"])

        if intent.get("category"):
            params["category"] = intent["category"]

        # Trier par prix croissant si budget, sinon par popularité
        if intent.get("budget"):
            params["orderby"] = "price"
            params["order"] = "asc"
        else:
            params["orderby"] = "popularity"

        results = await self._get("products", params)
        return results if isinstance(results, list) else []

    @staticmethod
    def _extract_intent(text: str) -> dict:
        """Extrait l'intention du message client (pas de budget — le LLM décide)."""
        return {"query": text, "budget": None, "category": None}

    @staticmethod
    def extract_product_info(product: dict) -> dict:
        """Extrait les infos essentielles d'un produit WooCommerce."""
        images = product.get("images", [])
        image_url = images[0]["src"] if images else ""

        # Prix formaté
        price = product.get("price", "") or "0"
        regular_price = product.get("regular_price", "") or "0"
        currency = "FCFA"

        try:
            price_float = float(price) if price else 0
        except (ValueError, TypeError):
            price_float = 0

        try:
            price_display = f"{int(price_float):,} {currency}".replace(",", " ")
        except (ValueError, TypeError):
            price_display = f"0 {currency}"

        # Catégories
        categories = [c["name"] for c in product.get("categories", [])]

        # Tags
        tags = [t["name"] for t in product.get("tags", [])]

        return {
            "id": product["id"],
            "name": product["name"],
            "sku": product.get("sku", ""),
            "price": str(price_float),
            "price_display": price_display,
            "description": product.get("short_description", ""),
            "image_url": image_url,
            "permalink": product.get("permalink", ""),
            "stock_status": product.get("stock_status", "instock"),
            "stock_quantity": product.get("stock_quantity", 0),
            "categories": categories,
            "tags": tags,
        }

    @staticmethod
    def format_for_llm(products: list[dict]) -> str:
        """Formate des produits pour le prompt LLM."""
        if not products:
            return "Aucun produit trouvé."

        lines = []
        for p in products:
            info = WooCommerceCatalog.extract_product_info(p)
            stock = "✅" if info["stock_status"] == "instock" else "❌"
            lines.append(
                f"- **{info['name']}** | {info['price_display']} | {stock}\n"
                f"  SKU: `{info['sku']}` | ID: {info['id']}\n"
                f"  Catégories: {', '.join(info['categories'])}\n"
                f"  {info['description']}"
            )
        return "\n".join(lines)

    @staticmethod
    def format_for_whatsapp(product: dict) -> str:
        """Formate un produit pour un message WhatsApp."""
        info = WooCommerceCatalog.extract_product_info(product)
        stock_emoji = "✅ En stock" if info["stock_status"] == "instock" else "❌ Rupture"
        return (
            f"📱 *{info['name']}*\n"
            f"💰 *{info['price_display']}*\n"
            f"📦 {stock_emoji}\n"
            f"🔗 {info['permalink']}"
        )
