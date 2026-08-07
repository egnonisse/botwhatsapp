"""Routeur rapide — décisions déterministes + micro-prompts (source unique de règles)."""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

from base_rules import BASE_RULES  # Source unique des règles — propagées automatiquement
from category_tracker import is_no_preference, resolve_search_query, wants_to_see_products

logger = logging.getLogger(__name__)


@dataclass
class FastResult:
    response: str
    new_stage: str = "qualification"
    context_update: dict = field(default_factory=dict)
    should_escalate: bool = False
    confidence: float = 0.95
    hit: bool = False


SALUTATIONS = [(r"^(bonjour|salut|bjr|hello|hi|yo|hey|coucou|slt)\b", "qualification")]

FAQ_TEMPLATES = {
    "prix_general": {"patterns": [r"\b(combien|quel.*prix|tarif|co[ûu]te?)\b"], "response": None, "stage": None},
    # Paiement AVANT livraison : "je paie à la livraison" contient "livraison"
    # mais c'est une réponse de PAIEMENT. Vérifié en premier (bug prod 05/08).
    "paiement": {"patterns": [r"\b(paiement|payer|paie|paye|paié|régler|regler|mobile money|orange money|moov|wave|espèces|especes|à la livraison|a la livraison)\b"],
                 "response": "Paiement à la livraison (espèces ou Mobile Money). Pas d'acompte. 💰", "stage": "qualification"},
    "livraison": {"patterns": [r"\b(livraison|délais?|delais?|delai|quand.*(?:reçois?|arrive|livré))\b"],
                  "response": "Livraison gratuite à Abidjan en 24-48h. Zones éloignées : 48-72h. 📦", "stage": "qualification"},
    "garantie": {"patterns": [r"\b(garantie|garanti|sav|panne|casse)\b"],
                 "response": "Tous nos produits sont garantis 1 an. Service après-vente à Abidjan. 🛠️", "stage": "qualification"},
    "merci": {"patterns": [r"\b(merci|thanks|parfait|super|nickel)\b"],
              "response": "Avec plaisir ! N'hésite pas si tu as d'autres questions. 😊", "stage": None},
}

CONFIRMATION_PATTERNS = [r"\b(oui|ok|vas-y|je prends?|valide|confirme|commande|go|allons-y)\b"]
IMAGE_PATTERNS = [r"\b(photo|image|montre|voir|couleur)\b"]


# ── Micro-prompts (sans règles — injectées au runtime depuis BASE_RULES) ──

MICRO_PROMPTS = {
    "qualification": """Tu es vendeur {company}.
{RULES}
Pose UNE question pour comprendre le besoin. Max 150 car.
new_stage: "recommandation" dès que le client donne un besoin, sinon "qualification".
Réponds UNIQUEMENT: {{"response":"...","new_stage":"recommandation","context_update":{{}},"should_escalate":false,"confidence":0.9}}""",

    "recommandation": """Tu es vendeur {company}.
{RULES}
Le client demande des produits. Voici le catalogue :
{catalog_context}
Réponds en 150 car max avec UNE SEULE intro courte : "Voici 3 congélateurs adaptés à ta famille 😊"
Ne liste PAS les produits toi-même (prix, liens, caractéristiques) : le système les envoie juste après.
Mets produit_id et produit_nom (le MEILLEUR choix) dans context_update.
new_stage: "closing" si le client confirme son intérêt (oui, ok, je prends...), sinon "recommandation".
Réponds UNIQUEMENT: {{"response":"...","new_stage":"recommandation","context_update":{{"produit_id":"ID","produit_nom":"nom"}},"should_escalate":false,"confidence":0.9}}""",

    "closing": """Tu es vendeur {company}.
{RULES}
Closing. Infos connues:
{session_state}
Confirme la commande ou demande l'info manquante (adresse/tel/nom). Max 150 car.
IMPORTANT : dès que le client donne son nom, son téléphone ou son adresse, mets-les
dans context_update : {{"nom":"...","telephone":"...","adresse":"..."}} (clés EXACTES nom/telephone/adresse).
new_stage: "closed_won" UNIQUEMENT si nom+tel+adresse+produit_id sont tous présents. Sinon "closing".
Réponds UNIQUEMENT: {{"response":"...","new_stage":"closing","context_update":{{"nom":"","telephone":"","adresse":""}},"should_escalate":false,"confidence":0.9}}""",

    "objection": """Tu es vendeur {company}.
{RULES}
Objection: "{user_text}"
Rassure, propose alternative. Max 150 car.
new_stage: "recommandation" si l'objection est levée et le client montre de l'intérêt, sinon "objection".
Réponds UNIQUEMENT: {{"response":"...","new_stage":"objection","context_update":{{}},"should_escalate":false,"confidence":0.9}}""",
}


class FastRouter:
    """Routeur rapide : règles → templates → micro-prompt → LLM."""

    def __init__(self, cache, conv_mgr, usp: str = ""):
        self.cache = cache
        self.conv_mgr = conv_mgr
        # Conditions réelles de la boutique (livraison, garantie, paiement) —
        # injectées dans les micro-prompts pour que le LLM ne les invente pas.
        self.usp = usp

    def _conditions_block(self) -> str:
        """Bloc 'conditions réelles' à injecter dans les prompts."""
        parts = []
        if self.usp:
            parts.append(f"\nConditions réelles (ne jamais inventer d'autres) :\n{self.usp}")
        # Paramètres boutique édités via dashboard (horaires, livraison, frais,
        # paiement, FAQ) — lus à CHAQUE appel pour appliquer sans redémarrage.
        try:
            from shop_settings import build_shop_block
            shop = build_shop_block()
            if shop:
                parts.append(f"\n{shop}")
        except Exception:
            pass
        return "\n".join(parts)

    def detect_intent(self, text: str, stage: str, context: dict) -> Optional[FastResult]:
        text_lower = text.lower().strip()

        for pattern, new_stage in SALUTATIONS:
            if re.search(pattern, text_lower):
                return FastResult(
                    response=f"{self._time_greeting()} ! Bienvenue chez Zariamall. Quel produit cherches-tu ? 😊",
                    new_stage="recommandation", hit=True)
        # 2. FAQ (uniquement si message court ou le pattern est le sujet principal)
        # Exception : "merci je prends ça" / "parfait je commande" = ACHAT, pas un
        # remerciement. Si le message contient une confirmation, on saute le
        # template "merci" (bug prod 05/08 : le client voulait acheter, le bot
        # répondait "Avec plaisir ! N'hésite pas...").
        is_confirmation = any(re.search(pat, text_lower) for pat in CONFIRMATION_PATTERNS)
        for key, faq in FAQ_TEMPLATES.items():
            if key == "merci" and is_confirmation:
                continue
            for pat in faq["patterns"]:
                if re.search(pat, text_lower):
                    # Stricte: <50 chars ou le pattern occupe >50% du message
                    if len(text_lower) < 50 or _faq_is_dominant(pat, text_lower):
                        if faq["response"]:
                            return FastResult(response=faq["response"],
                                              new_stage=faq["stage"] or stage, hit=True)
                        return None  # Signal: besoin LLM
                    # Sinon, ignorer ce match (faux positif probable)

        if is_confirmation:
            # Normaliser comme _send_response : le LLM peut stocker l'adresse
            # sous adresse/adresse_livraison/livraison (bug prod 05/08).
            ctx_address = (context.get("adresse") or context.get("adresse_livraison")
                           or context.get("livraison") or context.get("address") or "")
            if context.get("produit_id") and ctx_address:
                return FastResult(
                    response=f"Parfait ! Je finalise votre commande pour {context.get('produit_nom', 'votre produit')}. 📦",
                    new_stage="closing", context_update={"confirmation": "oui"}, hit=True)

        return None

    def get_micro_prompt(self, stage: str, user_text: str = "",
                         conv_id: int = None, company: str = "Zariamall") -> Optional[str]:
        prompt = MICRO_PROMPTS.get(stage)
        if not prompt:
            # Stage "decouverte" supprimé du pipeline (qualification → recommandation
            # directement). Si une conversation est encore à ce stage, on bascule
            # sur le micro-prompt recommandation.
            if stage == "decouverte":
                prompt = MICRO_PROMPTS.get("recommandation")
            if not prompt:
                return None

        # Injecter BASE_RULES au runtime (source unique, propagation automatique)
        prompt = prompt.replace("{company}", company)
        prompt = prompt.replace("{RULES}", BASE_RULES)
        # Injecter les conditions réelles (livraison/paiement/garantie) — le
        # LLM ne doit jamais les inventer (faille #5 corrigée).
        conditions = self._conditions_block()
        if conditions:
            # Insérer le bloc conditions APRÈS la première ligne ("Tu es vendeur X.")
            # — méthode robuste quel que soit le contenu du micro-prompt.
            first_nl = prompt.find("\n")
            if first_nl != -1:
                prompt = prompt[:first_nl] + conditions + prompt[first_nl:]

        ctx = self.conv_mgr.get_context(conv_id) if conv_id else {}
        category = ctx.get("_category")

        # Le client demande EXPLICITEMENT à voir les produits ("montre moi les
        # frigos") : on lui AFFICHE directement, sans question de qualification.
        # Vu en production 03/08 : 3x "Montre moi les frigos disponible" sans
        # jamais recevoir de produit. Aussi : un client en stage closing qui
        # demande de nouveaux produits doit REVENIR en recommandation, sinon le
        # micro-prompt closing répond "je vérifie... une minute !" sans rien.
        if wants_to_see_products(user_text):
            if stage in ("closing", "closed_won"):
                # Nouveau sujet : sortir du closing, revenir en recommandation
                prompt = MICRO_PROMPTS.get("recommandation", prompt)
                stage = "recommandation"
            query = resolve_search_query(user_text, category)
            products = self.cache.search(query, limit=8) if query else self.cache.search("", limit=8)
            if products:
                listing = self.cache.format_for_llm(products)
                prompt += (
                    f"\n\nLe client demande à VOIR les produits ({category or 'catalogue'}). "
                    f"Voici ce qui est disponible :\n{listing}\n"
                    "Présente-lui ces produits avec leurs prix. Ne repose PAS de question."
                )
                prompt = prompt.replace("{catalog_context}", listing)
            # Neutraliser l'instruction "Pose UNE question" du micro-prompt
            prompt = prompt.replace(
                "Pose UNE question pour préciser son besoin. Max 150 car.",
                "Le client veut VOIR les produits : présente-les IMMÉDIATEMENT. Max 150 car.")
            prompt = prompt.replace(
                "Pose UNE question pour comprendre le besoin. Max 150 car.",
                "Le client veut VOIR les produits : présente-les IMMÉDIATEMENT. Max 150 car.")
            prompt = prompt.replace(
                'new_stage: "decouverte" si le client répond avec un besoin, sinon "qualification".',
                'new_stage: "recommandation" (le client veut voir les produits).')

        # Le client ne donne aucun critère ("peu importe", "oui") : on lui présente
        # un classement au lieu de reposer une question. Sans ce bloc, le bot
        # rebouclait en questions (taille ? usage ? budget ? marque ?) et le
        # micro-prompt "decouverte" ne recevait AUCUN produit.
        if is_no_preference(user_text) and category:
            query = resolve_search_query(user_text, category)
            products = self.cache.top_products(query, limit=10, order="desc")
            if products:
                listing = self.cache.format_for_llm(products)
                prompt += (
                    f"\n\nLe client n'a donné aucun critère. Voici le TOP {len(products)} "
                    f"({category}) classé du plus cher au moins cher :\n{listing}\n"
                    "Présente-lui ce classement avec les prix. "
                    "Ne repose PAS de question de qualification."
                )
                prompt = prompt.replace("{catalog_context}", listing)

        if stage == "recommandation" and "{catalog_context}" in prompt:
            query = resolve_search_query(user_text, category)
            products = self.cache.search(query, limit=6) if query else self.cache.search("", limit=6)
            prompt = prompt.replace("{catalog_context}", self.cache.format_for_llm(products))

        if stage == "closing" and conv_id:
            lines = [f"{k}: {v}" for k, v in ctx.items() if not k.startswith("_") and v]
            prompt = prompt.replace("{session_state}", "\n".join(lines) or "En attente d'infos client.")

        if stage in ("decouverte", "objection"):
            prompt = prompt.replace("{user_text}", user_text[:200])

        return prompt

    @staticmethod
    def _time_greeting() -> str:
        from datetime import datetime
        h = datetime.now().hour
        if h < 12: return "Bonjour"
        if h < 18: return "Bon après-midi"
        return "Bonsoir"


def _faq_is_dominant(pattern: str, text: str) -> bool:
    """Vérifie que le pattern FAQ est le sujet principal du message (pas juste un mot parmi d'autres)."""
    match = re.search(pattern, text)
    if not match:
        return False
    matched = match.group()
    # Le texte matché doit représenter >30% du message total
    return len(matched) > len(text) * 0.3
