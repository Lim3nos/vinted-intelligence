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
    my_item_status: str


def _do_suggest(model_id: int, status: str, db: Session):
    model = db.execute(
        text("SELECT id FROM product_models WHERE id = :mid"),
        {"mid": model_id},
    ).fetchone()
    if not model:
        raise HTTPException(404, "Modèle introuvable")
    return suggest_price(model_id, status, db)


@router.post("/suggest")
def post_price_suggestion(body: PriceRequest, db: Session = Depends(get_db)):
    return _do_suggest(body.product_model_id, body.my_item_status, db)


@router.get("/suggest")
def get_price_suggestion(
    model_id: int,
    status: str = "Très bon état",
    db: Session = Depends(get_db),
):
    return _do_suggest(model_id, status, db)


@router.get("/history/{model_id}")
def get_price_history(model_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT
                ph.recorded_at,
                ROUND(ph.price::numeric, 0) AS price,
                l.title,
                l.item_status
            FROM price_history ph
            JOIN listings l ON l.id = ph.listing_id
            WHERE l.product_model_id = :mid
            ORDER BY ph.recorded_at DESC
            LIMIT 200
            """
        ),
        {"mid": model_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]
