"""
Gestion des paramètres calibrables stockés dans system_settings.
Cache 60 secondes max pour éviter de surcharger la base.
"""

import time
import json
from typing import Any
from sqlalchemy.orm import Session
from sqlalchemy import text

# Cache in-memory : key -> (valeur convertie, expires_at monotonic)
_cache: dict[str, tuple[Any, float]] = {}
_CACHE_TTL = 60.0  # secondes

SCORE_WEIGHT_KEYS = {
    "score_weight_repetition",
    "score_weight_velocity",
    "score_weight_rarity",
    "score_weight_lifespan",
}


def _invalidate_cache(*keys: str) -> None:
    """Supprime les entrées du cache pour les clés données (ou tout le cache si vide)."""
    if keys:
        for k in keys:
            _cache.pop(k, None)
    else:
        _cache.clear()


def get_setting(key: str, db: Session) -> Any:
    """
    Lit une valeur de paramètre depuis system_settings.
    Cache 60 secondes maximum.
    Convertit automatiquement selon value_type :
        'integer' → int, 'decimal' → float, 'text' → str.

    Paramètres :
        key : clé du paramètre (ex. 'price_min_default')
        db  : session SQLAlchemy active

    Retourne la valeur convertie.
    Lève ValueError si la clé est inconnue.
    """
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None:
        value, expires_at = cached
        if now < expires_at:
            return value

    row = db.execute(
        text("SELECT value, value_type FROM system_settings WHERE key = :k"),
        {"k": key},
    ).fetchone()

    if not row:
        raise ValueError(f"Paramètre '{key}' introuvable dans system_settings")

    # psycopg2 désérialise automatiquement JSONB → Python natif
    raw = row.value

    if row.value_type == "integer":
        value = int(raw)
    elif row.value_type == "decimal":
        value = float(raw)
    else:  # 'text'
        value = str(raw)

    _cache[key] = (value, now + _CACHE_TTL)
    return value


def reset_all_settings(db: Session) -> list[dict]:
    """
    Copie default_value dans value pour toutes les lignes de system_settings.
    Appelé par POST /api/settings/reset.
    Invalide l'intégralité du cache immédiatement après.

    Retourne les settings réinitialisés.
    """
    db.execute(
        text(
            "UPDATE system_settings SET value = default_value, updated_at = NOW()"
        )
    )
    db.commit()
    _invalidate_cache()
    return _fetch_all_settings(db)


def update_settings(updates: dict, db: Session) -> list[dict]:
    """
    Met à jour plusieurs paramètres en une seule opération.

    Validations obligatoires :
    - Si un des 4 poids de scoring est modifié : la somme des 4 doit être
      exactement 100, sinon HTTPException 400.
    - price_min_default doit être strictement < price_max_default.
    - Toutes les valeurs numériques doivent être positives.

    Paramètres :
        updates : dict {key: new_value}
        db      : session SQLAlchemy active

    Retourne les settings mis à jour ou lève ValueError avec message explicite.
    """
    if not updates:
        return _fetch_all_settings(db)

    # Valider les valeurs positives
    for k, v in updates.items():
        if isinstance(v, (int, float)) and v < 0:
            raise ValueError(f"La valeur de '{k}' doit être positive.")

    # Validation de la somme des poids de scoring
    if SCORE_WEIGHT_KEYS & set(updates.keys()):
        current_weights = {
            k: int(
                db.execute(
                    text("SELECT value FROM system_settings WHERE key = :k"),
                    {"k": k},
                ).scalar()
            )
            for k in SCORE_WEIGHT_KEYS
        }
        # Fusionner avec les nouvelles valeurs
        merged = {**current_weights, **{k: int(v) for k, v in updates.items() if k in SCORE_WEIGHT_KEYS}}
        total = sum(merged.values())
        if total != 100:
            raise ValueError(
                f"La somme des 4 poids de scoring doit être exactement 100 "
                f"(actuelle : {total}). Valeurs : {merged}"
            )

    # Validation price_min < price_max
    price_keys = {"price_min_default", "price_max_default"}
    if price_keys & set(updates.keys()):
        new_min = updates.get("price_min_default") or int(
            db.execute(text("SELECT value FROM system_settings WHERE key = 'price_min_default'")).scalar()
        )
        new_max = updates.get("price_max_default") or int(
            db.execute(text("SELECT value FROM system_settings WHERE key = 'price_max_default'")).scalar()
        )
        if int(new_min) >= int(new_max):
            raise ValueError(
                f"price_min_default ({new_min}) doit être strictement inférieur "
                f"à price_max_default ({new_max})."
            )

    # Appliquer les mises à jour
    for k, v in updates.items():
        row = db.execute(
            text("SELECT value_type FROM system_settings WHERE key = :k"),
            {"k": k},
        ).fetchone()
        if not row:
            raise ValueError(f"Paramètre '{k}' inconnu.")

        # Sérialiser selon le type JSONB attendu
        if row.value_type == "text":
            json_val = json.dumps(str(v))
        else:
            json_val = json.dumps(v)

        db.execute(
            text(
                """
                UPDATE system_settings
                SET value = CAST(:val AS jsonb), updated_at = NOW()
                WHERE key = :k
                """
            ),
            {"val": json_val, "k": k},
        )
        _invalidate_cache(k)

    db.commit()
    return _fetch_all_settings(db)


def _fetch_all_settings(db: Session) -> list[dict]:
    """Retourne toutes les lignes de system_settings sous forme de liste de dicts."""
    rows = db.execute(
        text("SELECT key, category, label, value, default_value, value_type, updated_at FROM system_settings ORDER BY category, key")
    ).fetchall()
    return [
        {
            "key": r.key,
            "category": r.category,
            "label": r.label,
            "value": r.value,
            "default_value": r.default_value,
            "value_type": r.value_type,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
