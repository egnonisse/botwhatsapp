"""Paramétrage boutique éditable via dashboard — infos injectées dans les prompts.

Stockage : data/shop_settings.json
Le LLM répond avec les VRAIES conditions (horaires, livraison, paiement, FAQ)
au lieu d'inventer. Éditable via dashboard -> /api/shop-settings.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = Path(__file__).parent / "data" / "shop_settings.json"

DEFAULTS = {
    "horaires": "Lundi-Samedi 8h-19h, Dimanche 10h-17h",
    "livraison": {
        "zones": "Abidjan et intérieur du pays",
        "delais": "24-48h sur Abidjan, 2-4 jours pour l'intérieur",
        "frais": "Gratuit sur Abidjan dès 50 000 FCFA d'achat, sinon 2 000 FCFA. "
                 "Intérieur : 3 000 à 5 000 FCFA selon la ville.",
    },
    "paiement": [
        "Cash à la livraison",
        "Orange Money",
        "Wave",
    ],
    "garantie": "1 an sur tous les téléphones, 6 mois sur les accessoires",
    "faq": [
        {"q": "Comment payer ma commande ?",
         "a": "Paiement à la livraison en cash, ou par Orange Money / Wave."},
        {"q": "Quels sont les délais de livraison ?",
         "a": "24-48h sur Abidjan, 2-4 jours pour l'intérieur."},
        {"q": "Où êtes-vous situés ?",
         "a": "Nous livrons partout en Côte d'Ivoire depuis Abidjan."},
    ],
}


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> dict:
    """Charge les paramètres boutique. Crée avec les défauts si absent."""
    if not path.exists():
        save_settings(DEFAULTS, path)
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("structure invalide")
        # Fusionner avec les défauts (clés ajoutées plus tard)
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in data.items() if v is not None})
        return merged
    except (ValueError, OSError) as e:
        logger.error(f"shop_settings.json illisible: {e} — retour aux défauts")
        return dict(DEFAULTS)


def save_settings(settings: dict, path: Path = DEFAULT_SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    logger.info("🏪 Paramètres boutique sauvegardés")


def build_shop_block(settings: dict = None) -> str:
    """Bloc texte 'Infos boutique' injecté dans les prompts LLM.

    Le LLM répond avec ces infos exactes au lieu d'inventer les conditions.
    """
    s = settings or load_settings()
    lines = []
    if s.get("horaires"):
        lines.append(f"- Horaires : {s['horaires']}")
    liv = s.get("livraison") or {}
    if liv.get("zones"):
        lines.append(f"- Zones de livraison : {liv['zones']}")
    if liv.get("delais"):
        lines.append(f"- Délais : {liv['delais']}")
    if liv.get("frais"):
        lines.append(f"- Frais de livraison : {liv['frais']}")
    if s.get("garantie"):
        lines.append(f"- Garantie : {s['garantie']}")
    paiement = s.get("paiement") or []
    if paiement:
        lines.append(f"- Paiement accepté : {', '.join(paiement)}")
    faq = s.get("faq") or []
    if faq:
        lines.append("- FAQ :")
        for item in faq[:6]:  # max 6 entrées pour ne pas gonfler le prompt
            q = (item.get("q") or "").strip()
            a = (item.get("a") or "").strip()
            if q and a:
                lines.append(f"  * {q} → {a}")
    if not lines:
        return ""
    return "Infos boutique (réponds UNIQUEMENT avec ces infos, ne les invente pas) :\n" + "\n".join(lines)
