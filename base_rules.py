"""Règles partagées — éditables depuis le dashboard. Rechargement automatique."""
import json, os

_RULES_FILE = "/opt/botwhatsapp/data/base_rules.json"
_DEFAULT = "Français. Tutoiement. 150 car max. Propose un produit, conclus."

def _read_rules():
    try:
        with open(_RULES_FILE, encoding="utf-8") as f:
            return json.load(f).get("rules", _DEFAULT)
    except Exception:
        return _DEFAULT

# Chargé au import
BASE_RULES = _read_rules()

def refresh_base_rules():
    """Recharge BASE_RULES depuis le JSON. Appelé par l'API dashboard."""
    global BASE_RULES
    BASE_RULES = _read_rules()
    return len(BASE_RULES)
