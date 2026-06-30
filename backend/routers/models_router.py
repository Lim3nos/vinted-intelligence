"""Router CRUD pour les modèles produit."""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from analyzer import calculate_signal_score, calculate_heatmap

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelCreate(BaseModel):
    name: str
    brand: Optional[str] = None
    keywords_rules: list = []
    search_id: Optional[int] = None
    user_priority: str = "normal"
    user_notes: Optional[str] = None


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    brand: Optional[str] = None
    keywords_rules: Optional[list] = None
    user_priority: Optional[str] = None
    user_notes: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("")
def list_models(
    min_score: int = 0,
    priority: str = "all",
    price_min: float = 0,
    price_max: float = 9999,
    active_days: int = 0,
    db: Session = Depends(get_db),
):
    filters = "WHERE pm.is_active = true AND pm.signal_score >= :min_score"
    params: dict = {"min_score": min_score}

    if priority != "all":
        filters += " AND pm.user_priority = :priority"
        params["priority"] = priority

    rows = db.execute(
        text(f"SELECT pm.* FROM product_models pm {filters} ORDER BY pm.signal_score DESC"),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/{model_id}")
def get_model(model_id: int, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT * FROM product_models WHERE id = :mid"),
        {"mid": model_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Modèle introuvable")
    return dict(row._mapping)


@router.post("", status_code=201)
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    import json
    row = db.execute(
        text(
            """
            INSERT INTO product_models
                (name, brand, keywords_rules, search_id, user_priority, user_notes, is_active)
            VALUES (:name, :brand, CAST(:kw AS jsonb), :sid, :prio, :notes, true)
            RETURNING *
            """
        ),
        {
            "name": body.name, "brand": body.brand,
            "kw": json.dumps(body.keywords_rules),
            "sid": body.search_id, "prio": body.user_priority,
            "notes": body.user_notes,
        },
    ).fetchone()
    db.commit()
    return dict(row._mapping)


@router.put("/{model_id}")
def update_model(model_id: int, body: ModelUpdate, db: Session = Depends(get_db)):
    import json
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "Aucun champ à mettre à jour")
    if "keywords_rules" in updates:
        updates["keywords_rules"] = f"CAST('{json.dumps(updates['keywords_rules'])}' AS jsonb)"
    sets = []
    params: dict = {"mid": model_id, "now": "NOW()"}
    for k, v in updates.items():
        if k == "keywords_rules":
            sets.append(f"{k} = CAST(:{k}_raw AS jsonb)")
            params[f"{k}_raw"] = json.dumps(body.keywords_rules)
        else:
            sets.append(f"{k} = :{k}")
            params[k] = v
    sets.append("updated_at = NOW()")
    row = db.execute(
        text(f"UPDATE product_models SET {', '.join(sets)} WHERE id = :mid RETURNING *"),
        params,
    ).fetchone()
    db.commit()
    if not row:
        raise HTTPException(404, "Modèle introuvable")
    return dict(row._mapping)


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: int, db: Session = Depends(get_db)):
    """Archive le modèle (is_active=false) sans supprimer les données."""
    result = db.execute(
        text("UPDATE product_models SET is_active=false, updated_at=NOW() WHERE id=:mid"),
        {"mid": model_id},
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Modèle introuvable")


@router.get("/{model_id}/listings")
def get_model_listings(
    model_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=50),
    db: Session = Depends(get_db),
):
    offset = (page - 1) * per_page
    rows = db.execute(
        text(
            """
            SELECT l.*,
                   (SELECT favourite_count FROM favourites_snapshots fs
                    WHERE fs.listing_id = l.id ORDER BY snapshot_at DESC LIMIT 1) AS latest_favs
            FROM listings l
            WHERE l.product_model_id = :mid
            ORDER BY l.first_seen_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"mid": model_id, "limit": per_page, "offset": offset},
    ).fetchall()
    total = db.execute(
        text("SELECT COUNT(*) FROM listings WHERE product_model_id = :mid"),
        {"mid": model_id},
    ).scalar()
    return {"total": total, "page": page, "per_page": per_page,
            "items": [dict(r._mapping) for r in rows]}


@router.get("/{model_id}/heatmap")
def get_heatmap(model_id: int, db: Session = Depends(get_db)):
    return calculate_heatmap(model_id, db)


@router.get("/{model_id}/price-history")
def get_price_history(model_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT ph.* FROM price_history ph
            JOIN listings l ON l.id = ph.listing_id
            WHERE l.product_model_id = :mid
            ORDER BY ph.recorded_at DESC LIMIT 200
            """
        ),
        {"mid": model_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/{model_id}/velocity")
def get_velocity(model_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT DATE(snapshot_at) AS day, AVG(favourite_count) AS avg_favs
            FROM favourites_snapshots fs
            JOIN listings l ON l.id = fs.listing_id
            WHERE l.product_model_id = :mid
            GROUP BY DATE(snapshot_at)
            ORDER BY day DESC LIMIT 30
            """
        ),
        {"mid": model_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/{model_id}/score-history")
def get_score_history(model_id: int, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT signal_score, score_confidence, updated_at FROM product_models WHERE id=:mid"),
        {"mid": model_id},
    ).fetchone()
    if not row:
        raise HTTPException(404, "Modèle introuvable")
    return dict(row._mapping)
