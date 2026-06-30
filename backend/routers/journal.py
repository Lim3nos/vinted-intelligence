"""Routers pour le journal de terrain et les retours post-vente."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db

router = APIRouter(tags=["journal"])


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

class JournalCreate(BaseModel):
    title: str
    content: Optional[str] = None
    product_model_id: Optional[int] = None
    entry_type: str = "idea"


class JournalUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    product_model_id: Optional[int] = None
    entry_type: Optional[str] = None


@router.get("/api/journal")
def list_journal(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT * FROM journal_entries ORDER BY created_at DESC LIMIT 200")
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/api/journal", status_code=201)
def create_journal(body: JournalCreate, db: Session = Depends(get_db)):
    if body.entry_type not in ("idea", "observation", "decision"):
        raise HTTPException(400, "entry_type invalide")
    row = db.execute(
        text(
            """
            INSERT INTO journal_entries (title, content, product_model_id, entry_type)
            VALUES (:title, :content, :mid, :etype)
            RETURNING *
            """
        ),
        {"title": body.title, "content": body.content,
         "mid": body.product_model_id, "etype": body.entry_type},
    ).fetchone()
    db.commit()
    return dict(row._mapping)


@router.put("/api/journal/{entry_id}")
def update_journal(entry_id: int, body: JournalUpdate, db: Session = Depends(get_db)):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Aucun champ")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    fields["eid"] = entry_id
    row = db.execute(
        text(f"UPDATE journal_entries SET {sets}, updated_at=NOW() WHERE id=:eid RETURNING *"),
        fields,
    ).fetchone()
    db.commit()
    if not row:
        raise HTTPException(404, "Entrée introuvable")
    return dict(row._mapping)


@router.delete("/api/journal/{entry_id}", status_code=204)
def delete_journal(entry_id: int, db: Session = Depends(get_db)):
    result = db.execute(
        text("DELETE FROM journal_entries WHERE id=:eid"), {"eid": entry_id}
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Entrée introuvable")


# ---------------------------------------------------------------------------
# Retours post-vente
# ---------------------------------------------------------------------------

class FeedbackCreate(BaseModel):
    product_model_id: Optional[int] = None
    listing_id: Optional[int] = None
    actual_sale_price: float
    days_to_sell: Optional[int] = None
    sourcing_cost: float
    user_rating: Optional[int] = None
    notes: Optional[str] = None


@router.get("/api/feedback")
def list_feedback(model_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = "SELECT sf.*, pm.name AS model_name FROM sales_feedback sf LEFT JOIN product_models pm ON pm.id = sf.product_model_id"
    params: dict = {}
    if model_id:
        query += " WHERE sf.product_model_id = :mid"
        params["mid"] = model_id
    query += " ORDER BY sf.sold_at DESC LIMIT 200"
    rows = db.execute(text(query), params).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/api/feedback", status_code=201)
def create_feedback(body: FeedbackCreate, db: Session = Depends(get_db)):
    if body.user_rating and body.user_rating not in range(1, 6):
        raise HTTPException(400, "user_rating doit être entre 1 et 5")
    row = db.execute(
        text(
            """
            INSERT INTO sales_feedback
                (product_model_id, listing_id, actual_sale_price, days_to_sell,
                 sourcing_cost, user_rating, notes)
            VALUES (:mid, :lid, :sale, :days, :cost, :rating, :notes)
            RETURNING *
            """
        ),
        {
            "mid": body.product_model_id, "lid": body.listing_id,
            "sale": body.actual_sale_price, "days": body.days_to_sell,
            "cost": body.sourcing_cost, "rating": body.user_rating,
            "notes": body.notes,
        },
    ).fetchone()
    db.commit()
    return dict(row._mapping)


@router.get("/api/feedback/stats")
def feedback_stats(db: Session = Depends(get_db)):
    row = db.execute(
        text(
            """
            SELECT
                COUNT(*) AS total_sales,
                AVG(margin) AS avg_margin,
                AVG(days_to_sell) AS avg_days_to_sell,
                SUM(margin) AS total_margin
            FROM sales_feedback
            """
        )
    ).fetchone()
    return dict(row._mapping) if row else {}
