"""
Logger persistant : écrit chaque log en console ET en table system_logs.
Ne jamais appeler log_to_db depuis un bloc except qui gère une erreur DB
(risque de boucle infinie).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from database.connection import SessionLocal

_console_logger = logging.getLogger("vinted")

if not _console_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    _console_logger.addHandler(_handler)
    _console_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


_LEVEL_MAP = {
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

VALID_LEVELS = {"INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_COMPONENTS = {
    "collector", "scheduler", "ai_clustering",
    "analyzer", "price_advisor", "jobs", "api",
}


def log_to_db(
    level: str,
    component: str,
    message: str,
    context: Optional[dict] = None,
) -> None:
    """
    Écrit un log en base de données ET en console.

    Paramètres :
        level     : 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
        component : composant source (collector, scheduler, …)
        message   : texte du log
        context   : dict JSON sérialisable avec des infos contextuelles optionnelles
    """
    level = level.upper()
    if level not in VALID_LEVELS:
        level = "INFO"

    # Log console toujours en premier
    _console_logger.log(
        _LEVEL_MAP.get(level, logging.INFO),
        "[%s] %s",
        component,
        message,
    )

    # Écriture en base dans une session dédiée (ne jamais réutiliser
    # la session du contexte appelant pour éviter les conflits de transaction)
    try:
        import json as _json
        db = SessionLocal()
        try:
            context_json = _json.dumps(context) if context else None
            db.execute(
                text(
                    """
                    INSERT INTO system_logs (level, component, message, context, created_at)
                    VALUES (:level, :component, :message, CAST(:context AS jsonb), :created_at)
                    """
                ),
                {
                    "level": level,
                    "component": component,
                    "message": message,
                    "context": context_json,
                    "created_at": datetime.now(timezone.utc),
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        # Ne pas logger en base si la base est en erreur — log console uniquement
        _console_logger.error(
            "[logger] Impossible d'écrire le log en base : %s", exc
        )
