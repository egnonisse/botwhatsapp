"""Module catalogue produit — recherche et recommendations.

Utilise products.json comme source (pas besoin de l'API WhatsApp Commerce).
Lorsque l'app Meta sera publiée, on pourra basculer sur l'API native.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


class ProductCatalog:
    """Catalogue produits avec recherche par mot-clé, budget, catégorie."""

    def __init__(self, catalog_path: str = None):
        path = Path(catalog_path) if catalog_path else BASE_DIR / "products.json"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.name = data.get("name", "Catalogue")
        self.description = data.get("description", "")
        self.products = data.get("products", [])
        logger.info(f"Catalogue chargé: {len(self.products)} produits")

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Recherche full-text dans nom, description, catégorie, tags."""
        query_lower = query.lower()
        results = []
        for p in self.products:
            searchable = f"{p['name']} {p['description']} {p['category']} {' '.join(p.get('tags', []))}".lower()
            if query_lower in searchable:
                results.append(p)
        return results[:limit]

    def filter_by_budget(self, max_budget: int) -> list[dict]:
        """Produits dans le budget (prix ≤ max_budget)."""
        return sorted(
            [p for p in self.products if p.get("price_value", 0) <= max_budget],
            key=lambda p: p.get("price_value", 0),
            reverse=True,
        )

    def filter_by_category(self, category: str) -> list[dict]:
        """Produits d'une catégorie donnée."""
        cat_lower = category.lower()
        return [p for p in self.products if cat_lower in p.get("category", "").lower()]

    def get_by_id(self, product_id: str) -> Optional[dict]:
        """Récupère un produit par son ID."""
        for p in self.products:
            if p["id"] == product_id:
                return p
        return None

    def recommend(self, query: str = "", budget: int = None,
                  category: str = None, limit: int = 5) -> list[dict]:
        """Recommande des produits selon critères combinés.

        Ordre de priorité : budget → catégorie → recherche texte.
        """
        candidates = list(self.products)

        if budget:
            candidates = [p for p in candidates if p.get("price_value", 0) <= budget]

        if category:
            cat_lower = category.lower()
            candidates = [p for p in candidates if cat_lower in p.get("category", "").lower()]

        if query:
            query_lower = query.lower()
            scored = []
            for p in candidates:
                score = 0
                name_lower = p["name"].lower()
                desc_lower = p["description"].lower()
                tags_lower = " ".join(p.get("tags", [])).lower()

                if query_lower in name_lower:
                    score += 10
                if query_lower in tags_lower:
                    score += 5
                if query_lower in desc_lower:
                    score += 2
                if score > 0:
                    scored.append((score, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            candidates = [p for _, p in scored]

        return candidates[:limit]

    def format_for_llm(self, products: list[dict]) -> str:
        """Formate une liste de produits pour le prompt LLM (qualification/recommandation)."""
        if not products:
            return "Aucun produit trouvé."

        lines = []
        for p in products:
            lines.append(
                f"- **{p['name']}** | {p['price']} | {p.get('category', '')}\n"
                f"  {p['description']}\n"
                f"  ID: `{p['id']}`"
            )
        return "\n".join(lines)

    def format_for_whatsapp(self, product: dict) -> str:
        """Formate un produit pour un message WhatsApp (texte simple, sans API catalogue)."""
        return (
            f"📱 *{product['name']}*\n"
            f"💰 *{product['price']}*\n"
            f"📦 {product.get('availability', 'Disponible').replace('in stock', '✅ En stock').replace('out of stock', '❌ Rupture')}\n"
            f"\n{product['description']}"
        )

    def extract_from_message(self, text: str) -> dict:
        """Extrait les intentions d'achat d'un message client.

        Retourne: {query, budget, category} utilisable avec recommend().
        """
        import re
        result = {"query": text, "budget": None, "category": None}

        text_lower = text.lower()

        # Détection budget
        budget_patterns = [
            r"(\d[\d\s]*)\s*(?:fcfa|f cfa|f\.?cfa|francs?|f)",
            r"budget\s*(?:de\s*)?(\d[\d\s]*)",
            r"max\s*(\d[\d\s]*)",
            r"pas\s*(?:plus|dépasser)\s*(\d[\d\s]*)",
        ]
        for pattern in budget_patterns:
            match = re.search(pattern, text_lower)
            if match:
                try:
                    result["budget"] = int(match.group(1).replace(" ", ""))
                except ValueError:
                    pass
                break

        # Détection catégorie
        categories = {
            "iphone": "iPhone",
            "samsung": "Samsung",
            "galaxy": "Samsung",
            "xiaomi": "Xiaomi",
            "redmi": "Xiaomi",
            "tecno": "Tecno",
            "android": None,  # trop vague
            "apple": "iPhone",
            "téléphone": None,
            "smartphone": None,
            "ecouteurs": "Audio",
            "airpods": "Audio",
            "chargeur": "Accessoires",
            "coque": "Accessoires",
            "accessoire": "Accessoires",
            "audio": "Audio",
        }
        for keyword, cat in categories.items():
            if keyword in text_lower and cat:
                result["category"] = cat
                break

        return result
