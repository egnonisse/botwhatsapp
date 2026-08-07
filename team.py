"""Gestion de l'équipe (numéros WhatsApp des agents) — notifications d'escalade.

Stockage : data/team.json
- name: nom de l'agent
- phone: numéro WhatsApp (format international, ex: +22548770834)
- active: True -> reçoit les alertes d'escalade
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TEAM_PATH = Path(__file__).parent / "data" / "team.json"


def load_team(team_path: Path = DEFAULT_TEAM_PATH) -> list[dict]:
    """Charge la liste des membres. Crée le fichier par défaut s'il n'existe pas."""
    if not team_path.exists():
        team_path.parent.mkdir(parents=True, exist_ok=True)
        team_path.write_text(json.dumps([], ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return []
    try:
        data = json.loads(team_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError) as e:
        logger.error(f"team.json illisible: {e}")
        return []


def save_team(team: list[dict], team_path: Path = DEFAULT_TEAM_PATH) -> None:
    team_path.parent.mkdir(parents=True, exist_ok=True)
    team_path.write_text(json.dumps(team, ensure_ascii=False, indent=2),
                         encoding="utf-8")


def add_member(name: str, phone: str, team_path: Path = DEFAULT_TEAM_PATH) -> dict:
    """Ajoute un membre (ou le réactive). Retourne le membre."""
    team = load_team(team_path)
    phone_norm = phone.strip().replace(" ", "")
    # Même numéro -> mettre à jour
    for m in team:
        if m.get("phone") == phone_norm:
            m["name"] = name.strip()
            m["active"] = True
            save_team(team, team_path)
            return m
    member = {"name": name.strip(), "phone": phone_norm, "active": True}
    team.append(member)
    save_team(team, team_path)
    logger.info(f"👥 Membre ajouté: {member['name']} ({member['phone']})")
    return member


def remove_member(phone: str, team_path: Path = DEFAULT_TEAM_PATH) -> bool:
    team = load_team(team_path)
    phone_norm = phone.strip().replace(" ", "")
    before = len(team)
    team = [m for m in team if m.get("phone") != phone_norm]
    if len(team) != before:
        save_team(team, team_path)
        logger.info(f"👥 Membre retiré: {phone_norm}")
        return True
    return False


def toggle_member(phone: str, team_path: Path = DEFAULT_TEAM_PATH) -> bool:
    """Active/désactive un membre. Retourne son nouvel état."""
    team = load_team(team_path)
    phone_norm = phone.strip().replace(" ", "")
    for m in team:
        if m.get("phone") == phone_norm:
            m["active"] = not m.get("active", True)
            save_team(team, team_path)
            logger.info(f"👥 Membre {m['name']} {'activé' if m['active'] else 'désactivé'}")
            return m["active"]
    return False


def active_phones(team_path: Path = DEFAULT_TEAM_PATH) -> list[str]:
    """Numéros des membres actifs (destinataires des alertes)."""
    return [m["phone"] for m in load_team(team_path) if m.get("active")]
