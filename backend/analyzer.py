"""
Calcul des métriques et scores de signal pour les modèles produit.
Tous les seuils sont lus depuis system_settings via get_setting().
"""

from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import text

from settings import get_setting
from logger import log_to_db

PARIS_TZ = ZoneInfo("Europe/Paris")


# ---------------------------------------------------------------------------
# Fonctions de scoring piecewise
# ---------------------------------------------------------------------------

def _pct_repetition(nb: int) -> float:
    """Pourcentage du poids A — répétition des ventes rapides (0 à 1)."""
    if nb == 0: return 0.0
    if nb == 1: return 0.29
    if nb == 2: return 0.57
    if nb == 3: return 0.80
    return 1.0


def _pct_velocity(ratio: float) -> float:
    """Pourcentage du poids B — vélocité favoris en fav/heure (0 à 1)."""
    if ratio < 0.5:  return 0.0
    if ratio < 1.0:  return 0.32
    if ratio < 2.0:  return 0.64
    return 1.0


def _pct_rarity(avg_active: float) -> float:
    """Pourcentage du poids C — rareté de l'offre (0 à 1)."""
    if avg_active > 5:   return 0.0
    if avg_active > 2:   return 0.35
    if avg_active >= 1:  return 0.70
    return 1.0


def _pct_lifespan(avg_hours: float) -> float:
    """Pourcentage du poids D — durée de vie courte en heures (0 à 1)."""
    if avg_hours > 168: return 0.0   # > 7 jours
    if avg_hours > 72:  return 0.35  # 3-7 jours
    if avg_hours > 24:  return 0.70  # 1-3 jours
    return 1.0                        # < 24h


# ---------------------------------------------------------------------------
# Score de signal
# ---------------------------------------------------------------------------

def calculate_signal_score(model_id: int, db: Session) -> tuple[int, str]:
    """
    Calcule le score de signal composite pour un modèle produit.

    Score brut sur 100 points, calculé sur les 30 derniers jours,
    normalisé par (jours de surveillance / 30) pour les modèles récents.
    Tous les seuils et poids sont lus depuis system_settings.

    Composantes (somme des poids = 100) :
      A) Répétition des ventes rapides   — score_weight_repetition
      B) Vélocité favoris               — score_weight_velocity
      C) Rareté de l'offre              — score_weight_rarity
      D) Durée de vie courte            — score_weight_lifespan

    Coefficient de confiance appliqué sur le score brut :
      - 0 à confidence_low_max observations   → ×0.25, confidence='low'
      - jusqu'à confidence_medium_max          → ×0.60, confidence='medium'
      - au-delà                               → ×1.00, confidence='high'

    Retourne : (score: int, confidence: str)
    """
    w_rep  = get_setting("score_weight_repetition", db)
    w_vel  = get_setting("score_weight_velocity",   db)
    w_rar  = get_setting("score_weight_rarity",     db)
    w_life = get_setting("score_weight_lifespan",   db)
    conf_low_max    = get_setting("confidence_low_max",    db)
    conf_medium_max = get_setting("confidence_medium_max", db)

    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)

    # Ventes rapides (< 72h) sur les 30 derniers jours
    rapid_rows = db.execute(
        text(
            """
            SELECT l.id, l.time_to_disappear_hours, l.first_seen_at
            FROM listings l
            WHERE l.product_model_id = :mid
              AND l.is_sold = true
              AND l.time_to_disappear_hours IS NOT NULL
              AND l.time_to_disappear_hours < 72
              AND l.disappeared_at >= :cutoff
            """
        ),
        {"mid": model_id, "cutoff": cutoff_30d},
    ).fetchall()

    nb_rapid = len(rapid_rows)

    # B) Vélocité favoris : fav au moment de la disparition / heures en ligne
    vel_values = []
    for row in rapid_rows:
        if not row.time_to_disappear_hours or row.time_to_disappear_hours <= 0:
            continue
        # Dernier snapshot favoris avant la disparition
        fav_row = db.execute(
            text(
                """
                SELECT favourite_count
                FROM favourites_snapshots
                WHERE listing_id = :lid
                ORDER BY snapshot_at DESC LIMIT 1
                """
            ),
            {"lid": row.id},
        ).fetchone()
        if fav_row and fav_row.favourite_count:
            vel_values.append(fav_row.favourite_count / row.time_to_disappear_hours)

    avg_velocity = sum(vel_values) / len(vel_values) if vel_values else 0.0

    # C) Rareté : moyenne des annonces actives simultanément (search_snapshots)
    model_row = db.execute(
        text("SELECT search_id FROM product_models WHERE id = :mid"),
        {"mid": model_id},
    ).fetchone()

    avg_active = 0.0
    if model_row and model_row.search_id:
        snap = db.execute(
            text(
                """
                SELECT AVG(active_listings_count) AS avg_active
                FROM search_snapshots
                WHERE search_id = :sid AND snapshot_at >= :cutoff
                """
            ),
            {"sid": model_row.search_id, "cutoff": cutoff_30d},
        ).fetchone()
        avg_active = float(snap.avg_active or 0)

    # D) Durée de vie moyenne
    all_sold = db.execute(
        text(
            """
            SELECT AVG(time_to_disappear_hours) AS avg_life
            FROM listings
            WHERE product_model_id = :mid
              AND is_sold = true
              AND time_to_disappear_hours IS NOT NULL
              AND disappeared_at >= :cutoff
            """
        ),
        {"mid": model_id, "cutoff": cutoff_30d},
    ).fetchone()
    avg_life = float(all_sold.avg_life or 999) if all_sold else 999.0

    # Jours de surveillance du modèle
    obs_row = db.execute(
        text(
            "SELECT observation_days, created_at FROM product_models WHERE id = :mid"
        ),
        {"mid": model_id},
    ).fetchone()
    obs_days = obs_row.observation_days if obs_row else 0
    if not obs_days and obs_row and obs_row.created_at:
        created = obs_row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        obs_days = max(1, (datetime.now(timezone.utc) - created).days)

    # Score brut (0–100)
    raw_score = (
        w_rep  * _pct_repetition(nb_rapid)
        + w_vel  * _pct_velocity(avg_velocity)
        + w_rar  * _pct_rarity(avg_active)
        + w_life * _pct_lifespan(avg_life)
    )

    # Normalisation pour les modèles récents (< 30 jours)
    obs_ratio = min(1.0, obs_days / 30.0)
    if obs_ratio > 0:
        raw_score *= obs_ratio

    # Coefficient de confiance
    if nb_rapid <= conf_low_max:
        coeff, confidence = 0.25, "low"
    elif nb_rapid <= conf_medium_max:
        coeff, confidence = 0.60, "medium"
    else:
        coeff, confidence = 1.00, "high"

    final_score = min(100, max(0, round(raw_score * coeff)))

    # Mettre à jour le modèle en base
    db.execute(
        text(
            """
            UPDATE product_models
            SET signal_score = :score, score_confidence = :conf,
                observation_days = :obs, updated_at = NOW()
            WHERE id = :mid
            """
        ),
        {"score": final_score, "conf": confidence,
         "obs": obs_days, "mid": model_id},
    )
    db.commit()

    return (final_score, confidence)


def recalculate_scores_for_search(search_id: int, db: Session) -> int:
    """
    Recalcule les scores de signal pour tous les modèles actifs d'une recherche.
    Retourne le nombre de modèles recalculés.
    """
    models = db.execute(
        text(
            "SELECT id FROM product_models WHERE search_id = :sid AND is_active = true"
        ),
        {"sid": search_id},
    ).fetchall()

    count = 0
    for m in models:
        try:
            calculate_signal_score(m.id, db)
            count += 1
        except Exception as e:
            log_to_db(
                "WARNING", "analyzer",
                f"Erreur recalcul score modèle #{m.id}: {e}",
                {"model_id": m.id, "search_id": search_id},
            )
    return count


def recalculate_all_scores(db: Session) -> int:
    """
    Recalcule les scores de signal pour TOUS les modèles actifs.
    Appelé par POST /api/admin/recalculate-scores.
    Retourne le nombre de modèles recalculés.
    """
    models = db.execute(
        text("SELECT id FROM product_models WHERE is_active = true")
    ).fetchall()

    count = 0
    for m in models:
        try:
            calculate_signal_score(m.id, db)
            count += 1
        except Exception as e:
            log_to_db(
                "WARNING", "analyzer",
                f"Erreur recalcul score modèle #{m.id}: {e}",
                {"model_id": m.id},
            )
    return count


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

def calculate_heatmap(model_id: int, db: Session) -> list[dict]:
    """
    Calcule la heatmap 7 jours × 24 heures pour un modèle.

    Source : uniquement les listings is_sold=true avec
    time_to_disappear_hours < heatmap_max_lifespan_hours.

    Conversion obligatoire UTC → Europe/Paris avant tout calcul.
    Un créneau retourne avg_velocity=null s'il a moins de
    heatmap_min_observations annonces.

    Retourne :
        [{"day": 0-6, "hour": 0-23, "avg_velocity": float|None, "sample_size": int}, ...]
    """
    max_lifespan = get_setting("heatmap_max_lifespan_hours", db)
    min_obs      = get_setting("heatmap_min_observations",   db)

    rows = db.execute(
        text(
            """
            SELECT l.first_seen_at, l.time_to_disappear_hours,
                   l.published_hour_utc, l.published_day_of_week
            FROM listings l
            WHERE l.product_model_id = :mid
              AND l.is_sold = true
              AND l.time_to_disappear_hours IS NOT NULL
              AND l.time_to_disappear_hours < :max_life
              AND l.time_to_disappear_hours > 0
            """
        ),
        {"mid": model_id, "max_life": max_lifespan},
    ).fetchall()

    # Calcul des vélocités avec conversion de fuseau horaire
    slot_data: dict[tuple[int, int], list[float]] = {}

    for row in rows:
        # Favoris au moment de la disparition (dernier snapshot)
        fav_row = db.execute(
            text(
                """
                SELECT fs.favourite_count
                FROM favourites_snapshots fs
                JOIN listings l ON l.id = fs.listing_id
                WHERE l.product_model_id = :mid
                  AND l.first_seen_at = :fsa
                ORDER BY fs.snapshot_at DESC LIMIT 1
                """
            ),
            {"mid": model_id, "fsa": row.first_seen_at},
        ).fetchone()

        fav_count = fav_row.favourite_count if fav_row else 0
        velocity = fav_count / row.time_to_disappear_hours

        # Conversion UTC → Europe/Paris pour obtenir le bon jour+heure
        first_seen = row.first_seen_at
        if first_seen is None:
            continue
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)

        paris_dt = first_seen.astimezone(PARIS_TZ)
        paris_day  = paris_dt.weekday()  # 0=lundi
        paris_hour = paris_dt.hour

        key = (paris_day, paris_hour)
        slot_data.setdefault(key, []).append(velocity)

    # Construire la grille 7×24
    result = []
    for day in range(7):
        for hour in range(24):
            values = slot_data.get((day, hour), [])
            n = len(values)
            result.append(
                {
                    "day": day,
                    "hour": hour,
                    "avg_velocity": round(sum(values) / n, 3) if n >= min_obs else None,
                    "sample_size": n,
                }
            )

    return result
