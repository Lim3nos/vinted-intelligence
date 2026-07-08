"""
Nettoyage et validation des mots-clés de matching produit (keywords_rules),
et matching titre -> modèle partagé entre collector.py, main.py et scheduler.py.

Cause racine du bug des "modèles parasités" : un mot-clé vide ou trop court
matche n'importe quel titre ("" est une sous-chaîne de toute chaîne en
Python), ce qui faisait que le matching assignait alors TOUTES les annonces
d'une recherche au modèle concerné, quel que soit leur contenu réel.
"""

import json
import re
from typing import Optional

MIN_KEYWORD_LENGTH = 2


def sanitize_keywords(keywords: list) -> list:
    """
    Nettoie une liste de mots-clés de matching : strip, lowercase, dédoublonne,
    rejette les entrées vides, non-textuelles, ou trop courtes
    (< MIN_KEYWORD_LENGTH caractères après strip).

    Retourne une nouvelle liste propre. Ne modifie jamais la liste d'origine.
    """
    seen = set()
    cleaned = []
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        kw_clean = kw.strip().lower()
        if len(kw_clean) < MIN_KEYWORD_LENGTH:
            continue
        if kw_clean in seen:
            continue
        seen.add(kw_clean)
        cleaned.append(kw_clean)
    return cleaned


def _parse_json_list(raw) -> list:
    """Désérialise un champ JSONB qui peut arriver sous forme de string (psycopg2) ou déjà de liste."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    return raw or []


def _words_from_phrase(phrase: str) -> list:
    """Découpe une phrase de variante de recherche en mots normalisés (>= MIN_KEYWORD_LENGTH)."""
    if not isinstance(phrase, str):
        return []
    words = re.findall(r"[^\W_]+", phrase.lower(), flags=re.UNICODE)
    return sanitize_keywords(words)


def build_keyword_sets(keywords_rules, search_variants) -> list:
    """
    Construit tous les jeux de mots-clés (ET) acceptés pour un modèle : le jeu
    de base (keywords_rules) et un jeu dérivé de chaque variante de recherche
    générée par Gemini (search_variants).

    Un titre matche le modèle s'il satisfait AU MOINS UN de ces jeux (OR de
    groupes ET) — permet de capter les variantes de nom d'un même produit
    (traduction, orthographe, formulation différente) sans élargir le
    matching à n'importe quel résultat approchant.

    Retourne une liste de listes de mots-clés, dédoublonnée.
    """
    sets = []
    seen = set()

    base = sanitize_keywords(_parse_json_list(keywords_rules))
    if base:
        seen.add(frozenset(base))
        sets.append(base)

    for variant in _parse_json_list(search_variants):
        words = _words_from_phrase(variant)
        if not words:
            continue
        key = frozenset(words)
        if key in seen:
            continue
        seen.add(key)
        sets.append(words)

    return sets


def match_model(title_normalized: str, models: list) -> Optional[int]:
    """
    Retourne l'id du product_model dont au moins un jeu de mots-clés (base ou
    variante, voir build_keyword_sets) est entièrement présent dans le titre
    normalisé. En cas de plusieurs matchs, choisit le jeu le plus spécifique
    (le plus grand nombre de mots-clés). Retourne None si aucun match.

    `models` : itérable d'objets avec attributs `.id`, `.keywords_rules`,
    et `.search_variants` (ce dernier peut être absent/None).
    """
    best_id: Optional[int] = None
    best_count = 0

    for m in models:
        keyword_sets = build_keyword_sets(
            getattr(m, "keywords_rules", None),
            getattr(m, "search_variants", None),
        )
        for kw_set in keyword_sets:
            if kw_set and all(kw in title_normalized for kw in kw_set) and len(kw_set) > best_count:
                best_count = len(kw_set)
                best_id = m.id

    return best_id
