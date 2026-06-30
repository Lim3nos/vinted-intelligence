"""Router CRUD pour les recherches surveillées."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from collector import run_snapshot
from logger import log_to_db

router = APIRouter(prefix="/api/searches", tags=["searches"])


class SearchCreate(BaseModel):
    name: str
    search_type: str
    keywords: str
    brand_ids: Optional[str] = None
    catalog_ids: Optional[str] = None
    price_min: int = 50
    price_max: int = 120
    snapshot_interval_hours: int = 3


class SearchUpdate(BaseModel):
    name: Optional[str] = None
    keywords: Optional[str] = None
    brand_ids: Optional[str] = None
    catalog_ids: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    snapshot_interval_hours: Optional[int] = None


@router.get("")
def list_searches(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM searches ORDER BY created_at DESC")).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("", status_code=201)
def create_search(body: SearchCreate, db: Session = Depends(get_db)):
    if body.search_type not in ("brand", "category", "model"):
        raise HTTPException(400, "search_type doit être 'brand', 'category' ou 'model'")
    row = db.execute(
        text(
            """
            INSERT INTO searches (name, search_type, keywords, brand_ids, catalog_ids,
                                  price_min, price_max, snapshot_interval_hours, is_active)
            VALUES (:name, :stype, :kw, :bids, :cids, :pmin, :pmax, :interval, true)
            RETURNING *
            """
        ),
        {
            "name": body.name, "stype": body.search_type, "kw": body.keywords,
            "bids": body.brand_ids, "cids": body.catalog_ids,
            "pmin": body.price_min, "pmax": body.price_max,
            "interval": body.snapshot_interval_hours,
        },
    ).fetchone()
    db.commit()
    return dict(row._mapping)


@router.put("/{search_id}")
def update_search(search_id: int, body: SearchUpdate, db: Session = Depends(get_db)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Aucun champ à mettre à jour")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["sid"] = search_id
    row = db.execute(
        text(f"UPDATE searches SET {sets} WHERE id = :sid RETURNING *"),
        fields,
    ).fetchone()
    db.commit()
    if not row:
        raise HTTPException(404, "Recherche introuvable")
    return dict(row._mapping)


@router.delete("/{search_id}", status_code=204)
def delete_search(search_id: int, db: Session = Depends(get_db)):
    """Archive la recherche (is_active=false) sans supprimer les données."""
    result = db.execute(
        text("UPDATE searches SET is_active=false WHERE id=:sid"),
        {"sid": search_id},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Recherche introuvable")


@router.post("/{search_id}/pause", status_code=200)
def pause_search(search_id: int, db: Session = Depends(get_db)):
    db.execute(text("UPDATE searches SET is_active=false WHERE id=:sid"), {"sid": search_id})
    db.commit()
    return {"status": "paused"}


@router.post("/{search_id}/resume", status_code=200)
def resume_search(search_id: int, db: Session = Depends(get_db)):
    db.execute(text("UPDATE searches SET is_active=true WHERE id=:sid"), {"sid": search_id})
    db.commit()
    return {"status": "resumed"}


@router.post("/{search_id}/snapshot", status_code=202)
async def manual_snapshot(search_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Déclenche un snapshot manuel immédiat (asynchrone)."""
    from jobs import start_exploration_job
    search = db.execute(text("SELECT id FROM searches WHERE id=:sid"), {"sid": search_id}).fetchone()
    if not search:
        raise HTTPException(404, "Recherche introuvable")

    async def _run():
        from database.connection import SessionLocal
        db2 = SessionLocal()
        try:
            await run_snapshot(search_id, db2)
        finally:
            db2.close()

    background_tasks.add_task(_run)
    return {"status": "snapshot_started", "search_id": search_id}
