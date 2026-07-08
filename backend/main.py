"""
Point d'entrée FastAPI — Vinted Market Intelligence.
Lance le scheduler APScheduler au démarrage.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import logging
from fastapi import FastAPI, Depends, Query, BackgroundTasks

logger = logging.getLogger("vinted.main")
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db, test_connection
from logger import log_to_db
from ai_clustering import is_circuit_open
from keywords import sanitize_keywords, match_model

from routers.searches import router as searches_router
from routers.models_router import router as models_router
from routers.exploration import router as exploration_router
from routers.price import router as price_router
from routers.journal import router as journal_router
from routers.settings_router import router as settings_router
from routers.listings_router import router as listings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Démarrage : init DB + scheduler. Arrêt : shutdown scheduler."""
    log_to_db("INFO", "api", "Démarrage Vinted Intelligence API")

    # Migrations : s'assurer que les colonnes récentes existent
    from database.connection import SessionLocal as _SL
    _db_mig = _SL()
    # Chaque migration dans son propre try/except pour isolation
    for migration_sql in [
        "ALTER TABLE product_models ADD COLUMN IF NOT EXISTS search_variants JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE searches ADD COLUMN IF NOT EXISTS extra_params JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE searches ADD COLUMN IF NOT EXISTS raw_vinted_url TEXT",
        # Table dédiée tokens auth Vinted (nullable, sans contraintes strictes)
        """CREATE TABLE IF NOT EXISTS vinted_auth (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )""",
        "INSERT INTO vinted_auth (key, value) VALUES ('access_token', NULL), ('refresh_token', NULL), ('expires_at', NULL) ON CONFLICT (key) DO NOTHING",
    ]:
        try:
            _db_mig.execute(text(migration_sql))
            _db_mig.commit()
        except Exception:
            _db_mig.rollback()

    # Nettoyage rétroactif de keywords_rules — cause racine du bug des "modèles
    # parasités" : un mot-clé vide ou trop court matche n'importe quel titre
    # (substring vide toujours présente en Python), ce qui fait que le matching
    # assigne alors la totalité du bruit d'une recherche au modèle concerné.
    # Idempotent : ne réécrit que les lignes réellement modifiées par le nettoyage.
    try:
        import json as _json
        model_rows = _db_mig.execute(
            text("SELECT id, name, keywords_rules FROM product_models")
        ).fetchall()
        fixed_count = 0
        emptied_ids = []
        for m in model_rows:
            raw = m.keywords_rules
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = []
            original = raw or []
            cleaned = sanitize_keywords(original)
            if cleaned != original:
                _db_mig.execute(
                    text("UPDATE product_models SET keywords_rules = CAST(:kw AS jsonb) WHERE id = :mid"),
                    {"kw": _json.dumps(cleaned), "mid": m.id},
                )
                fixed_count += 1
                if not cleaned:
                    emptied_ids.append(m.id)
        _db_mig.commit()
        if fixed_count:
            log_to_db(
                "WARNING", "api",
                f"Migration keywords_rules : {fixed_count} modele(s) nettoye(s) — "
                f"{len(emptied_ids)} n'ont plus aucun mot-cle valide "
                "(matching desactive jusqu'a correction manuelle)",
                {"emptied_model_ids": emptied_ids},
            )
    except Exception as e:
        _db_mig.rollback()
        log_to_db("ERROR", "api", f"Migration keywords_rules echouee : {e}")

    _db_mig.close()

    from scheduler import setup_scheduler
    from database.connection import SessionLocal
    scheduler = setup_scheduler(SessionLocal)
    scheduler.start()
    log_to_db("INFO", "scheduler", "APScheduler démarré")

    yield

    scheduler.shutdown(wait=False)
    log_to_db("INFO", "api", "Arrêt Vinted Intelligence API")


app = FastAPI(
    title="Vinted Market Intelligence",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(searches_router)
app.include_router(listings_router)
app.include_router(models_router)
app.include_router(exploration_router)
app.include_router(price_router)
app.include_router(journal_router)
app.include_router(settings_router)


# ---------------------------------------------------------------------------
# Santé système
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db_ok = test_connection()

    last_snap = db.execute(
        text(
            "SELECT MAX(last_snapshot_at) AS last FROM searches WHERE is_active = true"
        )
    ).fetchone()

    last_snap_at = last_snap.last if last_snap else None
    hours_since = None
    snapshot_warning = False

    if last_snap_at:
        if last_snap_at.tzinfo is None:
            last_snap_at = last_snap_at.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - last_snap_at
        hours_since = round(diff.total_seconds() / 3600, 1)
        snapshot_warning = hours_since > 6

    active_searches = db.execute(
        text("SELECT COUNT(*) FROM searches WHERE is_active = true")
    ).scalar()

    status = "ok"
    if not db_ok or snapshot_warning:
        status = "degraded"

    return {
        "status": status,
        "last_snapshot_at": last_snap_at.isoformat() if last_snap_at else None,
        "hours_since_last_snapshot": hours_since,
        "snapshot_warning": snapshot_warning,
        "db_connected": db_ok,
        "gemini_circuit_open": is_circuit_open(),
        "active_searches": active_searches,
    }


def _get_logs(level: str, limit: int, db: Session):
    valid_levels = {"INFO", "WARNING", "ERROR", "CRITICAL"}
    level = level.upper() if level.upper() in valid_levels else "INFO"
    rows = db.execute(
        text(
            """
            SELECT level, component, message, context, created_at
            FROM system_logs
            WHERE level = :level
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"level": level, "limit": limit},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@app.get("/api/health/logs")
def get_health_logs(
    level: str = "INFO",
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    return _get_logs(level, limit, db)


@app.get("/api/logs")
def get_logs(
    level: str = "INFO",
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    return _get_logs(level, limit, db)



@app.post("/api/admin/verify-sold", status_code=202)
def verify_sold_listings(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Lance en arrière-plan la vérification des listings marqués vendus via l'API Vinted.
    Retourne immédiatement, résultat dans les logs.
    """
    from database.connection import SessionLocal

    def _run_verify():
        _db = SessionLocal()
        try:
            _do_verify_sold(_db)
        finally:
            _db.close()

    background_tasks.add_task(_run_verify)
    return {"status": "verify_started", "message": "Vérification en arrière-plan — résultat dans /api/logs"}


def _do_verify_sold(db):
    """
    Vérification effective des listings vendus via page HTML item — exécuté en background.

    Méthode : scrape /items/{id} → extrait is_closed depuis JSON embarqué dans le HTML.
    Fonctionne sans auth, retourne le vrai statut is_closed.
    Fallback sur catalog vendeur si la page HTML échoue (timeout, 429).
    """
    from datetime import timedelta
    import time, random

    cutoff = datetime.now(timezone.utc) - timedelta(days=60)

    candidates = db.execute(
        text(
            """
            SELECT id, vinted_id, title, disappeared_at, seller_id
            FROM listings
            WHERE is_sold = true
              AND disappeared_at >= :cutoff
            ORDER BY disappeared_at DESC
            LIMIT 100
            """
        ),
        {"cutoff": cutoff},
    ).fetchall()

    if not candidates:
        log_to_db("INFO", "api", "verify-sold : aucun candidat trouvé")
        return

    from collector import _check_item_sold_via_html, _check_seller_still_has_item

    reset_count = 0
    confirmed_count = 0
    skipped_count = 0

    for listing in candidates:
        try:
            # Méthode 1 : page HTML item (is_closed exact)
            time.sleep(random.uniform(1.0, 2.5))
            result = _check_item_sold_via_html(listing.vinted_id)

            # Fallback méthode 2 : catalog vendeur
            if result is None and listing.seller_id:
                result_catalog = _check_seller_still_has_item(
                    listing.vinted_id, listing.seller_id, listing.title or ""
                )
                # catalog: True=actif → pas vendu ; False=absent → vendu
                result = (not result_catalog) if result_catalog is not None else None

            # result: True=vendu, False=encore actif, None=inconnu
            if result is False:
                # Item encore actif → c'était un faux positif → reset
                db.execute(
                    text(
                        """
                        UPDATE listings
                        SET is_sold = false,
                            disappeared_at = NULL,
                            time_to_disappear_hours = NULL,
                            final_price = NULL,
                            consecutive_absences = 0,
                            last_seen_at = NOW()
                        WHERE id = :lid
                        """
                    ),
                    {"lid": listing.id},
                )
                reset_count += 1
            elif result is True:
                # Confirmé vendu (is_closed=true ou page 404)
                confirmed_count += 1
            else:
                # Vérification impossible (timeout, 429, parse error) → ne pas toucher
                skipped_count += 1

        except Exception as e:
            skipped_count += 1
            logger.warning("Erreur verify-sold item %d: %s", listing.vinted_id, e)

    db.commit()

    result = {
        "checked": len(candidates),
        "reset": reset_count,
        "confirmed_sold": confirmed_count,
        "skipped": skipped_count,
    }
    log_to_db("INFO", "api", f"verify-sold : {reset_count} réinitialisés, {confirmed_count} confirmés, {skipped_count} ignorés", result)


@app.post("/api/admin/reset-and-rematch", status_code=202)
def reset_and_rematch(db: Session = Depends(get_db)):
    """Remet product_model_id à NULL sur tous les listings puis relance le rematch."""
    db.execute(text("UPDATE listings SET product_model_id = NULL"))
    db.commit()
    log_to_db("INFO", "api", "Reset product_model_id — rematch en cours")
    return rematch_listings(db)


@app.post("/api/admin/rematch-listings", status_code=202)
def rematch_listings(db: Session = Depends(get_db)):
    """
    Rattache rétroactivement les listings sans product_model_id aux modèles actifs
    en appliquant les keywords_rules. À appeler après avoir validé de nouveaux clusters.
    """
    from collections import defaultdict

    # Charger les modèles actifs
    model_rows = db.execute(
        text(
            "SELECT id, search_id, keywords_rules, search_variants "
            "FROM product_models WHERE is_active = true"
        )
    ).fetchall()

    models_by_search: dict = defaultdict(list)
    for m in model_rows:
        models_by_search[m.search_id].append(m)

    distinct_search_ids = list(models_by_search.keys())

    # Listings sans model_id pour les search_id qui ont des modèles
    listings = db.execute(
        text(
            "SELECT id, search_id, title_normalized FROM listings "
            "WHERE product_model_id IS NULL "
            "  AND title_normalized IS NOT NULL "
            "  AND search_id = ANY(:sids)"
        ),
        {"sids": distinct_search_ids},
    ).fetchall()

    matched = 0
    skipped_no_candidates = 0

    for listing in listings:
        candidates = models_by_search.get(listing.search_id, [])
        if not candidates:
            skipped_no_candidates += 1
            continue

        title = listing.title_normalized or ""
        best_id = match_model(title, candidates)

        if best_id:
            db.execute(
                text("UPDATE listings SET product_model_id = :mid WHERE id = :lid"),
                {"mid": best_id, "lid": listing.id},
            )
            matched += 1

    db.commit()

    result = {
        "matched": matched,
        "total_checked": len(listings),
        "models_loaded": len(model_rows),
        "search_ids_with_models": distinct_search_ids,
        "skipped_no_candidates": skipped_no_candidates,
    }
    log_to_db("INFO", "api", f"Rematch listings : {matched}/{len(listings)} rattachés", result)
    return result


@app.get("/api/debug/scrape")
def debug_scrape(q: str = "lemaire", db: Session = Depends(get_db)):
    """
    Diagnostic : teste une requête Vinted brute et retourne le statut HTTP,
    les headers Cloudflare, et les premiers résultats (max 3).
    Ne jamais appeler en boucle — délai anti-bot inclus.
    """
    import time, random
    from curl_cffi.requests import Session as CurlSession

    base_url = os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr")
    result: dict = {"base_url": base_url, "query": q}

    # Étape 1 : fetch du cookie anonyme
    session = CurlSession(impersonate="chrome")
    try:
        r0 = session.get(
            base_url + "/",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "fr-FR,fr;q=0.9"},
            timeout=15,
        )
        result["cookie_fetch_status"] = r0.status_code
        result["cookie_names"] = list(r0.cookies.keys())
        result["has_access_token"] = "access_token_web" in r0.cookies
    except Exception as e:
        result["cookie_fetch_error"] = str(e)
        return result

    # Étape 2 : requête API
    time.sleep(random.uniform(2.0, 3.5))
    try:
        r1 = session.get(
            f"{base_url}/api/v2/catalog/items",
            params={"search_text": q, "per_page": 5, "page": 1},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Referer": f"{base_url}/catalog",
                "Origin": base_url,
            },
            timeout=20,
        )
        result["api_status"] = r1.status_code
        result["cf_mitigated"] = r1.headers.get("cf-mitigated", "")
        result["cf_ray"] = r1.headers.get("cf-ray", "")
        result["content_type"] = r1.headers.get("content-type", "")
        if r1.status_code == 200:
            data = r1.json()
            items = data.get("items") or []
            result["items_count"] = len(items)
            result["sample_titles"] = [i.get("title") for i in items[:3]]
            if items:
                first = items[0]
                result["first_item_sold_fields"] = {
                    "id": first.get("id"),
                    "can_be_sold": first.get("can_be_sold"),
                    "is_sold": first.get("is_sold"),
                    "status": first.get("status"),
                    "all_keys": list(first.keys()),
                }
                # Étape 3 : tester l'item detail sur le 1er item du catalog
                # Pour savoir si l'API detail est accessible (auth suffisante)
                first_id = first.get("id")
                if first_id:
                    time.sleep(random.uniform(1.5, 2.5))
                    try:
                        r2 = session.get(
                            f"{base_url}/api/v2/items/{first_id}",
                            headers={
                                "Accept": "application/json, text/plain, */*",
                                "Accept-Language": "fr-FR,fr;q=0.9",
                                "Referer": f"{base_url}/items/{first_id}",
                                "Origin": base_url,
                            },
                            timeout=12,
                        )
                        result["item_detail_test"] = {
                            "vinted_id": first_id,
                            "status": r2.status_code,
                        }
                        if r2.status_code == 200:
                            det = r2.json().get("item", {})
                            result["item_detail_test"]["sold_fields"] = {
                                k: det.get(k) for k in [
                                    "is_closed", "item_closing_action", "badge",
                                    "can_buy", "can_be_sold", "is_reserved",
                                    "is_hidden", "is_visible", "status",
                                ]
                            }
                            result["item_detail_test"]["all_keys"] = list(det.keys())
                        else:
                            result["item_detail_test"]["body"] = r2.text[:200]
                    except Exception as e2:
                        result["item_detail_test"] = {"error": str(e2)}
        else:
            result["body_snippet"] = r1.text[:400]
    except Exception as e:
        result["api_error"] = str(e)

    log_to_db("INFO", "debug", "Debug scrape exécuté", result)
    return result


@app.post("/api/admin/warmup", status_code=202)
async def warmup(
    body: dict,
    db: Session = Depends(get_db),
):
    """
    Mode warm-up : 3 snapshots espacés de 20 min sur les recherches indiquées.
    Lance en arrière-plan.
    """
    from fastapi import BackgroundTasks
    from collector import run_snapshot
    from database.connection import SessionLocal
    import asyncio

    search_ids = body.get("search_ids", [])
    if not search_ids:
        return {"status": "no_searches_provided"}

    async def _warmup():
        for _ in range(3):
            for sid in search_ids:
                db2 = SessionLocal()
                try:
                    await run_snapshot(sid, db2)
                finally:
                    db2.close()
            log_to_db("INFO", "api", "Warm-up cycle terminé — pause 20 min")
            await asyncio.sleep(1200)

    import asyncio
    asyncio.create_task(_warmup())
    log_to_db(
        "INFO", "api",
        f"Warm-up démarré pour {len(search_ids)} recherche(s)",
        {"search_ids": search_ids},
    )
    return {"status": "warmup_started", "search_ids": search_ids, "cycles": 3}
