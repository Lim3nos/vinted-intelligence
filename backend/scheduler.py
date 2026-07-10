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


def run_variant_snapshots(db_session_factory):
    """
    Recherche chaque variante de nom générée par Gemini pour chaque modèle actif,
    rattache/insère les résultats qui matchent réellement (voir keywords.py), et
    backfill brand/état/photo sur les annonces déjà connues.

    Extrait en fonction de module (plutôt que nichée dans setup_scheduler) pour
    pouvoir être déclenchée manuellement via POST /api/admin/variant-snapshot,
    en plus du job APScheduler toutes les 6h.
    """
    from collector import (
        safe_request_paginated, normalize_title, _extract_price, _extract_seller,
        _detect_brand_in_title, csv_to_list,
    )
    from keywords import build_keyword_sets, keyword_set_matches

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
            return {"new": 0, "rejected": 0, "models": 0}

        now_utc = datetime.now(timezone.utc)
        total_new = 0
        total_rejected = 0

        for model in models:
            # Jeux de mots-clés acceptés (base OU une variante, chacun un ET
            # strict après canonicalisation des synonymes — voir keywords.py)
            # — vérifier uniquement les keywords_rules de base rejetterait les
            # résultats trouvés PAR une variante qui n'utilise pas les mêmes
            # mots (contradictoire avec le but même de la recherche par
            # variantes).
            brand_hint = (
                model.search_name.strip().lower()
                if model.search_type == "brand" and model.search_name
                else None
            )
            keyword_sets = build_keyword_sets(model.keywords_rules, model.search_variants, brand_hint=brand_hint)
            if not keyword_sets:
                continue

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

                        # Rejeter tout résultat qui ne satisfait entièrement aucun
                        # des jeux de mots-clés acceptés pour ce modèle
                        if not any(
                            keyword_set_matches(title_norm, kw_set) for kw_set in keyword_sets
                        ):
                            total_rejected += 1
                            continue

                        price = _extract_price(item.get("price"))
                        brand_name = (item.get("brand_title") or "").strip() or None
                        item_status = item.get("status") or None
                        brand_in_title = _detect_brand_in_title(title_norm, brand_name)
                        favourite_count = int(item.get("favourite_count") or 0)
                        view_count = item.get("view_count")
                        photo_url = None
                        photos = item.get("photos") or []
                        if photos and isinstance(photos[0], dict):
                            photo_url = photos[0].get("url") or photos[0].get("full_size_url")

                        existing = db.execute(
                            text("SELECT id, product_model_id FROM listings WHERE vinted_id=:vid"),
                            {"vid": vinted_id},
                        ).fetchone()

                        if existing:
                            # Rattacher au modèle si pas encore fait + backfill des champs
                            # manquants (annonces trouvées avant ce fix, qui n'avaient
                            # jamais brand/état/photo — voir historique du bug)
                            db.execute(
                                text(
                                    """
                                    UPDATE listings
                                    SET product_model_id = COALESCE(product_model_id, :mid),
                                        brand = COALESCE(brand, :brand),
                                        item_status = COALESCE(item_status, :item_status),
                                        photo_url = COALESCE(photo_url, :photo_url)
                                    WHERE id = :lid
                                    """
                                ),
                                {"mid": model.model_id, "lid": existing.id,
                                 "brand": brand_name, "item_status": item_status, "photo_url": photo_url},
                            )
                        else:
                            # Insérer avec product_model_id direct (trouvé via variante,
                            # titre vérifié ci-dessus) — mêmes champs que le snapshot principal
                            seller_info = _extract_seller(item)
                            url = item.get("url") or ""
                            is_sold_from_api = item.get("can_be_sold") is False
                            row = db.execute(
                                text(
                                    """
                                    INSERT INTO listings (
                                        vinted_id, search_id, product_model_id,
                                        title, title_normalized, price,
                                        brand, brand_in_title, item_status,
                                        seller_id, seller_login, url, photo_url,
                                        first_seen_at, last_seen_at, consecutive_absences,
                                        is_sold
                                    ) VALUES (
                                        :vid, :sid, :mid,
                                        :title, :title_norm, :price,
                                        :brand, :brand_in_title, :item_status,
                                        :seller_id, :seller_login, :url, :photo_url,
                                        :now, :now, 0,
                                        :is_sold
                                    ) ON CONFLICT (vinted_id) DO NOTHING
                                    RETURNING id
                                    """
                                ),
                                {
                                    "vid": vinted_id, "sid": model.search_id, "mid": model.model_id,
                                    "title": title, "title_norm": title_norm, "price": price,
                                    "brand": brand_name, "brand_in_title": brand_in_title,
                                    "item_status": item_status,
                                    **seller_info, "url": url, "photo_url": photo_url, "now": now_utc,
                                    "is_sold": is_sold_from_api,
                                },
                            ).fetchone()
                            if row:
                                db.execute(
                                    text(
                                        """
                                        INSERT INTO favourites_snapshots
                                            (listing_id, vinted_id, favourite_count, view_count, snapshot_at)
                                        VALUES (:lid, :vid, :fav, :views, :now)
                                        """
                                    ),
                                    {"lid": row.id, "vid": vinted_id,
                                     "fav": favourite_count, "views": view_count, "now": now_utc},
                                )
                            total_new += 1
                    except Exception:
                        continue

            db.commit()

        result = {"new": total_new, "rejected": total_rejected, "models": len(models)}
        log_to_db(
            "INFO", "scheduler",
            f"Snapshot variantes — {total_new} nouvelles annonces insérées, "
            f"{total_rejected} résultats rejetés (titre ne matchait pas les keywords_rules)",
            result,
        )
        return result

    except Exception as e:
        log_to_db("ERROR", "scheduler", f"Erreur run_variant_snapshots: {e}")
        return {"error": str(e)}
    finally:
        db.close()


def run_stale_refresh(db_session_factory, limit: int = 40):
    """
    Revisite individuellement les annonces suivies (rattachées à un modèle) qui
    n'ont pas été revues depuis plus de 6h — voir collector.py::refresh_stale_listings
    pour le détail. Extrait en fonction de module pour être appelable à la fois
    par le job APScheduler (toutes les heures) et par
    POST /api/admin/refresh-stale-listings (déclenchement manuel).
    """
    from collector import refresh_stale_listings

    db = db_session_factory()
    try:
        return refresh_stale_listings(db, limit=limit)
    except Exception as e:
        log_to_db("ERROR", "scheduler", f"Erreur run_stale_refresh: {e}")
        return {"error": str(e)}
    finally:
        db.close()


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
    scheduler.add_job(
        lambda: run_variant_snapshots(db_session_factory),
        trigger=IntervalTrigger(hours=6),
        id="variant_snapshots",
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: run_stale_refresh(db_session_factory),
        trigger=IntervalTrigger(hours=1),
        id="stale_refresh",
        misfire_grace_time=300,
        replace_existing=True,
    )

    return scheduler
