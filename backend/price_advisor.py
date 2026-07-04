"""
Suggesteur de prix basé sur les ventes rapides historiques du modèle.
"""

import statistics
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from logger import log_to_db

STATUS_ADJUSTMENTS = {
    "Neuf avec étiquette":  +0.15,
    "Neuf sans étiquette":  +0.08,
    "Très bon état":         0.00,
    "Bon état":             -0.12,
    "Satisfaisant":         -0.25,
}


def suggest_price(product_model_id: int, my_item_status: str, db: Session) -> dict:
    """
    Calcule le prix de vente optimal pour une annonce.

    Source : ventes rapides (is_sold=true, time_to_disappear_hours < 72h)
    des 60 derniers jours pour ce modèle. Utilise final_price (pas price).

    Algorithme :
    1. Récupérer ventes rapides des 60 derniers jours
    2. Séparer par item_status
    3. Calcul du prix suggéré selon disponibilité des données pour ce statut
    4. Ajustement -5% si > 2 annonces actives en ce moment
    5. Fourchette : [×0.92, ×1.08]
    6. Niveau de confiance selon nb ventes

    Retourne :
        dict avec suggested_price, price_range, confidence, current_competition,
               median_sold_price, sample_size, price_by_status, reasoning, warning
    """
    cutoff_60d = datetime.now(timezone.utc) - timedelta(days=60)

    # Ventes rapides du modèle sur 60 jours
    rapid_sales = db.execute(
        text(
            """
            SELECT final_price, price, item_status, time_to_disappear_hours,
                   title, disappeared_at, seller_login, latest_favs,
                   EXTRACT(EPOCH FROM (disappeared_at - first_seen_at)) / 3600 AS hours_online
            FROM listings
            WHERE product_model_id = :mid
              AND is_sold = true
              AND time_to_disappear_hours IS NOT NULL
              AND time_to_disappear_hours < 72
              AND disappeared_at >= :cutoff
              AND final_price IS NOT NULL
            ORDER BY disappeared_at DESC
            """
        ),
        {"mid": product_model_id, "cutoff": cutoff_60d},
    ).fetchall()

    if not rapid_sales:
        return {
            "suggested_price": None,
            "price_range": None,
            "confidence": "low",
            "current_competition": _count_active(product_model_id, db),
            "median_sold_price": None,
            "sample_size": 0,
            "price_by_status": {},
            "reasoning": "Aucune vente rapide enregistrée pour ce modèle sur les 60 derniers jours.",
            "warning": "Données insuffisantes — prix non calculable.",
        }

    # Grouper par statut
    by_status: dict[str, list[float]] = {}
    all_prices = []
    for row in rapid_sales:
        price = float(row.final_price)
        all_prices.append(price)
        status = row.item_status or "Inconnu"
        by_status.setdefault(status, []).append(price)

    global_median = statistics.median(all_prices)

    # Prix suggéré selon le statut demandé
    status_prices = by_status.get(my_item_status, [])
    if len(status_prices) >= 3:
        base_price = statistics.median(status_prices)
        used_status_data = True
        reasoning_base = (
            f"Médiane calculée sur {len(status_prices)} ventes rapides "
            f"en état « {my_item_status} »."
        )
    else:
        # Médiane globale + ajustement par statut
        adj = STATUS_ADJUSTMENTS.get(my_item_status, 0.0)
        base_price = global_median * (1 + adj)
        used_status_data = False
        reasoning_base = (
            f"Médiane globale ({global_median:.2f}€) ajustée de "
            f"{adj:+.0%} pour l'état « {my_item_status} »"
            f" (moins de 3 ventes dans cet état)."
        )

    # Concurrence actuelle
    current_competition = _count_active(product_model_id, db)
    competition_adj = 0.0
    if current_competition > 2:
        competition_adj = -0.05
        reasoning_competition = f" Descente de 5% (concurrence élevée : {current_competition} annonces actives)."
    else:
        reasoning_competition = f" ({current_competition} annonce(s) active(s) actuellement.)"

    suggested_price = round(base_price * (1 + competition_adj), 2)

    # Fourchette
    price_range = [round(suggested_price * 0.92, 2), round(suggested_price * 1.08, 2)]

    # Confiance
    nb_for_confidence = len(status_prices) if used_status_data else len(all_prices)
    if nb_for_confidence >= 5:
        confidence = "high"
    elif nb_for_confidence >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # Prix par statut (pour affichage Retool)
    price_by_status = {
        s: {"median": round(statistics.median(prices), 2), "count": len(prices)}
        for s, prices in by_status.items()
    }

    warning = None
    if confidence == "low":
        warning = f"Confiance faible ({nb_for_confidence} vente(s)) — prix à titre indicatif uniquement."

    recent_sales = [
        {
            "title": r.title,
            "price": float(r.final_price),
            "item_status": r.item_status,
            "sold_at": r.disappeared_at.isoformat() if r.disappeared_at else None,
            "hours_to_sell": round(float(r.hours_online), 1) if r.hours_online else None,
            "favoris": r.latest_favs,
            "seller": r.seller_login,
        }
        for r in rapid_sales
    ]

    return {
        "suggested_price": suggested_price,
        "price_range": price_range,
        "price_low": price_range[0],
        "price_high": price_range[1],
        "confidence": confidence,
        "current_competition": current_competition,
        "median_sold_price": round(global_median, 2),
        "sample_size": len(all_prices),
        "price_by_status": price_by_status,
        "reasoning": reasoning_base + reasoning_competition,
        "warning": warning,
        "recent_sales": recent_sales,
    }


def _count_active(product_model_id: int, db: Session) -> int:
    """Compte les annonces actives (non vendues) pour un modèle."""
    row = db.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM listings "
            "WHERE product_model_id = :mid AND is_sold = false"
        ),
        {"mid": product_model_id},
    ).fetchone()
    return int(row.cnt) if row else 0
