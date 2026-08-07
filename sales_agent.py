"""Agent de vente conversationnelle intelligent.

Gère le pipeline de vente (qualification → closing) en utilisant
soit l'API Hermes locale, soit une API LLM directe (OpenAI-compatible).
"""

import json
import re
import logging
import asyncio
import time
import httpx
from typing import Optional
from datetime import datetime

from conversation_manager import SalesStage, ConversationManager
from woocommerce import WooCommerceCatalog
from catalog_cache import CatalogCache
from semantic_search import expand_query
from fast_router import FastRouter, FastResult
from base_rules import BASE_RULES
from logger import log_metric
from category_tracker import track_category, resolve_search_query, is_no_preference

logger = logging.getLogger(__name__)


def _truncate_smart(text: str, max_chars: int,
                    min_prefix_chars: int = 20, tail_margin: int = 40) -> str:
    """Tronque intelligemment : à la dernière phrase ou au dernier mot.

    Ne coupe JAMAIS à l'intérieur d'une URL (bug prod 04/08 : le bot envoyait
    "https://zariamal" — lien tronqué au milieu, client recevait un lien mort).
    Si une URL est présente, elle est TOUJOURS conservée complète : on coupe
    le texte avant/après elle, jamais dedans.
    """
    import re as _re
    if len(text) <= max_chars:
        return text

    urls = list(_re.finditer(r'https?://\S+', text))
    if not urls:
        return _truncate_by_sentence_or_space(text, max_chars)

    # Une URL présente : garder le préfixe jusqu'à l'URL (tronqué si long)
    # + l'URL complète, même si le total dépasse légèrement max_chars.
    first_url = urls[0]
    prefix = text[:first_url.start()]
    prefix = _truncate_by_sentence_or_space(prefix, max(min_prefix_chars, max_chars - tail_margin))
    result = prefix.rstrip() + " " + first_url.group(0)
    return result if len(result) <= max_chars + 60 else prefix.rstrip() + " …"


def _truncate_by_sentence_or_space(text: str, max_chars: int) -> str:
    """Tronque sans URL : à la dernière phrase, sinon au dernier mot."""
    if len(text) <= max_chars:
        return text
    cut = max(text.rfind('.', 0, max_chars - 2),
              text.rfind('!', 0, max_chars - 2),
              text.rfind('?', 0, max_chars - 2))
    if cut > max_chars // 2:  # au moins la moitié du texte utile
        return text[:cut + 1]
    cut = text.rfind(' ', 0, max_chars - 2)
    if cut > 20:
        return text[:cut] + "…"
    return text[:max_chars - 1] + "…"


# Prompt système de vente conversationnelle
SALES_SYSTEM_PROMPT = """Tu es vendeur pour {company}. Tu converses avec des clients via WhatsApp.

Règles :
""" + BASE_RULES + """

Contexte : {product_description}
Tarifs : {pricing}
Arguments : {usp}

Tu es à l'étape **{sales_stage}** du pipeline :
qualification → recommandation → objection → closing

Catalogue : {product_count} produits — {categories_list}
{catalog_context}

## État de la session
{session_state}

## Commande automatique
Pendant la conversation, collecte ces infos et mets-les dans context_update :
- "nom" : nom ou prénom du client
- "telephone" : numéro de téléphone
- "adresse" : adresse de livraison
- "produit_id" : l'ID WooCommerce du produit qui intéresse le client
- "produit_nom" : le nom du produit
- "quantite" : le NOMBRE d'exemplaires demandé (ex: 2 si le client dit "je veux 2"). Défaut 1.
Ne confirme JAMAIS une quantité supérieure au stock disponible (stock affiché dans session_state).
Dès que tu as nom + telephone + adresse + produit_id → le système créera automatiquement la commande.
{{"response":"...","new_stage":"qualification","context_update":{{}},"should_escalate":false,"confidence":0.9}}"""


class SalesAgent:
    """Agent de vente conversationnelle intégré au pipeline WhatsApp."""

    def __init__(self, config: dict, conv_mgr: ConversationManager):
        self.config = config
        self.conv_mgr = conv_mgr
        self.company = config.get("bot", {}).get("company", "Mon Entreprise")
        self.mode = config.get("bot", {}).get("mode", "hybrid")
        self.escalation_keywords = config.get("bot", {}).get(
            "human_escalation_keywords",
            ["humain", "parler à un conseiller", "urgent", "rappelle moi"]
        )
        self.product_desc = config.get("sales", {}).get("product_description", "À définir")
        self.pricing = config.get("sales", {}).get("pricing", "À définir")
        self.usp = "\n".join(
            f"- {p}" for p in config.get("sales", {}).get("unique_selling_points", ["À définir"])
        )

        # Tuning métier : valeurs de calibration ajustables sans redéploiement
        tuning = config.get("tuning", {})
        self.loop_similarity_threshold = tuning.get("loop_similarity_threshold", 0.75)
        self.loop_similarity_secondary = tuning.get("loop_similarity_secondary", 0.6)
        self.max_qualification_questions = tuning.get("max_qualification_questions", 3)
        self.min_outgoing_for_loop = tuning.get("min_outgoing_for_loop", 3)
        self.known_brands = tuning.get("known_brands", [])
        self.store_domain = tuning.get("store_domain", "zariamall.com")
        # Troncature : longueur minimale du préfixe / marge à réserver pour la fin
        self.truncate_min_prefix = tuning.get("truncate_min_prefix", 20)
        self.truncate_tail_margin = tuning.get("truncate_tail_margin", 40)

        # Config LLM
        hermes_cfg = config.get("hermes", {})
        self.hermes_url = hermes_cfg.get("api_url", "http://localhost:8001")
        self.model = hermes_cfg.get("model", "deepseek-v4-pro")
        self.max_history = hermes_cfg.get("max_history", 20)

        # Paramètres LLM configurables (plus de valeurs codées en dur)
        self.temperature = hermes_cfg.get("temperature", 0.3)
        self.max_tokens = hermes_cfg.get("max_tokens", 4000)
        self.llm_timeout = hermes_cfg.get("timeout", 10)
        self.retry_sleep = hermes_cfg.get("retry_sleep", 1)
        self.max_retries = hermes_cfg.get("max_retries", 2)
        self.max_response_chars = hermes_cfg.get("max_response_chars", 150)
        self.llm_api_url = hermes_cfg.get("api_url_llm", "https://api.deepseek.com/v1/chat/completions")

        # Auto-review threshold: si confidence >= ce seuil, envoi auto (mode hybride)
        self.auto_confidence_threshold = 0.85

        # Catalogue WooCommerce (temps réel) + Cache FTS5 local
        wc_cfg = config.get("woocommerce", {})
        self.catalog = WooCommerceCatalog(
            base_url=wc_cfg.get("url", ""),
            consumer_key=wc_cfg.get("consumer_key", ""),
            consumer_secret=wc_cfg.get("consumer_secret", ""),
        ) if wc_cfg.get("url") else None

        self.cache = CatalogCache() if self.catalog else None

        # Cache des métadonnées + routeur rapide
        self._cached_count = None
        self._cached_categories = None
        self.router = FastRouter(self.cache, self.conv_mgr, usp=self.usp) if self.cache else None

    async def _build_system_prompt(self, sales_stage: str, customer_intent: dict = None,
                                    conv_id: int = None) -> tuple[str, str]:
        """Construit le prompt système — retourne (prompt, origin: 'micro' ou 'full')."""

        # Utiliser un micro-prompt spécifique au stage si dispo
        if self.router:
            # Un client en stage closing qui ne CONFIRME pas sa commande (mais
            # demande d'autres produits, pose une question, précise un besoin)
            # doit revenir en recommandation AVANT le prompt. Sinon le micro-
            # prompt closing répond "je vérifie... une minute !" sans produits.
            prompt_stage = sales_stage
            if sales_stage == "closing" and customer_intent:
                import re as _re
                text_lower = customer_intent.get("query", "").lower()
                is_confirmation = any(
                    _re.search(p, text_lower)
                    for p in ["\b(oui|ok|vas-y|je prends?|valide|confirme|commande|go|allons-y)\b"]
                )
                if not is_confirmation:
                    prompt_stage = "recommandation"
                    logger.info("Stage closing sans confirmation -> prompt recommandation")

            micro = self.router.get_micro_prompt(
                prompt_stage,
                customer_intent.get("query", "") if customer_intent else "",
                conv_id,
                self.company,
            )
            if micro:
                context = self.conv_mgr.get_context(conv_id) if conv_id else {}
                if context:
                    micro += "\n" + json.dumps(context, ensure_ascii=False, indent=2)
                return micro, "micro"
        # Rechercher les produits pertinents via Cache FTS5 (ultra-rapide)
        catalog_text = "Catalogue en cours de chargement..."

        if self.cache and self.catalog:
            try:
                # Synchro si nécessaire (1ère fois ou >1h)
                await self.cache.sync_if_needed(self.catalog)

                ctx = self.conv_mgr.get_context(conv_id) if conv_id else {}
                category = ctx.get("_category")
                raw_query = customer_intent.get("query", "") if customer_intent else ""

                # La catégorie mémorisée permet de chercher même sans mot-clé produit
                # ("oui", "peu importe") — sinon FTS5 renvoyait 0 résultat.
                query = resolve_search_query(raw_query, category)

                if is_no_preference(raw_query) and category:
                    # Le client s'en remet au vendeur : on présente un classement
                    # au lieu de reposer une question.
                    products = self.cache.top_products(query, limit=10, order="desc")
                    catalog_text = self.cache.format_for_llm(products)
                    catalog_text += (
                        "\n\nLe client n'a donné aucun critère. Présente ce TOP "
                        f"{len(products)} classé du plus cher au moins cher, avec les prix. "
                        "Ne repose PAS de question de qualification."
                    )
                    log_metric("cache_lookup", source="top_products",
                               results=len(products), category=category or "?")
                else:
                    products = self.cache.search(query, limit=8) if query else self.cache.search("", limit=8)
                    # Si peu de résultats, tenter l'expansion sémantique
                    if len(products) < 3 and query:
                        expanded = expand_query(query)
                        products = self.cache.search(expanded, limit=8)
                    catalog_text = self.cache.format_for_llm(products)
                    log_metric("cache_lookup", source="cache", results=len(products))
            except Exception as e:
                logger.warning(f"Erreur cache: {e}, fallback WooCommerce")
                log_metric("cache_lookup", source="woocommerce_fallback", error=str(e)[:80])
                try:
                    products = await self.catalog.search(
                        customer_intent.get("query", "") if customer_intent else "", limit=8
                    )
                    catalog_text = self.catalog.format_for_llm(products)
                except Exception as e2:
                    logger.warning(f"Erreur WooCommerce: {e2}")
                    catalog_text = "Catalogue temporairement indisponible."

        return SALES_SYSTEM_PROMPT.format(
            company=self.company,
            product_description=self.product_desc,
            pricing=self.pricing,
            usp=self.usp,
            sales_stage=sales_stage,
            product_count=self._get_product_count(),
            categories_list=self._get_categories_list(),
            session_state=self._get_session_state(conv_id),
            catalog_context=catalog_text,
        ), "full"

    async def process_message(self, wa_phone: str, wa_name: str, text: str) -> dict:
        """Traite un message entrant et génère une réponse.

        Returns:
            {
                "response": str,
                "confidence": float,
                "needs_review": bool,
                "sales_stage": str,
                "should_escalate": bool,
            }
        """
        # 1. Récupérer ou créer la conversation
        conv = self.conv_mgr.get_or_create_conversation(wa_phone, wa_name)
        conv_id = conv["id"]
        sales_stage = conv.get("sales_stage", "qualification")

        # 2. Enregistrer le message entrant + suivre la catégorie
        self.conv_mgr.add_message(conv_id, "incoming", text, wa_message_id="")
        self._track_category(conv_id, text)

        # 3. Escalade immédiate (mots-clés humain)
        escalation = self._handle_escalation(conv_id, text)
        if escalation:
            return escalation

        # 4. Fast path : templates déterministes (<1ms, pas de LLM)
        fast = self._try_fast_path(conv_id, text, sales_stage)
        if fast:
            return fast

        # 5. Pipeline LLM complet (intention → prompt → appel → validation)
        customer_intent = self.catalog._extract_intent(text) if self.catalog else {
            "query": text, "budget": None, "category": None}
        history = self.conv_mgr.get_history_for_llm(conv_id, self.max_history)
        system_prompt, origin = await self._build_system_prompt(sales_stage, customer_intent, conv_id)

        processed = await self._run_llm_pipeline(
            conv_id, sales_stage, system_prompt, history, origin)
        if processed is None:
            return self._technical_failure(sales_stage)

        response_text, confidence, new_stage, should_escalate, context_update = processed
        needs_review = self._determine_review_needed(confidence, should_escalate, new_stage)

        # 6. Persister : contexte, stage, message sortant
        return self._persist_outcome(
            conv_id, sales_stage, response_text, confidence, new_stage,
            should_escalate, context_update, needs_review, origin)

    # ── Sous-étapes de process_message (responsabilité unique) ──────────

    def _track_category(self, conv_id: int, text: str) -> None:
        """Suit la catégorie évoquée ; purge le produit si le sujet change.

        Sans ça, le bot continuait à recommander des TV après que le client
        soit passé aux téléphones (constaté en production).
        """
        ctx_now = self.conv_mgr.get_context(conv_id)
        prev_category = ctx_now.get("_category")
        category, changed = track_category(text, prev_category)
        if category and category != prev_category:
            self.conv_mgr.update_context(conv_id, {"_category": category})
        if changed:
            # Nouveau sujet : le produit ciblé précédemment n'est plus pertinent.
            self.conv_mgr.update_context(conv_id, {"produit_id": "", "produit_nom": ""})
            logger.info(f"Changement de catégorie: {prev_category} -> {category}")
            log_metric("category_switch", from_cat=prev_category or "?", to_cat=category,
                       conv_id=conv_id)

    def _handle_escalation(self, conv_id: int, text: str) -> Optional[dict]:
        """Escalade immédiate si le client demande un humain."""
        if not self._detect_escalation(text):
            return None
        self.conv_mgr.set_needs_review(conv_id)
        self.conv_mgr.update_sales_stage(conv_id, SalesStage.CLOSING)
        return {
            "response": "Je comprends ! Je passe le relais à un conseiller qui va vous répondre dans quelques minutes. 😊",
            "confidence": 1.0,
            "needs_review": False,
            "sales_stage": "closing",
            "should_escalate": True,
        }

    def _try_fast_path(self, conv_id: int, text: str, sales_stage: str) -> Optional[dict]:
        """Templates déterministes (salutations, FAQ) sans appel LLM."""
        if not self.router:
            return None
        context = self.conv_mgr.get_context(conv_id)
        fast = self.router.detect_intent(text, sales_stage, context)
        if not fast or not fast.hit:
            return None
        if fast.context_update:
            self.conv_mgr.update_context(conv_id, fast.context_update)
        if fast.new_stage and fast.new_stage != sales_stage:
            try:
                self.conv_mgr.update_sales_stage(conv_id, SalesStage(fast.new_stage))
                sales_stage = fast.new_stage
            except ValueError:
                pass
        msg_id = self.conv_mgr.add_message(conv_id, "outgoing", fast.response, reviewed=True)
        return {
            "response": fast.response,
            "confidence": fast.confidence,
            "needs_review": False,
            "sales_stage": sales_stage,
            "should_escalate": fast.should_escalate,
            "conv_id": conv_id,
            "message_id": msg_id,
            "fast_path": True,
            "origin": "fast",
        }

    async def _run_llm_pipeline(self, conv_id: int, sales_stage: str,
                                system_prompt: str, history: list,
                                origin: str) -> Optional[tuple]:
        """Appel LLM + validation business + filtre anti-hallucination + régénération.

        Retourne (response_text, confidence, new_stage, should_escalate,
        context_update) ou None si échec définitif.
        """
        # Appeler le LLM avec mesure de latence
        t0_llm = time.perf_counter()
        llm_response = await self._call_llm(system_prompt, history)
        llm_ms = (time.perf_counter() - t0_llm) * 1000

        if llm_response:
            log_metric("llm_call", origin=origin, model=self.model, latency_ms=round(llm_ms, 0))

        if not llm_response:
            logger.error("Échec de l'appel LLM")
            return None

        # Valider le schéma JSON (clés obligatoires).
        # Fallback texte libre : le dict ne contient QUE "response" (le LLM a
        # répondu en français sans JSON). Les clés manquantes sont remplies
        # avec des valeurs par défaut — le stage reste inchangé, confidence
        # moyenne. La réponse est commercialement valable, on ne la jette pas.
        required_keys = ["response", "new_stage", "confidence"]
        missing = [k for k in required_keys if k not in llm_response]
        if missing:
            if "response" in llm_response and missing == ["new_stage", "confidence"]:
                llm_response["new_stage"] = sales_stage
                llm_response["confidence"] = 0.6
                logger.warning(f"Réponse texte libre: clés {missing} remplies par défaut")
            else:
                logger.warning(f"Réponse LLM invalide: clés manquantes {missing}")
                return None

        response_text = llm_response.get("response", "")
        confidence = llm_response.get("confidence", 0.5)
        new_stage = llm_response.get("new_stage", sales_stage)

        # Le code valide les décisions métier du LLM (séparation proposeur / décideur)
        new_stage, should_escalate, context_update = self._validate_business_rules(
            conv_id, new_stage, llm_response.get("should_escalate", False),
            llm_response.get("context_update", {}))

        # Filtre anti-hallucination : liens inventés et phrases interdites.
        # En cas de rejet, on relance le LLM avec une instruction de correction
        # (1 seule tentative), avant de baisser les bras.
        if not self._validate_response(response_text, new_stage):
            logger.warning("Réponse rejetée par _validate_response, régénération demandée")
            regen_hint = (
                "\n\nIMPORTANT : ta réponse précédente a été REJETÉE.\n"
                "Motifs possibles : lien inventé (utilise UNIQUEMENT les liens du catalogue, ou aucun lien), "
                "ou phrase interdite (ne dis jamais \"je ne peux pas envoyer de photo\").\n"
                "Réponds à nouveau, corrigé, au format JSON. Ne réutilise pas le lien rejeté."
            )
            t1 = time.perf_counter()
            llm_retry = await self._call_llm(system_prompt + regen_hint, history)
            retry_ok = (llm_retry and
                        self._validate_response(llm_retry.get("response", ""),
                                                llm_retry.get("new_stage", sales_stage)))
            if retry_ok:
                llm_response = llm_retry
                response_text = llm_response.get("response", "")
                confidence = llm_response.get("confidence", 0.5)
                new_stage = llm_response.get("new_stage", sales_stage)
                log_metric("llm_regen", origin=origin, model=self.model,
                           latency_ms=round((time.perf_counter() - t1) * 1000, 0))
                logger.info("Régénération acceptée après correction")
            else:
                logger.error("Régénération refusée à nouveau")
                return None

        return response_text, confidence, new_stage, should_escalate, context_update

    def _persist_outcome(self, conv_id: int, sales_stage: str, response_text: str,
                         confidence: float, new_stage: str, should_escalate: bool,
                         context_update: dict, needs_review: bool, origin: str) -> dict:
        """Persiste le résultat : contexte, stage, message sortant, review."""
        # 1. Mettre à jour le contexte
        if context_update:
            self.conv_mgr.update_context(conv_id, context_update)
        # Résoudre le stock du produit ciblé depuis le CACHE (le code décide,
        # pas le LLM) : le LLM peut proposer "2×" alors que le stock = 1
        # (bug prod 05/08 — Blackview Active 12 Pro). Le contexte garde la
        # quantité dispo pour le session_state et create_order.
        ctx_now = self.conv_mgr.get_context(conv_id)
        pid = ctx_now.get("produit_id")
        if pid and self.cache:
            try:
                prod = self.cache.get_by_id(int(pid))
                if prod:
                    qty = prod.get("stock_quantity") or 0
                    st = prod.get("stock_status", "")
                    self.conv_mgr.update_context(conv_id, {
                        "produit_stock": f"{'instock' if st == 'instock' else 'rupture'} (qty={qty})",
                        "produit_qty_max": int(qty) if qty else 0,
                    })
            except (ValueError, TypeError):
                pass
        # Stocker le stage actuel pour détection de progression
        self.conv_mgr.update_context(conv_id, {"_current_stage": new_stage})

        # 2. Mettre à jour le stage
        if new_stage != sales_stage:
            try:
                stage = SalesStage(new_stage)
                self.conv_mgr.update_sales_stage(conv_id, stage)
            except ValueError:
                pass

        # 3. Enregistrer le message sortant (non envoyé tant que pas validé si review)
        msg_id = self.conv_mgr.add_message(
            conv_id, "outgoing", response_text,
            reviewed=not needs_review
        )

        # 4. Si escalade, marquer pour review
        if should_escalate:
            self.conv_mgr.set_needs_review(conv_id)

        return {
            "response": response_text,
            "confidence": confidence,
            "needs_review": needs_review,
            "sales_stage": new_stage,
            "should_escalate": should_escalate,
            "conv_id": conv_id,
            "message_id": msg_id,
            "origin": origin,
        }

    def _technical_failure(self, sales_stage: str) -> dict:
        """Réponse d'échec technique uniforme (LLM indisponible, réponse invalide)."""
        return {
            "response": "Désolé, je rencontre un problème technique. Un conseiller va vous répondre rapidement.",
            "confidence": 0.0,
            "needs_review": True,
            "sales_stage": sales_stage,
            "should_escalate": True,
        }

    def _get_product_count(self) -> str:
        """Nombre de produits (caché)."""
        if self._cached_count is None and self.cache:
            self._cached_count = str(self.cache.get_count())
        return self._cached_count or "?"

    def _get_categories_list(self) -> str:
        """Liste des catégories (cachée, calculée une fois)."""
        if self._cached_categories is not None:
            return self._cached_categories
        try:
            if self.cache:
                with self.cache._get_conn() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT categories FROM products WHERE categories != ''"
                    ).fetchall()
                    all_cats = set()
                    for r in rows:
                        for c in r[0].split(", "):
                            if c:
                                all_cats.add(c)
                    self._cached_categories = ", ".join(sorted(all_cats)[:20])
                    return self._cached_categories
        except Exception:
            pass
    def _get_session_state(self, conv_id: int = None) -> str:
        """Bloc 'État de la session' : commandes, infos client, tutoiement."""
        if not conv_id:
            return "Nouvelle conversation."

        ctx = self.conv_mgr.get_context(conv_id)
        lines = []

        # Commandes déjà créées
        order_id = ctx.get("_order_created")
        if order_id:
            lines.append(f"Commande existante : #{order_id} ({ctx.get('_order_status', 'pending')})")

        # Infos client
        nom = ctx.get("nom", "")
        tel = ctx.get("telephone", "")
        adresse = ctx.get("adresse", "") or ctx.get("adresse_livraison", "")
        if nom:
            lines.append(f"Client : {nom}")
        if tel:
            lines.append(f"Téléphone : {tel}")
        if adresse:
            lines.append(f"Adresse : {adresse}")

        # Produit ciblé
        produit = ctx.get("produit_nom", "")
        if produit:
            lines.append(f"Produit visé : {produit}")
        # Stock du produit visé (si connu) : le LLM ne doit JAMAIS confirmer
        # une quantité supérieure au stock dispo (bug prod 05/08 : le bot a
        # promis 2× Blackview Active 12 Pro alors que le stock = 1).
        stock = ctx.get("produit_stock", "")
        if stock:
            lines.append(f"Stock disponible pour ce produit : {stock}")

        # Tutoiement
        lines.append("Tutoiement : oui (reste cohérent)")

        return "\n".join(lines) if lines else "Nouveau client."

    def _detect_escalation(self, text: str) -> bool:
        """Détecte si l'utilisateur demande explicitement un humain."""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self.escalation_keywords)

    def _validate_business_rules(self, conv_id: int, new_stage: str,
                                  should_escalate: bool,
                                  context_update: dict) -> tuple[str, bool, dict]:
        """Le code garde le dernier mot sur les décisions métier (pas le LLM).

        - new_stage: validé/bloqué selon l'état réel du contexte
        - should_escalate: déclenché aussi par le code (blocage >3 msg, mots négatifs)
        - context_update: nettoyé (pas de clés _underscore injectées par le LLM)
        """
        ctx = self.conv_mgr.get_context(conv_id)

        # ── 1. Stage : ne jamais closed_won sans les 4 infos ──
        if new_stage == "closed_won":
            if not (ctx.get("nom") and ctx.get("telephone") and
                    ctx.get("adresse") and ctx.get("produit_id")):
                new_stage = "closing"
                logger.info("Stage 'closed_won' refusé: infos incomplètes")

        # ── 1b. Stage : jamais closing sans produit ciblé ──
        # Vu en prod : le client demandait des congélateurs, la conversation
        # était restée en closing d'une commande précédente, et le LLM répondait
        # "je vérifie... une minute !" sans jamais montrer de produits.
        if new_stage == "closing" and not ctx.get("produit_id"):
            new_stage = "recommandation"
            logger.info("Stage 'closing' refusé: aucun produit ciblé -> recommandation")

        # ── 2. Stage : pas de régression sautée (objection → closing interdit) ──
        VALID_NEXT = {
            "qualification": ["recommandation"],
            "recommandation": ["closing", "objection", "qualification"],
            "objection": ["recommandation", "closing"],
            "closing": ["closed_won", "recommandation", "objection"],
            "closed_won": ["closed_won"],  # On ne peut plus reculer
        }
        current_stage = ctx.get("_current_stage", "qualification")
        allowed = VALID_NEXT.get(current_stage, ["qualification"])
        if new_stage not in allowed and new_stage != current_stage:
            logger.info(f"Stage {current_stage}→{new_stage} refusé (autorisé: {allowed})")
            new_stage = current_stage

        # ── 2b. Progression forcée : le LLM boucle en questions sans jamais recommander ──
        # Vu en prod : 4 questions d'affilée ("taille ?", "usage ?", "budget ?", "marque ?")
        # sans qu'aucun produit ne soit proposé. Le code tranche à la place du LLM.
        if new_stage in ("qualification",):
            msgs = self.conv_mgr.get_messages(conv_id, limit=12)
            questions = sum(
                1 for m in msgs
                if m["direction"] == "outgoing" and m["content"].rstrip().endswith("?")
            )
            if questions >= self.max_qualification_questions:
                new_stage = "recommandation"
                logger.info(f"Progression forcée → recommandation ({questions} questions posées sans reco)")

        # ── 3. Escalade : déclenchée par le code, pas seulement le LLM ──
        # 3a. Messages entrants CONSÉCUTIFS sans réponse du bot (vrai signe de blocage).
        #     On s'arrête au premier sortant : compter tous les entrants d'une fenêtre
        #     déclenchait une escalade sur toute conversation normale de 8 messages.
        recent_msgs = self.conv_mgr.get_messages(conv_id, limit=8)
        consecutive_incoming = 0
        for m in reversed(recent_msgs):
            if m["direction"] == "incoming":
                consecutive_incoming += 1
            else:
                break
        if consecutive_incoming >= 4:
            should_escalate = True
            logger.info(f"Escalade code: {consecutive_incoming} msg entrants consécutifs sans réponse")

        # 3b. Mots négatifs forts (frustration client)
        negative_patterns = ["arnaque", "escroc", "faux", "mensonge", "incompétent",
                             "nul", "pourri", "ne réponds pas", "tu comprends rien"]
        last_incoming = ""
        for m in reversed(recent_msgs):
            if m["direction"] == "incoming":
                last_incoming = m["content"].lower()
                break
        if any(p in last_incoming for p in negative_patterns):
            should_escalate = True
            logger.info("Escalade code: frustration détectée")

        # 3c. Anti-boucle: 3 responses quasi-identiques consécutives
        outgoing = [m["content"] for m in recent_msgs if m["direction"] == "outgoing"][-4:]
        if len(outgoing) >= self.min_outgoing_for_loop:
            # Comparer la similarité (mots communs / total)
            def similarity(a, b):
                wa, wb = set(a.lower().split()), set(b.lower().split())
                if not wa or not wb: return 0
                return len(wa & wb) / len(wa | wb)
            s1 = similarity(outgoing[-1], outgoing[-2])
            s2 = similarity(outgoing[-2], outgoing[-3])
            # Seuil calibré sur les vraies conversations : questions distinctes
            # mesurent 5-25% de similarité, une boucle réelle 65-100%.
            if s1 > self.loop_similarity_threshold and s2 > self.loop_similarity_secondary:
                should_escalate = True
                logger.info(f"Escalade code: boucle détectée (similarité {s1:.0%}/{s2:.0%})")

        # ── 4. Context_update : nettoyer les clés sensibles ──
        sanitized = {}
        for k, v in context_update.items():
            if not k.startswith("_"):  # Pas de clés réservées
                sanitized[k] = str(v)[:200]  # Max 200 car par valeur

        # ── 4b. produit_id : ne JAMAIS accepter un ID inventé par le LLM ──
        # Vu en prod : "produit_id":"HUM-ORAIMO-4L" (ID fantaisiste) créait
        # une commande vers un produit inexistant. Le code valide contre le
        # catalogue avant de mémoriser.
        if "produit_id" in sanitized:
            pid = sanitized["produit_id"]
            valid = False
            if pid.isdigit() and self.cache:
                valid = self.cache.get_by_id(int(pid)) is not None
            if not valid:
                logger.warning(f"produit_id invalide rejeté: {pid!r}")
                sanitized.pop("produit_id")
                sanitized.pop("produit_nom", None)  # le nom suit l'ID
        context_update = sanitized

        return new_stage, should_escalate, context_update

    def _validate_response(self, response_text: str, new_stage: str) -> bool:
        """Vérifications indépendantes du score de confiance du LLM.

        Retourne False si la réponse doit être rejetée/révisée.
        """
        # Vide ou trop court
        if not response_text or len(response_text) < 3:
            return False

        # Mots/phrases interdits (le LLM les ignore parfois)
        forbidden = [
            "je ne peux pas envoyer de photo",
            "je ne peux pas montrer",
            "je ne peux pas afficher",
            "pas de photo",
            "pas d'image",
            "je suis un assistant",
            "je suis une IA",
        ]
        resp_lower = response_text.lower()
        for phrase in forbidden:
            if phrase in resp_lower:
                logger.warning(f"Réponse contient phrase interdite: '{phrase}'")
                return False

        # Stage closing/closed_won mais réponse vide ou confuse
        if new_stage in ("closing", "closed_won") and len(response_text) < 10:
            return False

        # Liens markdown inventes [texte](url) -> hallucination LLM.
        # WhatsApp ne supporte pas le markdown, et le LLM invente ces liens.
        import re as _re2
        md_links = _re2.findall(r'\[([^\]]+)\]\(([^)]+)\)', response_text)
        if md_links:
            logger.warning(f"Liens markdown inventes bloques: {md_links[:3]}")
            return False

        # Promesse de lien sans lien fourni -> réponse incomplète.
        # Vu en prod 04/08 : "Voici le lien du Congélateur Samsung Bespoke 323L 😊"
        # sans aucun lien. Le client doit recevoir soit un vrai lien, soit rien.
        if re.search(r"(voici le lien|le lien (du|de la|de l')?|lien :|lien:)", response_text, re.I) \
           and self.store_domain not in response_text:
            logger.warning("Promesse de lien sans lien fourni, réponse rejetée")
            return False

        # Liens {store_domain} hors catalogue -> hallucination LLM.
        # Vu en production 03/08 : tv55, tv55-samsung, blackview-bv7200, ?s=iPhone+14.
        import re as _re
        fake_links = _re.findall(r'(?:https?://)?' + re.escape(self.store_domain) + r'/([^\s\)\]]+)', response_text)
        for link_path in fake_links:
            # 1. Format invalide (pas /produit/<slug>) -> hallucination certaine
            m = _re.match(r'produit/([^/]+)', link_path)
            if not m:
                logger.warning(f"Lien invente bloqué (format non-catalogue): {self.store_domain}/{link_path}")
                return False
            # 2. Slug inconnu du catalogue -> hallucination certaine
            slug = m.group(1)
            if self.cache and not self.cache.get_by_slug(slug):
                logger.warning(f"Lien invente bloqué: {self.store_domain}/produit/{slug}")
                return False
            # 3. Sans cache, on ne peut pas vérifier le slug -> on accepte le format

        # PRIX HALLUCINÉ : tout prix FCFA cité doit exister dans le catalogue.
        # Vu en prod : le LLM inventait des prix de tête (974 900 FCFA au lieu
        # du vrai). On vérifie qu'AU MOINS un produit du cache a ce prix.
        if self.cache:
            import re as _re_price
            prices = _re_price.findall(r'([\d][\d\s]{3,})\s*FCFA', response_text, re.I)
            for raw_price in prices:
                try:
                    p = int(re.sub(r'\s+', '', raw_price))
                except ValueError:
                    continue
                if not self._price_exists_in_catalog(p):
                    logger.warning(f"Prix halluciné bloqué: {p} FCFA")
                    return False

        # PRODUIT INEXISTANT cité en toutes lettres (sans lien, sans ID).
        # Vu en prod : "Voici le Galaxy A35..." — le produit n'existe pas au
        # catalogue. Détection par TOKEN MODÈLE : toute séquence lettres+chiffres
        # (a35, v50, tab a11, s25...) citée doit exister dans le catalogue.
        # Pas de liste de marques : "Galaxy A35" sans "Samsung" doit être attrapé.
        if self.cache:
            import re as _re_name
            # Mots grammaticaux qui ne sont JAMAIS des modèles produit.
            # "voici 3 congélateurs" -> "voici 3" n'est pas un modèle (faux
            # positif vu en prod 05/08 : réponse rejetée à tort).
            _GRAMMATICAL = {
                "voici", "les", "des", "une", "un", "le", "la", "de", "du",
                "et", "ou", "pour", "avec", "sans", "dans", "sur", "sous",
                "que", "qui", "quoi", "est", "sont", "plus", "moins", "tres",
                "trop", "prix", "fcfa", "cest", "cote", "chez", "entre",
                "compte", "reponds", "dis", "dites", "dit", "tu", "vous", "je",
                # Prépositions : "à 299 900 FCFA", "a 299" ne sont jamais des
                # modèles produit (faux positif vu en prod 05/08 : réponse
                # rejetée car "à 299" était cherché dans le catalogue).
                "a", "à", "au", "aux", "en", "par", "vers", "depuis", "jusque",
                # Verbes de vente : "propose 2", "donne 3", "montre 4" ne sont
                # jamais des modèles produit.
                "propose", "proposes", "donne", "donnes", "montre", "montres",
                "envoie", "envoies", "cherche", "cherches", "veux", "voudrais",
                "besoin", "prends", "prend", "choisis", "vois", "voit",
                # Infos livraison : "livraison 24 48h" n'est pas un modèle
                # (faux positif vu en prod 05/08 : question délais de livraison
                # -> réponse rejetée car "livraison 24 48h" cherché au catalogue).
                "livraison", "livre", "livres", "delai", "delais",
                "delai de livraison", "delais de livraison", "heures", "heure",
                "jours", "jour", "semaine", "semaines",
            }
            # Tokens modèles : lettres suivies de chiffres (a35, v50, s25, tab a11)
            model_tokens = _re_name.findall(
                r"\b([a-zà-ÿ]+[\s-]?\d[\w\-]*)\b", resp_lower)
            # + tokens avec chiffres isolés accolés (ex: "tab a11" -> a11)
            model_tokens += _re_name.findall(r"\b(\d+[\w\-]*)\b", resp_lower)
            for mt in set(model_tokens):
                mt_clean = re.sub(r"[\s-]+", " ", mt).strip()
                if len(mt_clean) < 2:
                    continue
                # Chiffres purs isolés : JAMAIS des modèles produit. "299 900
                # FCFA" (prix), "2 options", "TV 55 pouces" -> ignorés. Un vrai
                # modèle a des lettres (a35, v50) ou une unité (300L, 4K).
                if mt_clean.isdigit():
                    continue
                # Durées (24h, 48h, 24 48h, 48 72h) : "en combien de temps
                # puis-je être livré ?" -> "48h" / "24 48h" ne sont pas des
                # modèles produit (faux positif vu en prod 05/08 : réponse
                # rejetée à tort). Les vrais tokens type "30000mah" gardent
                # des lettres avant le h.
                if _re_name.fullmatch(r"[\d ]+h", mt_clean):
                    continue
                # Exclure les faux modèles : le mot devant le chiffre est
                # grammatical ("voici 3", "les 2", "une 4K") -> pas un modèle.
                parts = mt_clean.split(" ")
                if len(parts) >= 2 and parts[0] in _GRAMMATICAL:
                    continue
                hits = self.cache.search(mt_clean, limit=3)
                exists = False
                for h in hits:
                    name_tokens = set(_re_name.findall(r"[a-zà-ÿ0-9]+", h["name"].lower()))
                    overlap = name_tokens & set(_re_name.findall(r"[a-zà-ÿ0-9]+", mt_clean))
                    model_in_overlap = [t for t in overlap if any(ch.isdigit() for ch in t)]
                    if model_in_overlap:
                        exists = True
                        break
                if not exists:
                    logger.warning(f"Produit cité inexistant bloqué: '{mt_clean}'")
                    return False

        return True

    def _price_exists_in_catalog(self, price: int) -> bool:
        """Vrai si au moins un produit du cache a ce prix (tolérance ±1%)."""
        try:
            with self.cache._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM products WHERE ABS(price - ?) <= MAX(? * 0.01, 50)",
                    (price, price)).fetchone()
                return (row["n"] if row else 0) > 0
        except Exception:
            return True  # en cas d'erreur, ne pas bloquer

    def _determine_review_needed(self, confidence: float, should_escalate: bool,
                                  new_stage: str) -> bool:
        """Détermine si la réponse nécessite une validation humaine."""
        if self.mode == "assist":
            return True  # Toujours review en mode assistance
        if self.mode == "auto":
            return False  # Jamais review en mode auto
        if should_escalate:
            return True
        # Mode hybride: review si confidence faible ou stage critique
        if confidence < self.auto_confidence_threshold:
            return True
        if new_stage in ("closing", "closed_won", "closed_lost"):
            return True  # Toujours valider les clôtures
        return False

    async def _call_llm(self, system_prompt: str, history: list[dict]) -> Optional[dict]:
        """Appelle DeepSeek directement (Hermes désactivé car indisponible)."""
        api_key = self.config.get("deepseek_api_key", "")
        if not api_key:
            logger.warning("Pas de clé API DeepSeek configurée")
            return None

        # Note: l'historique contient user/assistant alternés.
        # On ajoute un message "user" final pour forcer le format JSON.
        # Le LLM tolère 2 messages "user" consécutifs (le dernier est une instruction système).
        messages = [{"role": "system", "content": system_prompt}] + history
        messages.append({
            "role": "user",
            "content": "Réponds au format JSON demandé."
        })

        # Retry: N tentatives avec backoff
        last_error = None
        for attempt in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.llm_timeout) as client:
                    resp = await client.post(
                        self.llm_api_url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "max_tokens": self.max_tokens,
                        }
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return self._parse_json_response(content)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    logger.warning(f"LLM tentative {attempt+1} echouee: {type(e).__name__}: {e}, retry dans {self.retry_sleep}s...")
                    await asyncio.sleep(self.retry_sleep)

        logger.error(f"LLM echoue apres {self.max_retries} tentatives: {last_error}")
        return None

    def _parse_json_response(self, content: str) -> Optional[dict]:
        """Parse la réponse JSON du LLM (mode JSON natif — pas de markdown/reasoning).

        Si le LLM répond en texte libre français (sans JSON), on l'accepte en
        fallback : le texte devient "response", les clés manquantes seront
        remplies par le pipeline avec des valeurs par défaut. Vu en prod
        05/08 : "Oui, le Samsung WA80CG4240BWNQ est disponible à 299 900
        FCFA..." (réponse parfaite) jetée car non-JSON -> échec technique.
        """
        content = content.strip()
        # DeepSeek retourne parfois {{...}} au lieu de {...} quand le prompt
        # contient des accolades echappees. On nettoie avant parsing.
        if content.startswith("{{") and content.endswith("}}"):
            content = content[1:-1]
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end > start:
                try:
                    result = json.loads(content[start:end+1])
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON parsing failed: {e} | content[:200]={content[:200]}")
                    content_clean = content[start:end+1].replace("''", "'")
                    try:
                        result = json.loads(content_clean)
                    except json.JSONDecodeError:
                        return self._fallback_plain_text(content)
            else:
                return self._fallback_plain_text(content)

        if "response" in result:
            result["response"] = result["response"].replace("''", "'")
            result["response"] = _truncate_smart(result["response"], self.max_response_chars,
                                                 self.truncate_min_prefix, self.truncate_tail_margin)

        return result

    def _fallback_plain_text(self, content: str) -> Optional[dict]:
        """Fallback quand le LLM répond en texte libre au lieu de JSON.

        La réponse est commercialement correcte mais sans le format JSON
        demandé (new_stage, confidence...). On l'accepte : le pipeline remplit
        les clés manquantes avec des valeurs par défaut (stage inchangé,
        confidence moyenne). Un texte trop court ou vide reste un échec.
        """
        content = content.strip()
        # Rejeter les réponses vides, les erreurs techniques ou les sorties
        # de raisonnement (pas des réponses commerciales).
        if len(content) < 10:
            logger.error(f"JSON fallback total failure. Raw content[:300]: {content[:300]}")
            return None
        lower = content.lower()
        for noise in ("error:", "exception", "traceback", "```", "system:", "assistant:"):
            if noise in lower:
                logger.error(f"JSON fallback total failure (bruit). Raw content[:300]: {content[:300]}")
                return None
        # Acceptable : réponse texte libre du LLM (ex: "Oui, le Samsung ... est
        # disponible à 299 900 FCFA..."). Les clés manquantes seront remplies
        # par le pipeline (stage inchangé, confidence 0.6).
        logger.warning(f"LLM a répondu en texte libre (JSON absent), fallback accepté: {content[:120]}")
        return {"response": content}
