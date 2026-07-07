"""
Scraping Vinted et gestion des snapshots.
Utilise curl-cffi (impersonnation Chrome) pour contourner Cloudflare.
Gère la collecte des annonces, la déduplication, la détection de disparitions.
"""

import os
import re
import time
import random
import logging
import unicodedata
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import text
from curl_cffi.requests import Session as CurlSession

from logger import log_to_db

logger = logging.getLogger("vinted.collector")

# Distinction critique entre échec réseau et zéro résultat
RESULT_ERROR = None   # Échec de la requête — ne pas toucher aux données existantes
RESULT_EMPTY = []     # Zéro résultat réel — continuer normalement

BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr")

# Session curl-cffi partagée (réutilisée entre les requêtes pour garder les cookies)
_curl_session: Optional[CurlSession] = None
_cookie_fetched_at: Optional[float] = None
_COOKIE_TTL_SECONDS = 10 * 3600  # renouveler le cookie toutes les 10h

# Cache in-process du token Vinted authentifié
_auth_token_cache: Optional[str] = None
_auth_token_expires_at: int = 0


def _get_curl_session() -> CurlSession:
    """
    Retourne la session curl-cffi, en renouvelant le cookie anonyme si nécessaire.
    Impersonne Chrome pour passer Cloudflare.
    """
    global _curl_session, _cookie_fetched_at

    now = time.monotonic()
    needs_refresh = (
        _curl_session is None
        or _cookie_fetched_at is None
        or (now - _cookie_fetched_at) > _COOKIE_TTL_SECONDS
    )

    if needs_refresh:
        session = CurlSession(impersonate="chrome")
        # Obtenir le token anonyme Vinted depuis la page d'accueil
        try:
            r = session.get(
                BASE_URL + "/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "DNT": "1",
                },
                timeout=15,
            )
            if r.status_code == 200:
                logger.info("Cookie Vinted anonyme obtenu avec succès")
                _cookie_fetched_at = now
            else:
                logger.warning("Fetch cookie Vinted status %d", r.status_code)
        except Exception as e:
            logger.error("Impossible de récupérer le cookie Vinted : %s", e)
        _curl_session = session

    return _curl_session


# ---------------------------------------------------------------------------
# Auth Vinted (token utilisateur authentifié)
# ---------------------------------------------------------------------------

def _get_auth_token(db: Optional[Session] = None) -> Optional[str]:
    """
    Retourne le access_token_web authentifié stocké en DB s'il est valide.
    Cache in-process pour éviter une requête DB à chaque appel.
    Si le refresh_token_web est disponible, tente un renouvellement automatique.
    Retourne None si aucun token valide disponible.
    """
    global _auth_token_cache, _auth_token_expires_at

    now_ts = int(time.time())

    # Cache in-process valide (garde une marge de 60s)
    if _auth_token_cache and _auth_token_expires_at > now_ts + 60:
        return _auth_token_cache

    if db is None:
        return _auth_token_cache if _auth_token_cache and _auth_token_expires_at > now_ts else None

    try:
        rows = db.execute(text("SELECT key, value FROM vinted_auth")).fetchall()
        data = {r.key: r.value for r in rows}

        access_tok = data.get("access_token")
        refresh_tok = data.get("refresh_token")
        exp_str = data.get("expires_at")
        exp_ts = int(exp_str) if exp_str else 0

        # Token encore valide
        if access_tok and exp_ts > now_ts + 60:
            _auth_token_cache = access_tok
            _auth_token_expires_at = exp_ts
            return access_tok

        # Token expiré mais refresh_token disponible → cookie rotation
        if refresh_tok and exp_ts > 0:
            new_token, new_exp = _try_refresh_token(refresh_tok)
            if new_token and new_exp:
                db.execute(
                    text("INSERT INTO vinted_auth (key, value, updated_at) VALUES ('access_token', :v, NOW()) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()"),
                    {"v": new_token},
                )
                db.execute(
                    text("INSERT INTO vinted_auth (key, value, updated_at) VALUES ('expires_at', :v, NOW()) ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=NOW()"),
                    {"v": str(new_exp)},
                )
                db.commit()
                _auth_token_cache = new_token
                _auth_token_expires_at = new_exp
                logger.info("Token Vinted renouvelé automatiquement via refresh_token")
                return new_token

    except Exception as e:
        logger.debug("Erreur lecture token auth : %s", e)

    return None


def _try_refresh_token(refresh_token: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Tente de renouveler l'access_token via cookie rotation Vinted.
    Envoie une requête à la homepage avec refresh_token_web → Vinted retourne
    un nouveau access_token_web dans Set-Cookie si le refresh token est valide.
    Retourne (new_access_token, exp_timestamp) ou (None, None).
    """
    try:
        s = CurlSession(impersonate="chrome")
        s.cookies.set("refresh_token_web", refresh_token, domain="www.vinted.fr")
        resp = s.get(
            BASE_URL + "/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            timeout=15,
        )
        new_tok = resp.cookies.get("access_token_web")
        if not new_tok:
            return None, None

        # Décoder l'expiration du nouveau JWT
        import base64 as _b64, json as _json
        parts = new_tok.split(".")
        if len(parts) >= 2:
            pad = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = _json.loads(_b64.b64decode(pad))
            exp = payload.get("exp")
            scope = payload.get("scope", "")
            # Vérifier que c'est un token user (pas anonyme)
            if scope == "user" and exp:
                return new_tok, exp
    except Exception as e:
        logger.debug("Erreur refresh token : %s", e)
    return None, None


def _check_item_sold_via_html(vinted_id: int) -> Optional[bool]:
    """
    Vérifie le statut vendu en scrapant la page HTML publique de l'item.

    Vinted est un SPA React qui embarque l'état initial en JSON dans le HTML.
    Le champ 'is_closed' (True=vendu) est accessible sans auth dans ce JSON.
    Fonctionne depuis Railway sans auth, sur tous les items publics.

    Returns:
        True  → item vendu (is_closed=true ou page 404)
        False → item encore actif (is_closed=false)
        None  → vérification impossible (erreur réseau, timeout, 429)
    """
    try:
        session = _get_curl_session()
        resp = session.get(
            f"{BASE_URL}/items/{vinted_id}",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Referer": BASE_URL + "/catalog",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
            timeout=20,
        )

        if resp.status_code == 429:
            return None
        if resp.status_code == 404:
            return True   # page introuvable = item supprimé/vendu
        if resp.status_code != 200:
            return None

        # Extraire is_closed depuis le JSON embarqué dans le HTML.
        # Vinted sérialise parfois en JSON échappé (\"is_closed\") dans les scripts,
        # parfois en JSON brut ("is_closed") — les deux patterns doivent être testés.
        for pattern in (
            r'"is_closed"\s*:\s*(true|false)',       # JSON brut
            r'\\"is_closed\\"\s*:\s*(true|false)',   # JSON échappé dans attribut HTML
            r'is_closed.{0,8}(true|false)',           # loose match
        ):
            m = re.search(pattern, resp.text)
            if m:
                return m.group(1) == "true"

        # Fallback : chercher can_be_sold
        for pattern2 in (
            r'"can_be_sold"\s*:\s*(true|false)',
            r'\\"can_be_sold\\"\s*:\s*(true|false)',
            r'can_be_sold.{0,8}(true|false)',
        ):
            m2 = re.search(pattern2, resp.text)
            if m2:
                can_be_sold = m2.group(1) == "true"
                return not can_be_sold  # can_be_sold=false → vendu=True

        logger.debug("HTML item %s: is_closed non trouvé dans %d bytes", vinted_id, len(resp.text))
        return None

    except Exception as e:
        logger.debug("Erreur HTML item check %s: %s", vinted_id, e)
        return None


# ---------------------------------------------------------------------------
# Nettoyage des titres
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """
    Normalise un titre d'annonce Vinted pour la déduplication et le clustering.

    - Convertit en minuscules
    - Supprime les emojis et caractères non-ASCII
    - Supprime les caractères spéciaux sauf espaces et tirets
    - Normalise les espaces multiples
    - Tronque à 500 caractères maximum

    Retourne le titre normalisé (str).
    """
    if not title:
        return ""
    title = unicodedata.normalize("NFKD", title)
    title = "".join(
        c for c in title
        if not unicodedata.category(c).startswith("So") and ord(c) < 128
    )
    title = title.lower()
    title = re.sub(r"[^\w\s\-]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:500]


# ---------------------------------------------------------------------------
# Déduplication
# ---------------------------------------------------------------------------

def _jaccard_similarity(a: str, b: str) -> float:
    """Similarité de Jaccard sur les ensembles de mots. Retourne 0.0–1.0."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def is_duplicate(item: dict, db: Session) -> bool:
    """
    Détecte si une annonce est un doublon d'une annonce existante.

    Une annonce est un doublon si :
    - Même seller_id
    - ET même prix (à 1€ près)
    - ET titre normalisé identique à >= 85% (similarité de Jaccard sur les mots)
    - ET l'annonce originale a été vue dans les 7 derniers jours

    Paramètres :
        item : dict brut de l'API Vinted
        db   : session SQLAlchemy active

    Retourne True si doublon détecté, False sinon.
    """
    user = item.get("user") or {}
    seller_id = user.get("id") if isinstance(user, dict) else None
    price = item.get("price")
    if isinstance(price, dict):
        price = price.get("amount")
    title = item.get("title") or ""

    if not seller_id or price is None:
        return False

    title_norm = normalize_title(title)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    rows = db.execute(
        text(
            """
            SELECT title_normalized, price
            FROM listings
            WHERE seller_id = :seller_id
              AND last_seen_at >= :cutoff
              AND ABS(price - :price) <= 1
              AND is_sold = false
            """
        ),
        {"seller_id": seller_id, "cutoff": cutoff, "price": float(price)},
    ).fetchall()

    for row in rows:
        if _jaccard_similarity(title_norm, row.title_normalized or "") >= 0.85:
            return True

    return False


# ---------------------------------------------------------------------------
# Requêtes Vinted avec retry (curl-cffi)
# ---------------------------------------------------------------------------

_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/catalog",
    "Origin": BASE_URL,
    "DNT": "1",
}


def safe_request(params: dict, max_retries: int = 3) -> Optional[list]:
    """
    Effectue une requête Vinted avec retry et délai aléatoire anti-blocage.
    Utilise curl-cffi pour impersonner Chrome et passer Cloudflare.

    Retourne :
        - None  si la requête a échoué (erreur réseau, blocage persistant)
        - []    si la requête a réussi mais Vinted retourne zéro résultat
        - list  si des résultats sont trouvés (dicts bruts de l'API)

    IMPORTANT : ne jamais confondre None et [] dans le code appelant.
    """
    for attempt in range(max_retries):
        try:
            delay = random.uniform(2.5, 5.0) * (1 + attempt * 0.5)
            time.sleep(delay)

            session = _get_curl_session()
            resp = session.get(
                f"{BASE_URL}/api/v2/catalog/items",
                params=params,
                headers=_API_HEADERS,
                timeout=20,
            )

            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items") or []
                return items if items else RESULT_EMPTY

            # Capturer les détails pour diagnostic (Cloudflare, blocage IP, etc.)
            cf_header = resp.headers.get("cf-mitigated", "")
            body_snippet = resp.text[:300] if hasattr(resp, "text") else ""
            log_to_db(
                "WARNING", "collector",
                f"Réponse Vinted inattendue status={resp.status_code}",
                {
                    "attempt": attempt,
                    "status": resp.status_code,
                    "cf_mitigated": cf_header,
                    "body_snippet": body_snippet,
                    "params": {k: v for k, v in params.items() if k != "page"},
                },
            )

            if resp.status_code in (401, 403):
                global _cookie_fetched_at
                _cookie_fetched_at = None
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) * 10)
            else:
                return RESULT_ERROR

        except Exception as e:
            log_to_db(
                "ERROR", "collector",
                f"Erreur réseau tentative {attempt + 1}/{max_retries}: {e}",
                {"attempt": attempt},
            )
            if attempt < max_retries - 1:
                time.sleep((2 ** attempt) * 5)

    return RESULT_ERROR


def safe_request_paginated(params: dict, max_pages: int = 5) -> Optional[list]:
    """
    Gère la pagination Vinted automatiquement.
    Vinted retourne maximum 96 résultats par page.

    Paramètres :
        max_pages : nombre maximum de pages (défaut 5 = 480 résultats max)

    Retourne None si la première page échoue, sinon la liste complète.
    """
    all_items = []

    for page in range(1, max_pages + 1):
        page_params = {**params, "page": page, "per_page": 96}
        result = safe_request(page_params)

        if result is None:
            if page == 1:
                return RESULT_ERROR
            logger.warning("Pagination arrêtée à la page %d sur erreur", page)
            break

        if len(result) == 0:
            break

        all_items.extend(result)

        if len(result) < 96:
            break

    return all_items


# ---------------------------------------------------------------------------
# Helpers extraction données brutes API Vinted
# ---------------------------------------------------------------------------

def _extract_price(price_field) -> Optional[Decimal]:
    """Vinted retourne price comme dict {'amount': '200.0', 'currency_code': 'EUR'}."""
    if price_field is None:
        return None
    if isinstance(price_field, dict):
        raw = price_field.get("amount")
    else:
        raw = price_field
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _extract_seller(item: dict) -> dict:
    """Extrait les infos vendeur depuis un item brut de l'API Vinted."""
    user = item.get("user") or {}
    return {
        "seller_id": user.get("id"),
        "seller_login": user.get("login"),
        "seller_total_sales": user.get("positive_feedback_count"),
        "seller_feedback_score": user.get("feedback_reputation"),
    }


def _detect_brand_in_title(title_normalized: str, brand: Optional[str]) -> bool:
    """Détecte si une marque est présente dans le titre normalisé."""
    if not title_normalized:
        return False
    if brand and brand.lower() in title_normalized:
        return True
    return False


def _extract_title_keywords(title: str) -> str:
    """Extrait 3 mots clés distinctifs du titre pour la recherche catalog."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    words = [w for w in t.split() if len(w) >= 3]
    return " ".join(words[:3])


def _check_seller_still_has_item(vinted_id: int, seller_id: Optional[int], title: str) -> Optional[bool]:
    """
    Vérifie si l'item est encore dans le catalogue public du vendeur.

    Recherche ciblée : catalog/items?user_id={seller_id}&search_text={mots_titre}
    L'API catalog est accessible anonymement — contrairement à l'API item detail.

    Returns:
        True  → item trouvé dans le catalog du vendeur → encore actif, ne pas marquer vendu
        False → item non trouvé → probablement vendu ou retiré
        None  → vérification impossible (rate limit, erreur, seller_id manquant) → conservateur
    """
    if not seller_id:
        return None
    search_words = _extract_title_keywords(title)
    if not search_words:
        return None
    try:
        time.sleep(random.uniform(0.5, 1.5))
        session = _get_curl_session()
        resp = session.get(
            f"{BASE_URL}/api/v2/catalog/items",
            params={
                "user_id": seller_id,
                "search_text": search_words,
                "per_page": 96,
                "page": 1,
                "order": "newest_first",
            },
            headers={**_API_HEADERS, "Referer": f"{BASE_URL}/member/{seller_id}/items"},
            timeout=15,
        )
        if resp.status_code == 429:
            return None  # Rate limited — conservateur
        if resp.status_code != 200:
            return None  # Erreur — conservateur
        items = resp.json().get("items", [])
        item_ids = {it.get("id") for it in items}
        return vinted_id in item_ids
    except Exception:
        return None


def _match_model(title_normalized: str, models: list) -> Optional[int]:
    """
    Retourne l'id du product_model dont tous les keywords_rules sont présents
    dans le titre normalisé. En cas de plusieurs matchs, choisit le plus spécifique
    (celui avec le plus de keywords). Retourne None si aucun match.
    """
    import json as _json

    best_id: Optional[int] = None
    best_count = 0

    for m in models:
        raw = m.keywords_rules
        # psycopg2 sur certains envs retourne JSONB en string — parser explicitement
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except (ValueError, TypeError):
                raw = []
        keywords = raw or []
        if not keywords:
            continue
        # Tous les keywords sont obligatoires pour éviter les faux positifs
        mandatory = keywords
        if not all(kw.lower() in title_normalized for kw in mandatory):
            continue
        matches = sum(1 for kw in keywords if kw.lower() in title_normalized)
        if matches > best_count:
            best_count = matches
            best_id = m.id

    return best_id


# ---------------------------------------------------------------------------
# Snapshot complet
# ---------------------------------------------------------------------------

async def run_snapshot(search_id: int, db: Session) -> dict:
    """
    Effectue un snapshot complet pour une recherche donnée.

    Étapes :
    1. Récupérer la recherche depuis la base
    2. Scraper les annonces actives via Vinted
    3. None (erreur) → logger et abandonner sans toucher aux données
    4. [] (zéro résultat) → logger WARNING, continuer avec liste vide
    5. Pour chaque annonce : normaliser, dédupliquer, upsert + favoris + prix
    6. Détecter les disparitions (consecutive_absences → is_sold)
    7. Insérer un enregistrement search_snapshots avec métriques agrégées
    8. Mettre à jour last_snapshot_at sur la recherche
    9. Recalculer les scores des modèles de cette recherche

    Retourne un dict avec les métriques du snapshot.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Récupérer la recherche
    search = db.execute(
        text("SELECT * FROM searches WHERE id = :id AND is_active = true"),
        {"id": search_id},
    ).fetchone()

    if not search:
        log_to_db("ERROR", "collector", f"Recherche {search_id} introuvable ou inactive")
        return {"error": "search_not_found"}

    log_to_db(
        "INFO", "collector",
        f"Snapshot démarré — recherche #{search_id} '{search.name}'",
        {"search_id": search_id},
    )

    # 1b. Charger les product_models actifs de cette recherche pour le matching
    active_models = db.execute(
        text(
            "SELECT id, keywords_rules FROM product_models "
            "WHERE search_id = :sid AND is_active = true"
        ),
        {"sid": search_id},
    ).fetchall()

    # 2. Construire les paramètres de scraping
    scraper_params = {}
    if search.keywords:
        scraper_params["search_text"] = search.keywords
    if search.brand_ids:
        scraper_params["brand_ids"] = search.brand_ids
    if search.catalog_ids:
        scraper_params["catalog_ids"] = search.catalog_ids
    if search.price_min is not None:
        scraper_params["price_from"] = search.price_min
    if search.price_max is not None:
        scraper_params["price_to"] = search.price_max

    # Paramètres extra issus d'une URL Vinted (status_ids, size_ids, color_ids, etc.)
    try:
        import json as _json
        extra = search.extra_params
        if isinstance(extra, str):
            extra = _json.loads(extra)
        if isinstance(extra, dict):
            scraper_params.update(extra)
    except Exception:
        pass

    items = safe_request_paginated(scraper_params, max_pages=5)

    # 3. Erreur réseau → abandonner sans toucher aux données existantes
    if items is None:
        log_to_db(
            "ERROR", "collector",
            f"Snapshot #{search_id} abandonné — échec réseau",
            {"search_id": search_id},
        )
        return {"error": "network_failure", "search_id": search_id}

    # 4. Zéro résultat réel
    if len(items) == 0:
        log_to_db(
            "WARNING", "collector",
            f"Snapshot #{search_id} — zéro résultat retourné par Vinted",
            {"search_id": search_id},
        )

    new_count = 0
    updated_count = 0
    sold_from_api_count = 0
    scraped_vinted_ids = set()

    # 5. Traitement de chaque annonce
    for item in items:
        try:
            vinted_id = int(item.get("id", 0))
            if not vinted_id:
                continue

            scraped_vinted_ids.add(vinted_id)

            title = item.get("title") or ""
            title_norm = normalize_title(title)
            price = _extract_price(item.get("price"))
            brand_name = (item.get("brand_title") or "").strip() or None
            item_status = item.get("status") or None

            photo_url = None
            photos = item.get("photos") or []
            if photos and isinstance(photos[0], dict):
                photo_url = photos[0].get("url") or photos[0].get("full_size_url")

            url = item.get("url") or f"{BASE_URL}/items/{vinted_id}"

            created_ts = item.get("created_at_ts")
            pub_dt = None
            if created_ts:
                try:
                    pub_dt = datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
                except Exception:
                    pass

            published_hour_utc = pub_dt.hour if pub_dt else None
            published_day_of_week = pub_dt.weekday() if pub_dt else None

            seller_info = _extract_seller(item)
            brand_in_title = _detect_brand_in_title(title_norm, brand_name)
            favourite_count = int(item.get("favourite_count") or 0)
            view_count = item.get("view_count")

            # Détection vente directe via l'API Vinted (can_be_sold=False → article vendu/réservé)
            is_sold_from_api = item.get("can_be_sold") is False

            # c. Vérifier si l'annonce existe déjà
            existing = db.execute(
                text("SELECT id, price, is_sold, first_seen_at FROM listings WHERE vinted_id = :vid"),
                {"vid": vinted_id},
            ).fetchone()

            # Matching vers un product_model
            matched_model_id = _match_model(title_norm, active_models)

            if not existing:
                # Déduplication avant insertion
                if is_duplicate(item, db):
                    logger.info("Annonce %d ignorée (doublon détecté)", vinted_id)
                    continue

                # d. Nouvelle annonce
                row = db.execute(
                    text(
                        """
                        INSERT INTO listings (
                            vinted_id, search_id, product_model_id,
                            title, title_normalized, price,
                            brand, brand_in_title, item_status,
                            seller_id, seller_login, seller_total_sales, seller_feedback_score,
                            url, photo_url,
                            first_seen_at, last_seen_at, consecutive_absences,
                            published_hour_utc, published_day_of_week
                        ) VALUES (
                            :vinted_id, :search_id, :model_id,
                            :title, :title_norm, :price,
                            :brand, :brand_in_title, :item_status,
                            :seller_id, :seller_login, :seller_total_sales, :seller_feedback_score,
                            :url, :photo_url,
                            :now, :now, 0,
                            :pub_hour, :pub_dow
                        ) RETURNING id
                        """
                    ),
                    {
                        "vinted_id": vinted_id, "search_id": search_id,
                        "model_id": matched_model_id,
                        "title": title, "title_norm": title_norm, "price": price,
                        "brand": brand_name, "brand_in_title": brand_in_title,
                        "item_status": item_status,
                        **seller_info,
                        "url": url, "photo_url": photo_url,
                        "now": now_utc,
                        "pub_hour": published_hour_utc, "pub_dow": published_day_of_week,
                    },
                )
                listing_id = row.fetchone().id

                db.execute(
                    text(
                        """
                        INSERT INTO favourites_snapshots
                            (listing_id, vinted_id, favourite_count, view_count, snapshot_at)
                        VALUES (:lid, :vid, :fav, :views, :now)
                        """
                    ),
                    {"lid": listing_id, "vid": vinted_id,
                     "fav": favourite_count, "views": view_count, "now": now_utc},
                )
                db.execute(
                    text(
                        """
                        INSERT INTO price_history (listing_id, vinted_id, price, recorded_at)
                        VALUES (:lid, :vid, :price, :now)
                        """
                    ),
                    {"lid": listing_id, "vid": vinted_id, "price": price, "now": now_utc},
                )

                if is_sold_from_api:
                    # Annonce déjà vendue dès la première scrape — marquer immédiatement
                    db.execute(
                        text(
                            """
                            UPDATE listings
                            SET is_sold = true, disappeared_at = :now,
                                final_price = :price, time_to_disappear_hours = NULL
                            WHERE id = :lid
                            """
                        ),
                        {"now": now_utc, "price": price, "lid": listing_id},
                    )
                    sold_from_api_count += 1
                else:
                    new_count += 1

            else:
                listing_id = existing.id

                if is_sold_from_api and not existing.is_sold:
                    # Annonce existante qui vient d'être vendue — détecter via l'API
                    fe = existing.first_seen_at
                    if fe and fe.tzinfo is None:
                        fe = fe.replace(tzinfo=timezone.utc)
                    life_hours = (now_utc - fe).total_seconds() / 3600 if fe else None

                    db.execute(
                        text(
                            """
                            UPDATE listings
                            SET is_sold = true, disappeared_at = :now,
                                final_price = :price, time_to_disappear_hours = :life_h,
                                last_seen_at = :now, consecutive_absences = 0,
                                product_model_id = COALESCE(product_model_id, :model_id)
                            WHERE id = :lid
                            """
                        ),
                        {"now": now_utc, "price": price, "life_h": life_hours,
                         "model_id": matched_model_id, "lid": listing_id},
                    )
                    db.execute(
                        text(
                            """
                            INSERT INTO favourites_snapshots
                                (listing_id, vinted_id, favourite_count, view_count, snapshot_at)
                            VALUES (:lid, :vid, :fav, :views, :now)
                            """
                        ),
                        {"lid": listing_id, "vid": vinted_id,
                         "fav": favourite_count, "views": view_count, "now": now_utc},
                    )
                    sold_from_api_count += 1

                else:
                    # e. Annonce existante active : mettre à jour last_seen_at + snapshot favoris
                    db.execute(
                        text(
                            """
                            UPDATE listings
                            SET last_seen_at = :now,
                                consecutive_absences = 0,
                                product_model_id = COALESCE(product_model_id, :model_id)
                            WHERE id = :lid
                            """
                        ),
                        {"now": now_utc, "lid": listing_id, "model_id": matched_model_id},
                    )
                    db.execute(
                        text(
                            """
                            INSERT INTO favourites_snapshots
                                (listing_id, vinted_id, favourite_count, view_count, snapshot_at)
                            VALUES (:lid, :vid, :fav, :views, :now)
                            """
                        ),
                        {"lid": listing_id, "vid": vinted_id,
                         "fav": favourite_count, "views": view_count, "now": now_utc},
                    )

                    # Détecter changement de prix
                    if existing.price is not None and price is not None:
                        if abs(float(existing.price) - float(price)) > 0.01:
                            db.execute(
                                text("UPDATE listings SET price = :price WHERE id = :lid"),
                                {"price": price, "lid": listing_id},
                            )
                            db.execute(
                                text(
                                    """
                                    INSERT INTO price_history (listing_id, vinted_id, price, recorded_at)
                                    VALUES (:lid, :vid, :price, :now)
                                    """
                                ),
                                {"lid": listing_id, "vid": vinted_id, "price": price, "now": now_utc},
                            )

                    updated_count += 1

        except Exception as e:
            logger.error("Erreur traitement annonce %s: %s", item.get("id"), e)
            continue

    db.commit()

    # 6. Détection des disparitions
    # On ne vérifie que les items confirmés dans AU MOINS 2 snapshots distincts
    # (last_seen_at > first_seen_at). Les items vus pour la première fois dans ce
    # snapshot ou le précédent seront vérifiés au prochain cycle — évite les faux
    # positifs sur les recherches larges (marque entière) où on ne peut scraper
    # qu'une fraction du catalogue Vinted.
    disappeared_count = 0
    active_in_db = db.execute(
        text(
            """
            SELECT id, vinted_id, first_seen_at, price, seller_id, title
            FROM listings
            WHERE search_id = :sid
              AND is_sold = false
              AND last_seen_at > first_seen_at
            """
        ),
        {"sid": search_id},
    ).fetchall()

    verify_budget = 12  # max vérifications catalog par snapshot (anti rate-limit)
    has_auth_token = bool(_get_auth_token(db))  # check une seule fois
    for row in active_in_db:
        if row.vinted_id not in scraped_vinted_ids:
            updated = db.execute(
                text(
                    """
                    UPDATE listings
                    SET consecutive_absences = consecutive_absences + 1
                    WHERE id = :lid
                    RETURNING consecutive_absences, first_seen_at, price
                    """
                ),
                {"lid": row.id},
            ).fetchone()

            if updated and updated.consecutive_absences >= 4:
                # Priorité 1 : page HTML item (is_closed dans JSON embarqué, sans auth)
                if verify_budget > 0:
                    html_result = _check_item_sold_via_html(row.vinted_id)
                    verify_budget -= 1
                    if html_result is False:
                        # Item encore actif → reset absences
                        db.execute(
                            text("UPDATE listings SET consecutive_absences = 0 WHERE id = :lid"),
                            {"lid": row.id},
                        )
                        continue
                    if html_result is True:
                        item_gone = True
                    else:
                        # html_result=None → erreur réseau, fallback catalog
                        item_still_active = None
                        if verify_budget > 0:
                            item_still_active = _check_seller_still_has_item(
                                row.vinted_id, row.seller_id, row.title or ""
                            )
                            verify_budget -= 1
                        if item_still_active is True:
                            db.execute(
                                text("UPDATE listings SET consecutive_absences = 0 WHERE id = :lid"),
                                {"lid": row.id},
                            )
                            continue
                        item_gone = (item_still_active is False) or (updated.consecutive_absences >= 8)
                else:
                    # Budget épuisé → fallback absences uniquement
                    item_gone = updated.consecutive_absences >= 8

                if item_gone:
                    first_seen = updated.first_seen_at
                    if first_seen and first_seen.tzinfo is None:
                        first_seen = first_seen.replace(tzinfo=timezone.utc)
                    life_hours = (
                        (now_utc - first_seen).total_seconds() / 3600
                        if first_seen else None
                    )
                    db.execute(
                        text(
                            """
                            UPDATE listings
                            SET is_sold = true,
                                disappeared_at = :now,
                                time_to_disappear_hours = :life_h,
                                final_price = :price
                            WHERE id = :lid
                            """
                        ),
                        {"now": now_utc, "life_h": life_hours,
                         "price": updated.price, "lid": row.id},
                    )
                    disappeared_count += 1

    db.commit()

    # 7. Métriques agrégées pour search_snapshots
    agg = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE is_sold = false) AS active_count,
                AVG(price) FILTER (WHERE is_sold = false) AS avg_price
            FROM listings WHERE search_id = :sid
            """
        ),
        {"sid": search_id},
    ).fetchone()

    avg_fav = db.execute(
        text(
            """
            SELECT AVG(fs.favourite_count) AS avg_fav
            FROM favourites_snapshots fs
            JOIN listings l ON l.id = fs.listing_id
            WHERE l.search_id = :sid AND fs.snapshot_at >= :cutoff
            """
        ),
        {"sid": search_id, "cutoff": now_utc - timedelta(hours=4)},
    ).fetchone()

    db.execute(
        text(
            """
            INSERT INTO search_snapshots
                (search_id, active_listings_count, new_listings_count,
                 disappeared_listings_count, avg_price, avg_favourite_count, snapshot_at)
            VALUES (:sid, :active, :new, :disapp, :avg_price, :avg_fav, :now)
            """
        ),
        {
            "sid": search_id,
            "active": agg.active_count if agg else 0,
            "new": new_count, "disapp": disappeared_count,
            "avg_price": agg.avg_price if agg else None,
            "avg_fav": avg_fav.avg_fav if avg_fav else None,
            "now": now_utc,
        },
    )

    # 8. Mettre à jour last_snapshot_at
    db.execute(
        text("UPDATE searches SET last_snapshot_at = :now WHERE id = :sid"),
        {"now": now_utc, "sid": search_id},
    )
    db.commit()

    # 9. Recalculer les scores
    try:
        from analyzer import recalculate_scores_for_search
        recalculate_scores_for_search(search_id, db)
    except Exception as e:
        log_to_db("WARNING", "collector",
                  f"Recalcul scores échoué: {e}", {"search_id": search_id})

    metrics = {
        "search_id": search_id,
        "search_name": search.name,
        "total_scraped": len(items),
        "new_listings": new_count,
        "updated_listings": updated_count,
        "disappeared": disappeared_count,
        "sold_from_api": sold_from_api_count,
        "active_in_db": agg.active_count if agg else 0,
        "snapshot_at": now_utc.isoformat(),
    }

    log_to_db(
        "INFO", "collector",
        f"Snapshot #{search_id} terminé — {new_count} nouvelles, {disappeared_count} disparues",
        metrics,
    )

    return metrics
