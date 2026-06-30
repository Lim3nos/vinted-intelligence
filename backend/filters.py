"""
Filtres des 5 niveaux appliqués sur les résultats bruts du scraping.
Tous les seuils des niveaux 3, 4, 5 sont lus depuis system_settings.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from settings import get_setting

MASS_MARKET_BRANDS = {
    "zara", "h&m", "hm", "mango", "shein", "primark", "bershka",
    "pull and bear", "stradivarius", "asos", "boohoo", "pretty little thing",
    "forever 21", "c&a", "kiabi", "la redoute",
}

STATUS_RANK = {
    "Neuf avec étiquette": 5,
    "Neuf sans étiquette": 4,
    "Très bon état": 3,
    "Bon état": 2,
    "Satisfaisant": 1,
}


def build_filter_levels(db: Session) -> dict:
    """
    Construit la structure des 5 niveaux en lisant les seuils calibrables
    depuis system_settings à CHAQUE appel. Ne jamais mettre ces valeurs
    en constante globale — l'utilisateur peut les avoir modifiées depuis Retool.
    """
    return {
        1: {
            "min_favourites": 0, "max_active_listings": None,
            "require_brand": False, "min_status": None,
            "exclude_mass_market": False, "min_fav_velocity": None,
        },
        2: {
            "min_favourites": 1, "max_active_listings": None,
            "require_brand": False, "min_status": None,
            "exclude_mass_market": False, "min_fav_velocity": None,
        },
        3: {
            "min_favourites": get_setting("level3_min_favourites", db),
            "max_active_listings": None,
            "require_brand": True,
            "min_status": get_setting("level3_min_status", db),
            "exclude_mass_market": True,
            "min_fav_velocity": None,
        },
        4: {
            "min_favourites": get_setting("level4_min_favourites", db),
            "max_active_listings": None,
            "require_brand": True,
            "min_status": get_setting("level4_min_status", db),
            "exclude_mass_market": True,
            "min_fav_velocity": None,
        },
        5: {
            "min_favourites": get_setting("level5_min_favourites", db),
            "max_active_listings": get_setting("level5_max_active_listings", db),
            "require_brand": True,
            "min_status": get_setting("level4_min_status", db),
            "exclude_mass_market": True,
            "min_fav_velocity": get_setting("level5_min_fav_velocity", db),
            "require_recent_favourites": True,
            "recent_favourites_days": get_setting("level5_recent_fav_days", db),
            "recent_favourites_min": get_setting("level5_recent_fav_min", db),
        },
    }


def _extract_item_fields(item: dict) -> dict:
    """Extrait les champs nécessaires d'un item brut de l'API Vinted."""
    price_raw = item.get("price")
    if isinstance(price_raw, dict):
        price = float(price_raw.get("amount") or 0)
    else:
        price = float(price_raw or 0)

    user = item.get("user") or {}
    seller_id = user.get("id") if isinstance(user, dict) else None

    return {
        "vinted_id": item.get("id"),
        "title": item.get("title") or "",
        "price": price,
        "favourite_count": int(item.get("favourite_count") or 0),
        "brand_title": (item.get("brand_title") or "").strip().lower(),
        "status": item.get("status") or "",
        "seller_id": seller_id,
    }


def _passes_level(fields: dict, criteria: dict, db: Optional[Session] = None) -> bool:
    """
    Vérifie si un item passe les critères d'un niveau donné.

    Paramètres :
        fields   : champs extraits de l'item
        criteria : dict du niveau (issu de build_filter_levels)
        db       : session SQLAlchemy (requis pour le niveau 5 max_active_listings)
    """
    # Favoris minimum
    if fields["favourite_count"] < criteria["min_favourites"]:
        return False

    # Marque requise
    if criteria["require_brand"] and not fields["brand_title"]:
        return False

    # Exclure les marques mass-market
    if criteria["exclude_mass_market"]:
        if fields["brand_title"] in MASS_MARKET_BRANDS:
            return False

    # État minimum
    min_status = criteria.get("min_status")
    if min_status:
        item_rank = STATUS_RANK.get(fields["status"], 0)
        required_rank = STATUS_RANK.get(min_status, 0)
        if item_rank < required_rank:
            return False

    # Niveau 5 — max annonces actives simultanées (approximation en base)
    max_active = criteria.get("max_active_listings")
    if max_active is not None and db is not None:
        title = fields["title"].lower()
        words = [w for w in title.split() if len(w) > 3][:2]
        if len(words) >= 2:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
            count_row = db.execute(
                text(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM listings
                    WHERE is_sold = false
                      AND last_seen_at >= :cutoff
                      AND title_normalized ILIKE :kw1
                      AND title_normalized ILIKE :kw2
                    """
                ),
                {"cutoff": cutoff, "kw1": f"%{words[0]}%", "kw2": f"%{words[1]}%"},
            ).fetchone()
            if count_row and count_row.cnt > max_active:
                return False

    # Niveau 5 — favoris récents (proxy : favs actuels >= min si pas d'historique)
    if criteria.get("require_recent_favourites"):
        recent_min = criteria.get("recent_favourites_min", 10)
        if fields["favourite_count"] < recent_min:
            return False

    return True


def apply_filter_level(items: list, level: int, db: Session) -> list:
    """
    Applique les critères du niveau donné à une liste d'annonces brutes.
    Appelle build_filter_levels(db)[level] à chaque appel — jamais de cache.

    Retourne la liste filtrée.
    """
    criteria = build_filter_levels(db)[level]
    result = []
    for item in items:
        fields = _extract_item_fields(item)
        if _passes_level(fields, criteria, db if level == 5 else None):
            result.append(item)
    return result


def apply_all_filter_levels(items: list, db: Session) -> dict:
    """
    Applique les 5 niveaux sur les mêmes données brutes.

    Retourne :
    {
        "level_1": [...],
        "level_2": [...],
        "level_3": [...],
        "level_4": [...],
        "level_5": [...],
        "total_scraped": int
    }
    """
    levels = build_filter_levels(db)
    return {
        **{
            f"level_{i}": [
                item for item in items
                if _passes_level(_extract_item_fields(item), levels[i], db if i == 5 else None)
            ]
            for i in range(1, 6)
        },
        "total_scraped": len(items),
    }
