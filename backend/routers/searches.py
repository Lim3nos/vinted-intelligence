"""Router CRUD pour les recherches surveillées."""

import json
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from collector import run_snapshot
from logger import log_to_db

router = APIRouter(prefix="/api/searches", tags=["searches"])

# Params Vinted qu'on stocke dans extra_params (passés tels quels à l'API)
_VINTED_EXTRA_PARAM_KEYS = {
    "status_ids", "size_ids", "color_ids", "material_ids",
    "video_game_rating_ids", "disposal_conditions",
}


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


class SearchFromUrl(BaseModel):
    vinted_url: str
    name: Optional[str] = None
    price_min: int = 0
    price_max: int = 9999
    snapshot_interval_hours: int = 3


@router.post("/from-url", status_code=201)
def create_search_from_url(body: SearchFromUrl, db: Session = Depends(get_db)):
    """
    Crée une recherche surveillée à partir d'une URL Vinted copiée depuis le navigateur.

    Exemple d'URL : https://www.vinted.fr/catalog?brand_ids=53&catalog_ids=79&status_ids=6
    On extrait : search_text → keywords, brand_ids, catalog_ids, status_ids, size_ids, etc.
    Les filtres non couverts (status_ids, size_ids, color_ids) sont stockés dans extra_params
    et passés tels quels à l'API Vinted lors du scraping.
    """
    parsed = urlparse(body.vinted_url)
    if "vinted." not in parsed.netloc:
        raise HTTPException(400, "L'URL doit être une URL vinted.fr (ou autre domaine Vinted)")

    params = parse_qs(parsed.query, keep_blank_values=False)

    def first(key: str) -> Optional[str]:
        # Vinted encode les arrays sous deux formes : brand_ids=X ou brand_ids[]=X
        vals = params.get(key) or params.get(key + "[]")
        return vals[0] if vals else None

    def all_vals(key: str) -> list:
        return params.get(key, []) + params.get(key + "[]", [])

    keywords = first("search_text") or ""
    # Vinted peut envoyer plusieurs brand_ids[] → on les joint par virgule
    brand_ids_list = all_vals("brand_ids")
    brand_ids = ",".join(brand_ids_list) if brand_ids_list else None
    catalog_ids_list = all_vals("catalog_ids")
    catalog_ids = ",".join(catalog_ids_list) if catalog_ids_list else None

    # price_from / price_to dans l'URL Vinted override les valeurs du body
    price_min = int(first("price_from") or body.price_min)
    price_max = int(first("price_to") or body.price_max)

    # Paramètres supplémentaires stockés dans extra_params
    # Gère aussi la forme key[]=val
    extra = {}
    for key in _VINTED_EXTRA_PARAM_KEYS:
        vals = all_vals(key)
        if vals:
            extra[key] = ",".join(vals)

    # Générer un nom auto si non fourni
    name = body.name
    if not name:
        parts = []
        if keywords:
            parts.append(keywords)
        if brand_ids:
            parts.append(f"brand:{brand_ids}")
        if catalog_ids:
            parts.append(f"cat:{catalog_ids}")
        if extra.get("status_ids"):
            parts.append(f"etat:{extra['status_ids']}")
        name = " | ".join(parts) if parts else "Recherche Vinted"

    # search_type déduit : si brand_ids → brand, si catalog_ids → category, sinon model
    if brand_ids and not catalog_ids:
        search_type = "brand"
    elif catalog_ids:
        search_type = "category"
    else:
        search_type = "model"

    row = db.execute(
        text(
            """
            INSERT INTO searches (name, search_type, keywords, brand_ids, catalog_ids,
                                  price_min, price_max, snapshot_interval_hours,
                                  extra_params, raw_vinted_url, is_active)
            VALUES (:name, :stype, :kw, :bids, :cids, :pmin, :pmax, :interval,
                    :extra, :raw_url, true)
            RETURNING *
            """
        ),
        {
            "name": name, "stype": search_type, "kw": keywords,
            "bids": brand_ids, "cids": catalog_ids,
            "pmin": price_min, "pmax": price_max,
            "interval": body.snapshot_interval_hours,
            "extra": json.dumps(extra),
            "raw_url": body.vinted_url,
        },
    ).fetchone()
    db.commit()
    log_to_db("INFO", "api", f"Recherche créée depuis URL Vinted : '{name}'",
              {"search_id": row.id, "extra_params": extra, "url": body.vinted_url})
    return dict(row._mapping)
