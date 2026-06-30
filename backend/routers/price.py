"""Router pour le suggesteur de prix."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from price_advisor import suggest_price

router = APIRouter(prefix="/api/price", tags=["price"])


class PriceRequest(BaseModel):
    product_model_id: int
    item_status: str


@router.post("/suggest")
def get_price_suggestion(body: PriceRequest, db: Session = Depends(get_db)):
    model = db.execute(
        text("SELECT id FROM product_models WHERE id = :mid"),
        {"mid": body.product_model_id},
    ).fetchone()
    if not model:
        raise HTTPException(404, "Modèle introuvable")
    return suggest_price(body.product_model_id, body.item_status, db)


@router.get("/history/{model_id}")
def get_price_history(model_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT ph.price, ph.recorded_at, l.title, l.item_status
            FROM price_history ph
            JOIN listings l ON l.id = ph.listing_id
            WHERE l.product_model_id = :mid
            ORDER BY ph.recorded_at DESC LIMIT 200
            """
        ),
        {"mid": model_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]
