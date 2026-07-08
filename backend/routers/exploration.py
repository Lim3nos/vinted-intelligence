"""Router pour l'exploration (jobs asynchrones) et la validation de clusters."""

import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db
from jobs import start_exploration_job, get_job_status
from keywords import sanitize_keywords

router = APIRouter(tags=["exploration"])


class ExplorationParams(BaseModel):
    search_type: str  # 'brand' | 'category'
    query: str
    price_min: int = 50
    price_max: int = 120
    filter_level: int = 3


class ValidateClusterBody(BaseModel):
    model_name: str
    suggested_keywords: list = []
    search_id: Optional[int] = None
    nb_listings: Optional[int] = None
    median_price: Optional[float] = None


class ValidateClustersBody(BaseModel):
    clusters: list  # liste de ValidateClusterBody-like dicts
    search_id: Optional[int] = None  # search_id par défaut si absent dans chaque cluster


@router.post("/api/exploration/start", status_code=202)
async def start_exploration(
    body: ExplorationParams,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if body.search_type not in ("brand", "category", "model", "keyword"):
        raise HTTPException(400, "search_type doit être 'brand', 'category', 'model' ou 'keyword'")
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
    Crée un product_model à partir d'un cluster validé par l'utilisateur.
    Colonnes réelles : name, keywords_rules (jsonb), search_id, user_priority, is_active.
    """
    # Vérifier que le modèle n'existe pas déjà (même nom + même search_id)
    existing = db.execute(
        text("SELECT id FROM product_models WHERE name = :name AND search_id = :sid"),
        {"name": body.model_name, "sid": body.search_id},
    ).fetchone()
    if existing:
        raise HTTPException(409, f"Un modèle '{body.model_name}' existe déjà pour cette recherche (id={existing.id})")

    clean_keywords = sanitize_keywords(body.suggested_keywords)
    if not clean_keywords:
        raise HTTPException(
            400,
            f"Mots-clés invalides pour '{body.model_name}' : tous vides ou trop courts "
            "(minimum 2 caractères) — refusé pour éviter un modèle qui matcherait tout",
        )

    try:
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
                "kw": json.dumps(clean_keywords),
                "sid": body.search_id,
            },
        ).fetchone()
        db.commit()
        model_id = row.id

        # Générer les variantes de recherche en arrière-plan (Gemini)
        try:
            from ai_clustering import generate_search_variants
            search_query = ""
            if body.search_id:
                s = db.execute(text("SELECT keywords FROM searches WHERE id=:sid"), {"sid": body.search_id}).fetchone()
                if s:
                    search_query = s.keywords or ""
            variants = generate_search_variants(body.model_name, body.suggested_keywords, search_query)
            if variants:
                db.execute(
                    text("UPDATE product_models SET search_variants=CAST(:v AS jsonb) WHERE id=:mid"),
                    {"v": json.dumps(variants), "mid": model_id},
                )
                db.commit()
        except Exception:
            pass  # Variantes non critiques

        return dict(row._mapping)
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Erreur création modèle : {e}")


@router.post("/api/exploration/validate-clusters", status_code=201)
def validate_clusters(body: ValidateClustersBody, db: Session = Depends(get_db)):
    """
    Valide plusieurs clusters en une seule requête.
    Retourne : {created: [...], skipped: [...], errors: [...]}
    """
    created = []
    skipped = []
    errors = []

    for item in body.clusters:
        model_name = item.get("model_name", "").strip()
        if not model_name:
            errors.append({"model_name": "(vide)", "reason": "model_name manquant"})
            continue

        search_id = item.get("search_id") or body.search_id
        suggested_keywords = sanitize_keywords(item.get("suggested_keywords", []))
        if not suggested_keywords:
            errors.append({
                "model_name": model_name,
                "reason": "mots-clés invalides : tous vides ou trop courts (minimum 2 caractères)",
            })
            continue

        # Doublon
        existing = db.execute(
            text("SELECT id FROM product_models WHERE name = :name AND search_id = :sid"),
            {"name": model_name, "sid": search_id},
        ).fetchone()
        if existing:
            skipped.append({"model_name": model_name, "existing_id": existing.id})
            continue

        try:
            row = db.execute(
                text(
                    """
                    INSERT INTO product_models
                        (name, keywords_rules, search_id, user_priority, is_active)
                    VALUES (:name, CAST(:kw AS jsonb), :sid, 'normal', true)
                    RETURNING id, name, signal_score, is_active, created_at
                    """
                ),
                {
                    "name": model_name,
                    "kw": json.dumps(suggested_keywords),
                    "sid": search_id,
                },
            ).fetchone()
            db.commit()
            model_data = dict(row._mapping)

            # Variantes de recherche
            try:
                from ai_clustering import generate_search_variants
                search_kw = ""
                if search_id:
                    s = db.execute(text("SELECT keywords FROM searches WHERE id=:sid"), {"sid": search_id}).fetchone()
                    if s:
                        search_kw = s.keywords or ""
                variants = generate_search_variants(model_name, suggested_keywords, search_kw)
                if variants:
                    db.execute(
                        text("UPDATE product_models SET search_variants=CAST(:v AS jsonb) WHERE id=:mid"),
                        {"v": json.dumps(variants), "mid": model_data["id"]},
                    )
                    db.commit()
                    model_data["search_variants"] = variants
            except Exception:
                pass

            created.append(model_data)
        except Exception as e:
            db.rollback()
            errors.append({"model_name": model_name, "reason": str(e)})

    return {
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "summary": f"{len(created)} créés, {len(skipped)} ignorés (doublons), {len(errors)} erreurs",
    }
