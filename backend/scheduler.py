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
    # Job 5 — Snapshots sur variantes de recherche des modèles
    # -----------------------------------------------------------------------
    def job_variant_snapshots():
        import asyncio
        from collector import safe_request_paginated, normalize_title, _extract_price, _extract_seller, csv_to_list
        from keywords import build_keyword_sets, keyword_set_matches
        from datetime import datetime, timezone

        db = db_session_factory()
        try:
            models = db.execute(
                text(
                    """
                    SELECT pm.id AS model_id, pm.name, pm.search_variants, pm.keywords_rules,
                           s.price_min, s.price_max, s.brand_ids, s.id AS search_id,
                           s.name AS search_name, s.search_type
                    FROM product_models pm
                    JOIN searches s ON s.id = pm.search_id
                    WHERE pm.is_active = true
                      AND pm.search_variants IS NOT NULL
                      AND jsonb_array_length(pm.search_variants) > 0
                    """
                )
            ).fetchall()

            if not models:
                return

            now_utc = datetime.now(timezone.utc)
            total_new = 0
            total_rejected = 0

            for model in models:
                # Jeux de mots-clés acceptés (base OU une variante) — un résultat de
                # recherche Vinted (fuzzy/pertinence, pas un ET strict) n'est accepté
                # que s'il satisfait entièrement au moins un jeu. Vérifier uniquement
                # les keywords_rules de base rejetterait les résultats trouvés PAR une
                # variante qui n'utilise pas les mêmes mots (contradictoire avec le but
                # même de la recherche par variantes).
                keyword_sets = build_keyword_sets(model.keywords_rules, model.search_variants)
                if not keyword_sets:
                    continue

                brand_hint = (
                    model.search_name.strip().lower()
                    if model.search_type == "brand" and model.search_name
                    else None
                )
                brand_ids_list = csv_to_list(model.brand_ids)

                variants = model.search_variants or []
                for variant in variants:
                    if not variant:
                        continue
                    params = {"search_text": variant}
                    if model.price_min:
                        params["price_from"] = model.price_min
                    if model.price_max:
                        params["price_to"] = model.price_max
                    if brand_ids_list:
                        params["brand_ids[]"] = brand_ids_list

                    items = safe_request_paginated(params, max_pages=2)
                    if not items:
                        continue

                    for item in items:
                        try:
                            vinted_id = int(item.get("id", 0))
                            if not vinted_id:
                                continue

                            title = item.get("title") or ""
                            title_norm = normalize_title(title)

                            # Rejeter tout résultat qui ne satisfait aucun des jeux de
                            # mots-clés acceptés pour ce modèle (voir keyword_set_matches)
                            if not any(
                                keyword_set_matches(title_norm, kw_set, brand_hint)
                                for kw_set in keyword_sets
                            ):
                                total_rejected += 1
                                continue

                            existing = db.execute(
                                text("SELECT id, product_model_id FROM listings WHERE vinted_id=:vid"),
                                {"vid": vinted_id},
                            ).fetchone()

                            if existing:
                                # Rattacher au modèle si pas encore fait
                                if not existing.product_model_id:
                                    db.execute(
                                        text("UPDATE listings SET product_model_id=:mid WHERE id=:lid"),
                                        {"mid": model.model_id, "lid": existing.id},
                                    )
                            else:
                                # Insérer avec product_model_id direct (trouvé via variante,
                                # titre vérifié ci-dessus)
                                price = _extract_price(item.get("price"))
                                seller_info = _extract_seller(item)
                                url = item.get("url") or ""
                                is_sold_from_api = item.get("can_be_sold") is False
                                db.execute(
                                    text(
                                        """
                                        INSERT INTO listings (
                                            vinted_id, search_id, product_model_id,
                                            title, title_normalized, price,
                                            seller_id, seller_login, url,
                                            first_seen_at, last_seen_at, consecutive_absences,
                                            is_sold
                                        ) VALUES (
                                            :vid, :sid, :mid,
                                            :title, :title_norm, :price,
                                            :seller_id, :seller_login, :url,
                                            :now, :now, 0,
                                            :is_sold
                                        ) ON CONFLICT (vinted_id) DO NOTHING
                                        """
                                    ),
                                    {
                                        "vid": vinted_id, "sid": model.search_id, "mid": model.model_id,
                                        "title": title, "title_norm": title_norm, "price": price,
                                        **seller_info, "url": url, "now": now_utc,
                                        "is_sold": is_sold_from_api,
                                    },
                                )
                                total_new += 1
                        except Exception:
                            continue

                db.commit()

            if total_new > 0 or total_rejected > 0:
                log_to_db(
                    "INFO", "scheduler",
                    f"Snapshot variantes — {total_new} nouvelles annonces insérées, "
                    f"{total_rejected} résultats rejetés (titre ne matchait pas les keywords_rules)",
                    {"new": total_new, "rejected": total_rejected},
                )

        except Exception as e:
            log_to_db("ERROR", "scheduler", f"Erreur job_variant_snapshots: {e}")
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
    scheduler.add_job(
        job_variant_snapshots,
        trigger=IntervalTrigger(hours=6),
        id="variant_snapshots",
        misfire_grace_time=300,
        replace_existing=True,
    )

    return scheduler
