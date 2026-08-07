"""Gestionnaire de conversations avec persistance SQLite.

Gère les états de conversation, files d'attente de supervision,
et historique des messages.
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


class ConversationStatus(str, Enum):
    ACTIVE = "active"           # Bot actif, conversation en cours
    PENDING_REVIEW = "pending"   # En attente de validation humaine
    HUMAN_HANDLED = "human"      # Pris en main par un humain
    CLOSED = "closed"            # Conversation terminée
    BLOCKED = "blocked"          # Utilisateur bloqué/opt-out


class SalesStage(str, Enum):
    QUALIFICATION = "qualification"
    DECOUVERTE = "decouverte"
    RECOMMANDATION = "recommandation"
    OBJECTION = "objection"
    CLOSING = "closing"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


class ConversationManager:
    """Gère les conversations WhatsApp avec persistance SQLite."""

    def __init__(self, db_path: str = "data/conversations.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """Initialise les tables SQLite."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wa_phone TEXT NOT NULL UNIQUE,
                    wa_name TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    sales_stage TEXT DEFAULT 'qualification',
                    context_json TEXT DEFAULT '{}',
                    unread_count INTEGER DEFAULT 0,
                    needs_review INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
                    wa_message_id TEXT,
                    content TEXT NOT NULL,
                    content_type TEXT DEFAULT 'text',
                    metadata_json TEXT DEFAULT '{}',
                    reviewed INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conv_id
                    ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_conversations_status
                    ON conversations(status);
                CREATE INDEX IF NOT EXISTS idx_conversations_wa_phone
                    ON conversations(wa_phone);
            """)
            logger.info(f"Base de données initialisée: {self.db_path}")

    # ─── Conversations ────────────────────────────────────────

    def get_or_create_conversation(self, wa_phone: str, wa_name: str = "") -> dict:
        """Récupère ou crée une conversation pour un numéro WhatsApp."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE wa_phone = ?",
                (wa_phone,)
            ).fetchone()

            if row:
                # Mise à jour du nom si changé
                if wa_name and wa_name != row["wa_name"]:
                    conn.execute(
                        "UPDATE conversations SET wa_name = ?, updated_at = datetime('now') WHERE id = ?",
                        (wa_name, row["id"])
                    )
                    conn.commit()
                    # Re-fetch pour avoir les données à jour
                    row = conn.execute(
                        "SELECT * FROM conversations WHERE id = ?", (row["id"],)
                    ).fetchone()
                return dict(row)

            # Nouvelle conversation
            cursor = conn.execute(
                """INSERT INTO conversations (wa_phone, wa_name, status, sales_stage)
                   VALUES (?, ?, 'active', 'qualification')""",
                (wa_phone, wa_name)
            )
            conn.commit()
            return {
                "id": cursor.lastrowid,
                "wa_phone": wa_phone,
                "wa_name": wa_name,
                "status": "active",
                "sales_stage": "qualification",
                "context_json": "{}",
                "unread_count": 0,
                "needs_review": 0,
            }

    def get_conversation(self, wa_phone: str) -> Optional[dict]:
        """Récupère une conversation par numéro WhatsApp."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE wa_phone = ?",
                (wa_phone,)
            ).fetchone()
            return dict(row) if row else None

    def get_conversation_by_id(self, conv_id: int) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conv_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_status(self, conv_id: int, status: ConversationStatus):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (status.value, conv_id)
            )
            conn.commit()

    def update_sales_stage(self, conv_id: int, stage: SalesStage):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET sales_stage = ?, updated_at = datetime('now') WHERE id = ?",
                (stage.value, conv_id)
            )
            conn.commit()

    def update_context(self, conv_id: int, context: dict):
        """Met à jour le contexte de la conversation (données structurées)."""
        current = self.get_conversation_by_id(conv_id)
        if current:
            existing = json.loads(current.get("context_json", "{}"))
            existing.update(context)
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE conversations SET context_json = ?, updated_at = datetime('now') WHERE id = ?",
                    (json.dumps(existing, ensure_ascii=False), conv_id)
                )
                conn.commit()

    def get_context(self, conv_id: int) -> dict:
        conv = self.get_conversation_by_id(conv_id)
        return json.loads(conv.get("context_json", "{}")) if conv else {}

    def set_needs_review(self, conv_id: int):
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE conversations
                   SET needs_review = 1, status = 'pending', updated_at = datetime('now')
                   WHERE id = ?""",
                (conv_id,)
            )
            conn.commit()

    def clear_review(self, conv_id: int):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE conversations SET needs_review = 0, updated_at = datetime('now') WHERE id = ?",
                (conv_id,)
            )
            conn.commit()

    def get_pending_reviews(self) -> list[dict]:
        """Liste les conversations en attente de validation humaine."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE needs_review = 1 ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_active_conversations(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE status != 'closed' ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_conversations(self, limit: int = 100) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ─── Messages ────────────────────────────────────────────

    def add_message(self, conv_id: int, direction: str, content: str,
                    content_type: str = "text", wa_message_id: str = "",
                    metadata: dict = None, reviewed: bool = False) -> int:
        """Ajoute un message à la conversation. Retourne l'ID du message."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO messages (conversation_id, direction, wa_message_id,
                   content, content_type, metadata_json, reviewed)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    conv_id, direction, wa_message_id, content,
                    content_type, json.dumps(metadata or {}, ensure_ascii=False),
                    1 if reviewed else 0
                )
            )
            conn.execute(
                "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
                (conv_id,)
            )
            conn.commit()
            return cursor.lastrowid

    def get_messages(self, conv_id: int, limit: int = 50) -> list[dict]:
        """Récupère les derniers messages d'une conversation."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM messages WHERE conversation_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (conv_id, limit)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_message_by_wamid(self, wa_message_id: str) -> Optional[dict]:
        """Retrouve un message par son ID WhatsApp (wamid).

        Utilisé pour la fonctionnalité "Répondre" de WhatsApp : le client répond
        à un message précis, context.id contient le wamid de CE message. On
        retrouve le message sortant (et son produit via metadata_json).
        """
        if not wa_message_id:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE wa_message_id = ? ORDER BY id DESC LIMIT 1",
                (wa_message_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_message_wamid(self, message_id: int, wa_message_id: str,
                             metadata: dict = None) -> None:
        """Associe le wamid (retourné par l'API Meta APRÈS l'envoi) à un message.

        Le wamid n'est connu qu'après send_text ; on le stocke ensuite pour que
        la fonctionnalité "Répondre" puisse retrouver le message d'origine.
        """
        if not message_id or not wa_message_id:
            return
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE messages SET wa_message_id = ?, metadata_json = ? WHERE id = ?",
                (wa_message_id,
                 json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None
                 else "{}",
                 message_id)
            )
            conn.commit()

    def clear_conversation(self, conv_id: int):
        """Vide TOUS les messages et le contexte d'une conversation, sans la supprimer.
        
        Garde conv_id, wa_phone, wa_name intacts. Le bot repart a zero avec ce client.
        """
        with self._get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            conn.execute("UPDATE conversations SET context_json = '{}', sales_stage = 'qualification', needs_review = 0, updated_at = datetime('now') WHERE id = ?", (conv_id,))
            conn.commit()
        logger.info(f"Conversation #{conv_id} vidée (messages supprimés, contexte/stage réinitialisés)")


    def get_last_message(self, conv_id: int) -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
                (conv_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_unreviewed_messages(self, conv_id: int) -> list[dict]:
        """Messages sortants non encore validés par un humain."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM messages
                   WHERE conversation_id = ? AND direction = 'outgoing' AND reviewed = 0
                   ORDER BY created_at""",
                (conv_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def approve_message(self, message_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE messages SET reviewed = 1 WHERE id = ?", (message_id,))
            conn.commit()

    def get_history_for_llm(self, conv_id: int, max_messages: int = 20) -> list[dict]:
        """Formatte l'historique pour le LLM (role/content)."""
        messages = self.get_messages(conv_id, limit=max_messages)
        formatted = []
        for msg in messages:
            role = "user" if msg["direction"] == "incoming" else "assistant"
            formatted.append({"role": role, "content": msg["content"]})
        return formatted
