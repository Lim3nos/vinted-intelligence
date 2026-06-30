"""Router pour les paramètres calibrables et les actions admin."""

import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

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
