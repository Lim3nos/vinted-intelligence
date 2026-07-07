"""Router pour l'accès aux listings collectés."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional

from database.connection import get_db

router = APIRouter(prefix="/api/listings", tags=["listings"])


@router.get("")
def get_listings(
    search_id: Optional[int] = Query(None),
    is_sold: Optional[bool] = Query(None),
    price_min: Optional[float] = Query(None),
    price_max: Optional[float] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    order: str = Query("newest"),
    db: Session = Depends(get_db),
):
    """
    Liste les listings avec filtres.

    order: newest (first_seen DESC), cheapest (price ASC), expensive (price DESC),
           sold_recent (disappeared_at DESC)
    """
    order_map = {
        "newest":      "l.first_seen_at DESC",
        "oldest":      "l.first_seen_at ASC",
        "cheapest":    "l.price ASC",
        "expensive":   "l.price DESC",
        "sold_recent": "l.disappeared_at DESC NULLS LAST",
    }
    order_clause = order_map.get(order, "l.first_seen_at DESC")

    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if search_id is not None:
        conditions.append("l.search_id = :search_id")
        params["search_id"] = search_id
    if is_sold is not None:
        conditions.append("l.is_sold = :is_sold")
        params["is_sold"] = is_sold
    if price_min is not None:
        conditions.append("l.price >= :price_min")
        params["price_min"] = price_min
    if price_max is not None:
        conditions.append("l.price <= :price_max")
        params["price_max"] = price_max

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = db.execute(
        text(f"""
            SELECT
                l.id,
                l.vinted_id,
                l.search_id,
                s.name AS search_name,
                l.title,
                l.price,
                l.final_price,
                l.brand,
                l.item_status,
                l.seller_login,
                l.seller_total_sales,
                l.seller_feedback_score,
                l.url,
                l.photo_url,
                l.first_seen_at,
                l.last_seen_at,
                l.disappeared_at,
                l.is_sold,
                l.time_to_disappear_hours,
                EXTRACT(EPOCH FROM (COALESCE(l.disappeared_at, NOW()) - l.first_seen_at)) / 3600
                    AS hours_active
            FROM listings l
            LEFT JOIN searches s ON s.id = l.search_id
            {where}
            ORDER BY {order_clause}
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    # Total count
    count_row = db.execute(
        text(f"SELECT COUNT(*) FROM listings l {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    ).fetchone()

    return {
        "total": count_row[0],
        "offset": offset,
        "limit": limit,
        "listings": [dict(r._mapping) for r in rows],
    }


@router.get("/stats")
def get_listings_stats(
    search_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Stats agrégées par recherche (ou toutes les recherches)."""
    where = "WHERE l.search_id = :sid" if search_id is not None else ""
    params = {"sid": search_id} if search_id is not None else {}

    row = db.execute(
        text(f"""
            SELECT
                COUNT(*) FILTER (WHERE l.is_sold = false)  AS active_count,
                COUNT(*) FILTER (WHERE l.is_sold = true)   AS sold_count,
                COUNT(*)                                    AS total_count,
                AVG(l.price) FILTER (WHERE l.is_sold=false) AS avg_price_active,
                MIN(l.price) FILTER (WHERE l.is_sold=false) AS min_price_active,
                MAX(l.price) FILTER (WHERE l.is_sold=false) AS max_price_active,
                AVG(l.time_to_disappear_hours)
                    FILTER (WHERE l.is_sold=true)           AS avg_hours_to_sell,
                COUNT(*) FILTER (WHERE l.first_seen_at >= NOW() - INTERVAL '24 hours'
                                   AND l.is_sold = false)  AS new_last_24h
            FROM listings l
            {where}
        """),
        params,
    ).fetchone()

    return dict(row._mapping)
