"""Cache catalogue WooCommerce → SQLite FTS5 pour recherche ultra-rapide.

- Synchro initiale : importe tous les produits WooCommerce
- Synchro delta : mise à jour périodique (toutes les heures)
- Recherche FTS5 : < 10ms sur 2000+ produits
"""

import json
import logging
import sqlite3
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from logger import log_metric

logger = logging.getLogger(__name__)


class CatalogCache:
    """Cache local SQLite FTS5 du catalogue WooCommerce."""

    def __init__(self, db_path: str = "data/catalog_cache.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._last_sync: Optional[datetime] = None

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """Crée les tables si elles n'existent pas."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT,
                    sku TEXT,
                    price REAL,
                    price_display TEXT,
                    stock_status TEXT,
                    stock_quantity INTEGER,
                    short_description TEXT,
                    permalink TEXT,
                    image_url TEXT,
                    categories TEXT,
                    tags TEXT,
                    updated_at TEXT
                );

                -- Table FTS5 pour recherche full-text
                CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
                    name,
                    sku,
                    short_description,
                    categories,
                    tags,
                    content='products',
                    content_rowid='id'
                );

                -- Triggers pour maintenir FTS5 synchronisé
                CREATE TRIGGER IF NOT EXISTS products_ai AFTER INSERT ON products BEGIN
                    INSERT INTO products_fts(rowid, name, sku, short_description, categories, tags)
                    VALUES (new.id, new.name, new.sku, new.short_description, new.categories, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS products_ad AFTER DELETE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, name, sku, short_description, categories, tags)
                    VALUES ('delete', old.id, old.name, old.sku, old.short_description, old.categories, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS products_au AFTER UPDATE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, name, sku, short_description, categories, tags)
                    VALUES ('delete', old.id, old.name, old.sku, old.short_description, old.categories, old.tags);
                    INSERT INTO products_fts(rowid, name, sku, short_description, categories, tags)
                    VALUES (new.id, new.name, new.sku, new.short_description, new.categories, new.tags);
                END;

                CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);
                CREATE INDEX IF NOT EXISTS idx_products_stock ON products(stock_status);
            """)
            logger.info("Cache catalogue initialisé")

    # ─── Synchro ──────────────────────────────────────────

    async def full_sync(self, woocommerce_catalog) -> int:
        """Synchro complète : vide le cache et réimporte tout WooCommerce."""
        logger.info("Démarrage synchro complète du catalogue...")
        start = datetime.now(timezone.utc)

        # Récupérer tous les produits WooCommerce (par lots de 100)
        all_products = []
        page = 1
        while True:
            try:
                batch = await woocommerce_catalog._get("products", {
                    "per_page": 100,
                    "page": page,
                    "status": "publish",
                })
                if not batch:
                    break
                all_products.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            except Exception as e:
                logger.error(f"Erreur synchro page {page}: {e}")
                break

        # Vider et réinsérer
        with self._get_conn() as conn:
            conn.execute("DELETE FROM products")
            for p in all_products:
                info = woocommerce_catalog.extract_product_info(p)
                conn.execute(
                    """INSERT INTO products (id, name, slug, sku, price, price_display,
                       stock_status, stock_quantity, short_description, permalink,
                       image_url, categories, tags, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        info["id"], info["name"],
                        p.get("slug", ""),
                        info["sku"],
                        float(info["price"]) if info["price"] else 0,
                        info["price_display"],
                        info["stock_status"],
                        info["stock_quantity"],
                        self._clean_html(info["description"]),
                        info["permalink"],
                        info["image_url"],
                        ", ".join(info["categories"]),
                        ", ".join(info["tags"]),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            conn.commit()

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        self._last_sync = datetime.now(timezone.utc)
        logger.info(f"Synchro terminée: {len(all_products)} produits en {elapsed:.1f}s")
        log_metric("catalog_sync", products=len(all_products), elapsed_s=round(elapsed, 1))
        return len(all_products)

    async def sync_if_needed(self, woocommerce_catalog, max_age_seconds: int = 3600):
        """Synchro si le cache date de plus d'une heure."""
        if self._last_sync is None or (
            datetime.now(timezone.utc) - self._last_sync
        ).total_seconds() > max_age_seconds:
            await self.full_sync(woocommerce_catalog)

    # ─── Recherche ────────────────────────────────────────

    def search(self, query: str, limit: int = 10,
               in_stock_only: bool = True) -> list[dict]:
        """Recherche FTS5 ultra-rapide. Aucun filtre budget.

        Anti-bruit : si la requête contient des tokens mais qu'AUCUN n'apparaît
        dans les noms des résultats, on considère qu'il s'agit d'un bruit
        ("un truc pour mon chien" -> montres connectées) et on retombe sur un
        échantillon diversifié au lieu de montrer des produits absurdes.
        """
        if not query or not query.strip():
            return self._get_diverse_sample(limit, None, in_stock_only)

        t0 = time.perf_counter()
        fts_query = self._escape_fts_query(query)

        with self._get_conn() as conn:
            sql = """
                SELECT p.*,
                       CASE WHEN p.stock_status='instock' THEN 1 ELSE 0 END AS sort_stock
                FROM products p
                JOIN products_fts fts ON p.rowid = fts.rowid
                WHERE products_fts MATCH ?
            """
            params = [fts_query]

            if in_stock_only:
                sql += " AND p.stock_status = 'instock'"

            # Exclure les produits sans prix (0 FCFA)
            sql += " AND p.price > 0"

            # Boost: les vrais produits en premier (SQL-safe)
            exact_match = query.strip().upper().replace("'", "''")  # échapper apostrophes SQL
            sql += f" ORDER BY CASE WHEN UPPER(p.name) LIKE '{exact_match}%' THEN 0 ELSE 1 END,"
            sql += " sort_stock DESC, rank LIMIT ?"
            params.append(limit)

            results = [dict(r) for r in conn.execute(sql, params).fetchall()]
            ms = (time.perf_counter() - t0) * 1000
            log_metric("fts5_search", query=query[:50], results=len(results), latency_ms=round(ms, 1))

        # Anti-bruit : les tokens de la requête doivent matcher le contenu des
        # résultats (nom OU description OU catégories). Ex: "mon chien" ->
        # tokens {chien} -> aucune montre ne contient "chien" -> fallback
        # échantillon diversifié. Attention : ne vérifier QUE le nom rejetait
        # les vrais frigos ("Samsung Side-by-Side 655L" n'a pas "frigo" dans
        # son nom mais dans sa description) — bug prod 05/08. Et les accents
        # doivent être ignorés ("refrigerateur" vs "réfrigérateur").
        if results:
            import re as _re
            tokens = [t for t in _re.findall(r"[a-zà-ÿ0-9]+", query.lower())
                      if len(t) > 2]
            if tokens:
                import unicodedata as _ud
                def _no_accents(s: str) -> str:
                    s = _ud.normalize("NFD", s)
                    return "".join(c for c in s if _ud.category(c) != "Mn")
                haystack = _no_accents(" ".join(
                    f"{r.get('name', '')} {r.get('short_description', '')} "
                    f"{r.get('categories', '')}".lower()
                    for r in results
                ))
                if not any(t in haystack for t in tokens):
                    logger.info(f"Recherche bruit détecté ('{query}') -> échantillon diversifié")
                    return self._get_diverse_sample(limit, None, in_stock_only)

        return results

    def search_by_intent(self, text: str, limit: int = 8) -> list[dict]:
        """Recherche avec nettoyage des mots parasites (pas de budget)."""
        import re
        query = re.sub(
            r'\b(budget|prix|max|fcfa|f\s*cfa|francs?|montre|photo|image|voir|couleur)\b',
            '', text, flags=re.I
        ).strip()
        return self.search(query, limit=limit)

    def get_by_id(self, product_id) -> Optional[dict]:
        """Lookup EXACT par ID produit. Aucune approximation possible."""
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return None
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
            return dict(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[dict]:
        """Lookup EXACT par slug (extrait d'une URL produit). Pas de recherche floue."""
        if not slug:
            return None
        slug = slug.strip().strip("/").lower()
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM products WHERE LOWER(slug) = ?", (slug,)).fetchone()
            if row:
                return dict(row)
            # Le permalink porte le slug canonique : filet de sécurité
            row = conn.execute(
                "SELECT * FROM products WHERE LOWER(permalink) LIKE ?", (f"%/{slug}/%",)
            ).fetchone()
            return dict(row) if row else None

    def top_products(self, query: str = "", limit: int = 10,
                     order: str = "desc", in_stock_only: bool = True) -> list[dict]:
        """Top-N produits triés par PRIX, pour recommander sans interroger le client.

        Utilisé quand le client ne donne aucun critère ("peu importe", "oui") :
        au lieu de reposer une question, le bot présente un classement.

        order="desc" : du plus cher au moins cher (haut de gamme d'abord)
        order="asc"  : du moins cher au plus cher (entrée de gamme d'abord)
        """
        direction = "DESC" if str(order).lower().startswith("d") else "ASC"
        t0 = time.perf_counter()

        with self._get_conn() as conn:
            params = []
            if query and query.strip():
                sql = """
                    SELECT p.* FROM products p
                    JOIN products_fts fts ON p.rowid = fts.rowid
                    WHERE products_fts MATCH ? AND p.price > 0
                """
                params.append(self._escape_fts_query(query))
            else:
                sql = "SELECT p.* FROM products p WHERE p.price > 0"

            if in_stock_only:
                sql += " AND p.stock_status = 'instock'"

            sql += f" ORDER BY p.price {direction} LIMIT ?"
            params.append(limit)

            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        ms = (time.perf_counter() - t0) * 1000
        log_metric("top_products", query=(query or "")[:50], order=direction,
                   results=len(rows), latency_ms=round(ms, 1))
        return rows

    def get_count(self) -> int:
        """Nombre total de produits dans le cache."""
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def get_stats(self) -> dict:
        """Statistiques du cache."""
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            in_stock = conn.execute(
                "SELECT COUNT(*) FROM products WHERE stock_status='instock'"
            ).fetchone()[0]
            return {
                "total": total,
                "in_stock": in_stock,
                "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            }

    # ─── Helpers ──────────────────────────────────────────

    def _get_all(self, limit: int, max_price: float = None,
                 in_stock_only: bool = True) -> list[dict]:
        """Retourne les produits populaires (fallback sans query)."""
        sql = "SELECT * FROM products WHERE 1=1"
        params = []

        if max_price is not None:
            sql += " AND price <= ?"
            params.append(max_price)

        if in_stock_only:
            sql += " AND stock_status = 'instock'"

        sql += " ORDER BY price ASC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _get_diverse_sample(self, limit: int, max_price: float = None,
                            in_stock_only: bool = True) -> list[dict]:
        """Retourne un échantillon diversifié — 1 produit par grande catégorie."""
        # Catégories majeures à montrer (dans l'ordre)
        major_categories = [
            "Smartphones", "TV", "iPhone", "Samsung", "Tablettes",
            "Audio", "Barres de Son", "Électroménager", "Ordinateurs",
            "Casques", "Montres", "Accessoires",
        ]

        results = []
        seen_ids = set()

        with self._get_conn() as conn:
            for cat in major_categories:
                if len(results) >= limit:
                    break
                sql = "SELECT * FROM products WHERE categories LIKE ?"
                params = [f"%{cat}%"]

                if max_price is not None:
                    sql += " AND price <= ?"
                    params.append(max_price)
                if in_stock_only:
                    sql += " AND stock_status = 'instock'"

                sql += " LIMIT 1"
                row = conn.execute(sql, params).fetchone()
                if row and row["id"] not in seen_ids:
                    results.append(dict(row))
                    seen_ids.add(row["id"])

            # Compléter avec des produits aléatoires si pas assez
            if len(results) < limit:
                remaining = limit - len(results)
                placeholders = ",".join(["?"] * len(seen_ids)) if seen_ids else "0"
                rows = conn.execute(
                    f"SELECT * FROM products WHERE id NOT IN ({placeholders}) AND stock_status='instock' ORDER BY RANDOM() LIMIT ?",
                    list(seen_ids) + [remaining]
                ).fetchall()
                for row in rows:
                    results.append(dict(row))

        return results

    @staticmethod
    def _escape_fts_query(query: str) -> str:
        """Échappe et formate une requête FTS5 avec OR pour plus de tolérance."""
        clean = query.replace('"', '').replace("'", "")
        terms = [t for t in clean.split() if len(t) > 1]
        if not terms:
            return clean
        return " OR ".join(f'"{t}"*' for t in terms[:10])  # max 10 termes

    @staticmethod
    def _clean_html(text: str) -> str:
        """Nettoie le HTML basique du texte."""
        import re
        # Enlever les balises HTML
        text = re.sub(r'<[^>]+>', ' ', text)
        # Enlever les entités HTML
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
        # Normaliser les espaces
        text = re.sub(r'\s+', ' ', text)
        return text.strip()[:500]  # Limiter à 500 caractères

    @staticmethod
    def format_for_llm(products: list[dict]) -> str:
        """Formate des produits pour le prompt LLM."""
        if not products:
            return "Aucun produit trouvé."

        lines = []
        for p in products:
            stock_qty = p.get("stock_quantity", 0)
            if p["stock_status"] == "instock":
                stock = f"✅ En stock ({stock_qty} dispo)"
            else:
                stock = "❌ Rupture"

            line = (
                f"- **{p['name']}** | {p['price_display']} | {stock}\n"
                f"  SKU: `{p['sku']}` | ID: {p['id']}\n"
                f"  Catégories: {p['categories']}\n"
                f"  {p['short_description']}"
            )
            if p.get("permalink"):
                line += f"\n  🔗 {p['permalink']}"
            lines.append(line)
        return "\n".join(lines)
