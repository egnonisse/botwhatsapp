"""Système de log complet — traque tout le cycle de vie d'un message.

Structure:
- logs/bot.log        : tout (rotatif, 10MB × 3 fichiers)
- logs/errors.log     : erreurs uniquement
- logs/metrics.jsonl  : métriques structurées (latence, origine, stage)
- logs/webhooks.log   : payloads bruts des webhooks (debug)
"""

import logging
import json
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from functools import wraps
from datetime import datetime, timezone


LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# ── Formatter structuré ───────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """Format: TIME LEVEL [MODULE] message | extra_json"""
    def format(self, record):
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        base = f"{ts} {record.levelname:<7} [{record.name}] {record.getMessage()}"
        if hasattr(record, "metrics") and record.metrics:
            base += " | " + json.dumps(record.metrics, ensure_ascii=False, default=str)
        return base


# ── Handlers ──────────────────────────────────────────────────

def _make_handler(filename: str, level: int, max_mb: int = 10, backups: int = 3):
    h = RotatingFileHandler(LOG_DIR / filename, maxBytes=max_mb * 1024 * 1024, backupCount=backups,
                            encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(StructuredFormatter())
    return h


# Logger racine
root = logging.getLogger()
root.setLevel(logging.DEBUG)

# Nettoyer les handlers existants
root.handlers.clear()

# Handler console (info+)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(StructuredFormatter())
root.addHandler(console)

# Handler fichier général (debug+)
root.addHandler(_make_handler("bot.log", logging.DEBUG))

# Handler erreurs (warning+)
root.addHandler(_make_handler("errors.log", logging.WARNING))

# Logger métriques dédié
metrics_logger = logging.getLogger("metrics")
metrics_logger.propagate = False
metrics_logger.setLevel(logging.INFO)
metrics_logger.addHandler(_make_handler("metrics.jsonl", logging.INFO, max_mb=5, backups=2))


# ── Logger webhook ─────────────────────────────────────────────

webhook_logger = logging.getLogger("webhook")
webhook_logger.propagate = False
webhook_logger.setLevel(logging.DEBUG)
webhook_logger.addHandler(_make_handler("webhooks.log", logging.DEBUG, max_mb=20, backups=2))


# ── Logger conversation (1 fichier par conv) ───────────────────

conv_loggers: dict[int, logging.Logger] = {}

def get_conv_logger(conv_id: int, wa_name: str = "") -> logging.Logger:
    """Logger dédié à une conversation. Format: logs/conv_<id>_<nom>.log"""
    if conv_id in conv_loggers:
        return conv_loggers[conv_id]

    safe_name = "".join(c for c in wa_name if c.isalnum() or c in " _-")[:20] if wa_name else str(conv_id)
    filename = f"conv_{conv_id}_{safe_name}.log"
    handler = _make_handler(filename, logging.DEBUG, max_mb=2, backups=1)

    logger = logging.getLogger(f"conv.{conv_id}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.addHandler(handler)

    conv_loggers[conv_id] = logger
    return logger


# ── Métriques structurées ──────────────────────────────────────

def log_metric(event: str, **kwargs):
    """Enregistre une métrique structurée.
    Ex: log_metric("llm_call", origin="micro", latency_ms=450, stage="recommandation")
    """
    record = logging.LogRecord("metrics", logging.INFO, "", 0, event, (), None)
    record.metrics = {"event": event, "timestamp": time.time(), **kwargs}
    metrics_logger.handle(record)


def log_webhook(payload: dict):
    """Enregistre un payload webhook brut."""
    webhook_logger.debug(json.dumps(payload, ensure_ascii=False, default=str))


# ─── Décorateur de timing ──────────────────────────────────────

def timed(event_name: str):
    """Décorateur qui log la durée d'une fonction async."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                ms = (time.perf_counter() - t0) * 1000
                log_metric(event_name, status="ok", latency_ms=round(ms, 1))
                return result
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                log_metric(event_name, status="error", latency_ms=round(ms, 1), error=str(e)[:200])
                raise
        return wrapper
    return decorator


# ─── Init ──────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
logger.info("=== BotWhatsApp logger initialisé ===")
