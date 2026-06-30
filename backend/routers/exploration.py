"""Router pour l'exploration (jobs asynchrones) et la validation de clusters."""

import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from jobs import start_exploration_job, get_job_status

router = APIRouter(tags=["exploration"])


class ExplorationParams(BaseModel):
    search_type: str  # 'brand' | 'category'
    query: str
    price_min: int = 50
    price_max: int = 120
    filter_level: int = 3


class ValidateClusterBody(BaseModel):
    model_name: str
    keywords_rules: list
    search_id: Optional[int] = None


@router.post("/api/exploration/start", status_code=202)
async def start_exploration(
    body: ExplorationParams,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if body.search_type not in ("brand", "category"):
        raise HTTPException(400, "search_type doit être 'brand' ou 'category'")
    if not body.query.strip():
        raise HTTPException(400, "query ne peut pas être vide")
    if body.filter_level not in range(1, 6):
        raise HTTPException(400, "filter_level doit être entre 1 et 5")

    job_id = await start_exploration_job(body.model_dump(), background_tasks, db)
    return {"job_id": job_id}


@router.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str, db: Session = Depends(get_db)):
    return await get_job_status(job_id, db)


@router.post("/api/exploration/validate-cluster", status_code=201)
def validate_cluster(body: ValidateClusterBody, db: Session = Depends(get_db)):
    """
    Crée un product_model à partir d'un cluster validé par l'utilisateur
    et l'active en surveillance.
    """
    row = db.execute(
        text(
            """
            INSERT INTO product_models
                (name, keywords_rules, search_id, user_priority, is_active)
            VALUES (:name, CAST(:kw AS jsonb), :sid, 'normal', true)
            RETURNING *
            """
        ),
        {
            "name": body.model_name,
            "kw": json.dumps(body.keywords_rules),
            "sid": body.search_id,
        },
    ).fetchone()
    db.commit()
    return dict(row._mapping)
