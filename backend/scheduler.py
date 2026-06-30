"""
Planificateur APScheduler — 4 jobs récurrents.
"""

import random
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from logger import log_to_db

logger = logging.getLogger("vinted.scheduler")


def setup_scheduler(db_session_factory):
    """
    Configure et retourne le planificateur APScheduler.

    Jobs :
    1. main_snapshots      — toutes les 3h (intervalle lu depuis system_settings)
    2. score_recalculation — toutes les 6h
    3. weekly_cleanup      — dimanche à 3h UTC
    4. health_check        — toutes les heures

    Tous les jobs ont misfire_grace_time=300 (5 min de tolérance).
    Les exceptions sont loggées en base et ne font pas crasher les autres jobs.
    """
    scheduler = BackgroundScheduler(timezone="UTC")

    # -----------------------------------------------------------------------
    # Job 1 — Snapshots automatiques
    # -----------------------------------------------------------------------
    def job_main_snapshots():
        import asyncio
        from collector import run_snapshot
        from settings import get_setting

        db = db_session_factory()
        try:
            # Lire l'intervalle dynamiquement depuis system_settings
            interval_h = get_setting("snapshot_interval_hours", db)

            searches = db.execute(
                text("SELECT id, name FROM searches WHERE is_active = true")
            ).fetchall()

            if not searches:
                log_to_db("INFO", "scheduler", "Aucune recherche active — snapshot ignoré")
                return

            # Varier l'ordre pour éviter les patterns détectables
            shuffled = list(searches)
            random.shuffle(shuffled)

            log_to_db(
                "INFO", "scheduler",
                f"Cycle de snapshot démarré — {len(shuffled)} recherche(s)",
                {"count": len(shuffled)},
            )

            for s in shuffled:
                db2 = db_session_factory()
                try:
                    asyncio.run(run_snapshot(s.id, db2))
                except Exception as e:
                    log_to_db(
                        "ERROR", "scheduler",
                        f"Snapshot #{s.id} '{s.name}' échoué: {e}",
                        {"search_id": s.id},
                    )
                finally:
                    db2.close()

        except Exception as e:
            log_to_db("ERROR", "scheduler", f"Erreur job_main_snapshots: {e}")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # Job 2 — Recalcul des scores
    # -----------------------------------------------------------------------
    def job_score_recalculation():
        from analyzer import recalculate_all_scores

        db = db_session_factory()
        try:
            count = recalculate_all_scores(db)
            log_to_db(
                "INFO", "scheduler",
                f"Recalcul scores terminé — {count} modèles",
                {"count": count},
            )
        except Exception as e:
            log_to_db("ERROR", "scheduler", f"Erreur job_score_recalculation: {e}")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # Job 3 — Nettoyage hebdomadaire (données > 90 jours)
    # -----------------------------------------------------------------------
    def job_weekly_cleanup():
        db = db_session_factory()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            for table in ("favourites_snapshots", "search_snapshots", "system_logs"):
                result = db.execute(
                    text(f"DELETE FROM {table} WHERE snapshot_at < :cutoff")
                    if table != "system_logs"
                    else text("DELETE FROM system_logs WHERE created_at < :cutoff"),
                    {"cutoff": cutoff},
                )
                db.commit()
                log_to_db(
                    "INFO", "scheduler",
                    f"Nettoyage {table} — {result.rowcount} lignes supprimées (> 90j)",
                    {"table": table, "deleted": result.rowcount},
                )
        except Exception as e:
            log_to_db("ERROR", "scheduler", f"Erreur job_weekly_cleanup: {e}")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # Job 4 — Health check
    # -----------------------------------------------------------------------
    def job_health_check():
        db = db_session_factory()
        try:
            row = db.execute(
                text(
                    "SELECT MAX(last_snapshot_at) AS last FROM searches WHERE is_active = true"
                )
            ).fetchone()

            if row and row.last:
                last = row.last
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if hours_since > 6:
                    log_to_db(
                        "CRITICAL", "scheduler",
                        f"Alerte : aucun snapshot depuis {hours_since:.1f}h",
                        {"hours_since": round(hours_since, 1)},
                    )
            else:
                log_to_db(
                    "WARNING", "scheduler",
                    "Health check : aucun snapshot enregistré dans la base",
                )
        except Exception as e:
            log_to_db("ERROR", "scheduler", f"Erreur job_health_check: {e}")
        finally:
            db.close()

    # -----------------------------------------------------------------------
    # Enregistrement des jobs
    # -----------------------------------------------------------------------
    scheduler.add_job(
        job_main_snapshots,
        trigger=IntervalTrigger(hours=3),
        id="main_snapshots",
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        job_score_recalculation,
        trigger=IntervalTrigger(hours=6),
        id="score_recalculation",
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        job_weekly_cleanup,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
        id="weekly_cleanup",
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        job_health_check,
        trigger=IntervalTrigger(hours=1),
        id="health_check",
        misfire_grace_time=300,
        replace_existing=True,
    )

    return scheduler
