"""Router pour les paramètres calibrables et les actions admin."""

import base64
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import text

from database.connection import get_db
from settings import update_settings, reset_all_settings, _fetch_all_settings
from analyzer import recalculate_all_scores
from logger import log_to_db

router = APIRouter(tags=["settings"])


class SettingsUpdateBody(BaseModel):
    updates: dict


@router.get("/api/settings")
def get_all_settings(db: Session = Depends(get_db)):
    return _fetch_all_settings(db)


@router.put("/api/settings")
def put_settings(body: SettingsUpdateBody, db: Session = Depends(get_db)):
    try:
        result = update_settings(body.updates, db)
        log_to_db("INFO", "api", "Paramètres mis à jour", {"keys": list(body.updates.keys())})
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/settings/reset")
def reset_settings(db: Session = Depends(get_db)):
    result = reset_all_settings(db)
    log_to_db("INFO", "api", "Paramètres réinitialisés aux valeurs d'origine")
    return result


@router.post("/api/admin/recalculate-scores")
def recalculate_scores(db: Session = Depends(get_db)):
    t0 = time.monotonic()
    count = recalculate_all_scores(db)
    duration = round(time.monotonic() - t0, 2)
    log_to_db(
        "INFO", "api",
        f"Recalcul des scores terminé — {count} modèles en {duration}s",
        {"recalculated": count, "duration_seconds": duration},
    )
    return {"recalculated_models": count, "duration_seconds": duration}


# ---------------------------------------------------------------------------
# Gestion du token Vinted authentifié (access_token_web + refresh_token_web)
# ---------------------------------------------------------------------------

def _decode_jwt_exp(token: str) -> Optional[int]:
    """Extrait l'expiration (Unix timestamp) d'un JWT sans validation de signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.b64decode(padded))
        return payload.get("exp")
    except Exception:
        return None


class VintedSessionBody(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None


@router.put("/api/settings/vinted-session")
def update_vinted_session(body: VintedSessionBody, db: Session = Depends(get_db)):
    """
    Stocke les tokens Vinted authentifiés (access_token_web + optionnel refresh_token_web).
    Appelez cet endpoint en collant les valeurs depuis les cookies navigateur (DevTools → Application).
    Le access_token a une durée de vie de ~2h ; le refresh_token dure ~30j.
    """
    exp = _decode_jwt_exp(body.access_token)
    if exp is None:
        raise HTTPException(400, "access_token invalide (JWT mal formé)")

    now_ts = int(time.time())
    if exp <= now_ts:
        raise HTTPException(400, f"access_token déjà expiré depuis {now_ts - exp}s — copiez un token frais depuis votre navigateur")

    minutes_left = (exp - now_ts) // 60
    # Stockage dans vinted_auth (table dédiée, colonnes nullable)
    db.execute(
        text("INSERT INTO vinted_auth (key, value, updated_at) VALUES ('access_token', :v, NOW()) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()"),
        {"v": body.access_token},
    )
    db.execute(
        text("INSERT INTO vinted_auth (key, value, updated_at) VALUES ('expires_at', :v, NOW()) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()"),
        {"v": str(exp)},
    )
    if body.refresh_token:
        db.execute(
            text("INSERT INTO vinted_auth (key, value, updated_at) VALUES ('refresh_token', :v, NOW()) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()"),
            {"v": body.refresh_token},
        )
    db.commit()

    log_to_db("INFO", "api", f"Token Vinted mis à jour — expire dans {minutes_left} min",
              {"exp": exp, "has_refresh": body.refresh_token is not None})
    return {
        "status": "ok",
        "expires_in_minutes": minutes_left,
        "has_refresh_token": body.refresh_token is not None,
    }


@router.get("/api/settings/vinted-session")
def get_vinted_session_status(db: Session = Depends(get_db)):
    """Retourne le statut du token Vinted stocké (validité, expiration)."""
    try:
        rows = db.execute(text("SELECT key, value FROM vinted_auth")).fetchall()
        data = {r.key: r.value for r in rows}
        exp_ts = int(data.get("expires_at") or 0)
        now_ts = int(time.time())
        return {
            "has_access_token": bool(data.get("access_token")),
            "has_refresh_token": bool(data.get("refresh_token")),
            "token_valid": exp_ts > now_ts,
            "expires_in_seconds": max(0, exp_ts - now_ts) if exp_ts else None,
            "expires_in_minutes": max(0, (exp_ts - now_ts) // 60) if exp_ts else None,
        }
    except Exception as e:
        import traceback
        detail = traceback.format_exc()
        log_to_db("ERROR", "api", f"vinted-session status error: {e}", {"traceback": detail})
        raise HTTPException(500, f"Erreur interne: {e}")
