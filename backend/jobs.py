"""
Gestion des jobs asynchrones (explorations longues).
Le scraping dure 2-4 min à cause des délais anti-blocage — on ne peut pas
bloquer Retool qui a un timeout de 30s.
"""

import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from collector import safe_request_paginated
from filters import apply_all_filter_levels
from ai_clustering import cluster_listings_by_model
from logger import log_to_db

logger = logging.getLogger("vinted.jobs")


def _cluster_is_relevant(cluster: dict, query_words: set) -> bool:
    """
    Filtre de pertinence : au moins un mot de la requête (≥3 chars) doit
    apparaître dans le model_name ou les suggested_keywords du cluster.
    Les groupes "Autres" et "Divers" sont systématiquement exclus.
    """
    name = cluster.get("model_name", "").lower().strip()
    # Exclure les fourre-tout générés par Gemini
    if name in ("autres", "divers", "other", "various", "miscellaneous"):
        return False
    if name.startswith("autre") or name.startswith("divers"):
        return False

    name_words = set(name.split())
    kw_words = {w.lower() for kw in cluster.get("suggested_keywords", []) for w in kw.split()}
    combined = name_words | kw_words
    return bool(query_words & combined)


# ---------------------------------------------------------------------------
# Création et exécution d'un job d'exploration
# ---------------------------------------------------------------------------

def _run_exploration_job(job_id: str, params: dict) -> None:
    """
    Exécute le job d'exploration en arrière-plan (lancé via FastAPI BackgroundTasks).
    Écrit toutes les mises à jour dans la table async_jobs.
    """
    from database.connection import SessionLocal
    db = SessionLocal()

    try:
        # Marquer le job comme en cours
        db.execute(
            text(
                "UPDATE async_jobs SET status='running', started_at=:now WHERE id=:jid"
            ),
            {"now": datetime.now(timezone.utc), "jid": job_id},
        )
        db.commit()

        log_to_db("INFO", "jobs", f"Job {job_id} démarré", {"params": params})

        # 1. Construire les paramètres de scraping
        scraper_params: dict = {}
        query = params.get("query", "")
        search_type = params.get("search_type", "brand")

        if search_type == "brand":
            scraper_params["search_text"] = query
        else:
            scraper_params["search_text"] = query

        price_min = params.get("price_min")
        price_max = params.get("price_max")
        if price_min is not None:
            scraper_params["price_from"] = price_min
        if price_max is not None:
            scraper_params["price_to"] = price_max

        filter_level = int(params.get("filter_level", 3))
        max_pages = 3 + filter_level  # niveaux hauts = plus de pages

        # 2. Scraper
        items = safe_request_paginated(scraper_params, max_pages=max_pages)

        if items is None:
            _fail_job(db, job_id, "Échec réseau lors du scraping Vinted")
            return

        log_to_db(
            "INFO", "jobs",
            f"Job {job_id} — {len(items)} annonces scrappées",
            {"job_id": job_id, "count": len(items)},
        )

        # 3. Appliquer les 5 niveaux de filtre
        filtered = apply_all_filter_levels(items, db)

        # 4. Clustering Gemini sur les titres du niveau sélectionné
        level_items = filtered.get(f"level_{filter_level}", [])

        # Pré-filtre : au moins un mot de la query doit apparaître dans le titre
        # Évite que Gemini reçoive des annonces hors-sujet (marques parasites)
        query_words = {w for w in query.lower().split() if len(w) >= 3}
        if query_words:
            before_title = len(level_items)
            level_items = [
                item for item in level_items
                if any(w in (item.get("title") or "").lower() for w in query_words)
            ]
            if before_title != len(level_items):
                log_to_db(
                    "INFO", "jobs",
                    f"Pré-filtre titre: {before_title} → {len(level_items)} items pour Gemini",
                    {"before": before_title, "after": len(level_items), "query_words": list(query_words)},
                )

        titles = [it.get("title", "") for it in level_items if it.get("title")]

        clusters = []
        if titles:
            clusters = cluster_listings_by_model(titles)
            # Enrichir les clusters avec les métriques
            import statistics as _stats
            for cluster in clusters:
                indices = cluster.get("indices", [])
                cluster_items = [level_items[i] for i in indices if i < len(level_items)]

                prices = []
                favs = []
                for ci in cluster_items:
                    p = ci.get("price")
                    if isinstance(p, dict):
                        p = p.get("amount")
                    if p:
                        try:
                            prices.append(float(p))
                        except Exception:
                            pass
                    f = ci.get("favourite_count")
                    if f is not None:
                        favs.append(int(f))

                cluster["nb_listings"] = len(cluster_items)
                cluster["median_price"] = round(_stats.median(prices), 2) if prices else None
                cluster["avg_favourites"] = round(sum(favs) / len(favs), 1) if favs else None
                cluster["sample_items"] = [
                    {
                        "id": ci.get("id"),
                        "title": ci.get("title"),
                        "price": ci.get("price"),
                        "favourite_count": ci.get("favourite_count"),
                        "url": ci.get("url"),
                    }
                    for ci in cluster_items[:3]
                ]

            # Filtre de pertinence : exclure les clusters "Autres", "Divers", etc.
            if query_words:
                before = len(clusters)
                clusters = [c for c in clusters if _cluster_is_relevant(c, query_words)]
                log_to_db(
                    "INFO", "jobs",
                    f"Filtre pertinence: {before} → {len(clusters)} clusters conservés",
                    {"before": before, "after": len(clusters), "query_words": list(query_words)},
                )

        result = {
            "total_scraped": filtered["total_scraped"],
            "filtered": {f"level_{i}": len(filtered[f"level_{i}"]) for i in range(1, 6)},
            "clusters": clusters,
            "query": query,
            "search_type": search_type,
        }

        # 5. Marquer le job terminé
        db.execute(
            text(
                """
                UPDATE async_jobs
                SET status='completed', result=CAST(:result AS jsonb), completed_at=:now
                WHERE id=:jid
                """
            ),
            {
                "result": json.dumps(result),
                "now": datetime.now(timezone.utc),
                "jid": job_id,
            },
        )
        db.commit()
        log_to_db(
            "INFO", "jobs",
            f"Job {job_id} terminé — {len(clusters)} clusters",
            {"job_id": job_id, "nb_clusters": len(clusters)},
        )

    except Exception as e:
        _fail_job(db, job_id, str(e))
    finally:
        db.close()


def _fail_job(db: Session, job_id: str, error_message: str) -> None:
    """Marque un job comme échoué en base."""
    try:
        db.execute(
            text(
                """
                UPDATE async_jobs
                SET status='failed', error_message=:err, completed_at=:now
                WHERE id=:jid
                """
            ),
            {"err": error_message, "now": datetime.now(timezone.utc), "jid": job_id},
        )
        db.commit()
    except Exception as e2:
        logger.error("Impossible de marquer le job %s en échec: %s", job_id, e2)

    log_to_db(
        "ERROR", "jobs",
        f"Job {job_id} échoué: {error_message}",
        {"job_id": job_id},
    )


async def start_exploration_job(params: dict, background_tasks, db: Session) -> str:
    """
    Crée un job d'exploration asynchrone et retourne son UUID immédiatement.
    Le scraping et le clustering s'exécutent en arrière-plan via BackgroundTasks.

    Paramètres :
        params           : {search_type, query, price_min, price_max, filter_level}
        background_tasks : FastAPI BackgroundTasks
        db               : session SQLAlchemy active

    Retourne : job_id (UUID string)
    """
    job_id = str(uuid.uuid4())

    db.execute(
        text(
            """
            INSERT INTO async_jobs (id, job_type, status, params, created_at)
            VALUES (:jid, 'exploration', 'pending', CAST(:params AS jsonb), :now)
            """
        ),
        {
            "jid": job_id,
            "params": json.dumps(params),
            "now": datetime.now(timezone.utc),
        },
    )
    db.commit()

    background_tasks.add_task(_run_exploration_job, job_id, params)
    return job_id


async def get_job_status(job_id: str, db: Session) -> dict:
    """
    Retourne le statut et le résultat d'un job.
    Appelé par Retool en polling toutes les 3 secondes.

    Retourne :
        {"status", "result", "error_message", "elapsed_seconds"}
    """
    row = db.execute(
        text(
            """
            SELECT status, result, error_message, created_at, started_at, completed_at
            FROM async_jobs WHERE id = :jid
            """
        ),
        {"jid": job_id},
    ).fetchone()

    if not row:
        return {"status": "not_found", "result": None, "error_message": "Job introuvable", "elapsed_seconds": 0}

    now = datetime.now(timezone.utc)
    created = row.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = int((now - created).total_seconds()) if created else 0

    return {
        "status": row.status,
        "result": row.result,  # déjà désérialisé par psycopg2 (JSONB)
        "error_message": row.error_message,
        "elapsed_seconds": elapsed,
    }
