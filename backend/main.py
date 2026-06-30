"""
Point d'entrée FastAPI — Vinted Market Intelligence.
Lance le scheduler APScheduler au démarrage.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

from database.connection import get_db, test_connection
from logger import log_to_db
from ai_clustering import is_circuit_open

from routers.searches import router as searches_router
from routers.models_router import router as models_router
from routers.exploration import router as exploration_router
from routers.price import router as price_router
from routers.journal import router as journal_router
from routers.settings_router import router as settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Démarrage : init DB + scheduler. Arrêt : shutdown scheduler."""
    log_to_db("INFO", "api", "Démarrage Vinted Intelligence API")

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


@app.get("/api/health/logs")
def get_logs(
    level: str = "INFO",
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    valid_levels = {"INFO", "WARNING", "ERROR", "CRITICAL"}
    if level.upper() not in valid_levels:
        level = "INFO"

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
        {"level": level.upper(), "limit": limit},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


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
