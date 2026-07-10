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

# Groupes de synonymes/traductions (fr/en/it/de) : des mots qui désignent LA
# MÊME CHOSE dans des langues ou formulations différentes (ex: pendentif =
# pendant = necklace = collier, observé directement dans les variantes
# générées par Gemini pour un même produit). Chaque mot d'un groupe est
# canonicalisé vers le premier terme du groupe avant tout matching.
#
# Contrairement à un assouplissement du ET (accepter un match partiel), ceci
# ne relâche JAMAIS la précision : un titre doit toujours satisfaire TOUS les
# mots du jeu de mots-clés, juste sous n'importe laquelle de leurs variantes
# linguistiques. Ça évite de rater "whistle necklace" quand le jeu attend
# "whistle pendant", sans jamais risquer qu'un mot générique isolé (une
# couleur, une matière, une catégorie de produit) suffise à lui seul à
# confirmer un modèle — un sac et un pendentif peuvent tous les deux être
# "noir" ou en "laine", ça ne les rend pas interchangeables.
SYNONYM_GROUPS = [
    {"pendentif", "pendant", "necklace", "collier", "ciondolo", "collana"},
    {"wool", "laine", "lana", "wolle"},
    {"trousers", "pantalon", "pants", "pantaloni", "hose"},
    {"bag", "sac", "borsa"},
    {"shoes", "chaussures", "chaussure", "scarpe"},
    {"jacket", "veste", "giacca"},
    {"dress", "robe", "vestito"},
    {"shirt", "chemise", "camicia"},
    {"blouse", "chemisier", "blusa", "camicetta"},
    {"tank", "débardeur"},
    {"skirt", "jupe", "gonna"},
    {"coat", "manteau", "cappotto"},
    {"belt", "ceinture", "cintura"},
    {"scarf", "echarpe", "sciarpa"},
    {"gloves", "gants", "guanti"},
    {"wallet", "portefeuille", "portafoglio"},
    {"leather", "cuir", "pelle", "leder"},
    {"silk", "soie", "seta"},
    {"cotton", "coton", "cotone"},
]

_CANONICAL_MAP: dict = {}
for _group in SYNONYM_GROUPS:
    _canon = sorted(_group)[0]
    for _word in _group:
        _CANONICAL_MAP[_word] = _canon


def _canonicalize(word: str) -> str:
    """Remplace un mot par sa forme canonique s'il appartient à un groupe de synonymes."""
    return _CANONICAL_MAP.get(word, word)


def sanitize_keywords(keywords: list) -> list:
    """
    Nettoie une liste de mots-clés de matching : strip, lowercase, canonicalise
    les synonymes (voir SYNONYM_GROUPS), dédoublonne, rejette les entrées
    vides, non-textuelles, ou trop courtes (< MIN_KEYWORD_LENGTH caractères).

    Retourne une nouvelle liste propre. Ne modifie jamais la liste d'origine.
    """
    seen = set()
    cleaned = []
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        kw_clean = _canonicalize(kw.strip().lower())
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
    """Découpe une phrase (variante de recherche ou titre) en mots normalisés
    et canonicalisés (>= MIN_KEYWORD_LENGTH)."""
    if not isinstance(phrase, str):
        return []
    words = re.findall(r"[^\W_]+", phrase.lower(), flags=re.UNICODE)
    return sanitize_keywords(words)


def build_keyword_sets(keywords_rules, search_variants, brand_hint: Optional[str] = None) -> list:
    """
    Construit tous les jeux de mots-clés (ET) acceptés pour un modèle : le jeu
    de base (keywords_rules) et un jeu dérivé de chaque variante de recherche
    générée par Gemini (search_variants).

    Un titre matche le modèle s'il satisfait ENTIÈREMENT (tous les mots,
    après canonicalisation des synonymes) AU MOINS UN de ces jeux (OR de
    groupes ET) — permet de capter les variantes de nom d'un même produit
    (traduction, orthographe, formulation différente) sans jamais relâcher
    la précision : chaque jeu reste un ET strict sur tous ses mots.

    Filtre en plus les variantes qui ne partagent AUCUN mot distinctif (hors
    marque) avec le jeu de base curé — ex. Gemini a pu générer "marque + top"
    pour un modèle "Blouse" : "top" ne recoupe ni "blouse" ni "chemisier", et
    matcherait n'importe quel haut de la marque (pull, t-shirt, polo...).
    Une variante n'est gardée que si elle recoupe la définition de base,
    `brand_hint` (nom de la recherche si search_type='brand') permettant
    d'exclure la marque elle-même de cette vérification de recoupement.

    Retourne une liste de listes de mots-clés (déjà canonicalisés), dédoublonnée.
    """
    sets = []
    seen = set()

    base = sanitize_keywords(_parse_json_list(keywords_rules))
    base_key = frozenset(base)
    if base:
        seen.add(base_key)
        sets.append(base)

    brand_c = _canonicalize(brand_hint.strip().lower()) if brand_hint else None
    base_distinctive = (base_key - {brand_c}) if brand_c else base_key

    for variant in _parse_json_list(search_variants):
        words = _words_from_phrase(variant)
        if not words:
            continue
        key = frozenset(words)
        if key in seen:
            continue

        if base_distinctive:
            variant_distinctive = key - ({brand_c} if brand_c else set())
            if variant_distinctive.isdisjoint(base_distinctive):
                continue

        seen.add(key)
        sets.append(words)

    return sets


def keyword_set_matches(title_normalized: str, kw_set: list) -> bool:
    """
    Vérifie si un jeu de mots-clés matche entièrement un titre : tous les mots
    du jeu (déjà canonicalisés par build_keyword_sets/sanitize_keywords)
    doivent être présents dans le titre une fois celui-ci lui-même
    canonicalisé — voir SYNONYM_GROUPS pour la logique de canonicalisation.
    """
    if not kw_set:
        return False
    title_words = set(_words_from_phrase(title_normalized))
    return all(kw in title_words for kw in kw_set)


def match_model(title_normalized: str, models: list, brand_hint: Optional[str] = None) -> Optional[int]:
    """
    Retourne l'id du product_model dont au moins un jeu de mots-clés (base ou
    variante, voir build_keyword_sets) matche entièrement le titre normalisé
    (voir keyword_set_matches). En cas de plusieurs matchs, choisit le jeu le
    plus spécifique (le plus grand nombre de mots-clés). Retourne None si
    aucun match.

    `models` : itérable d'objets avec attributs `.id`, `.keywords_rules`,
    et `.search_variants` (ce dernier peut être absent/None).
    `brand_hint` : nom de la recherche parente si search_type='brand' — sert
    uniquement à filtrer les variantes trop génériques dans build_keyword_sets,
    ne relâche jamais le ET strict du matching lui-même.
    """
    best_id: Optional[int] = None
    best_count = 0

    for m in models:
        keyword_sets = build_keyword_sets(
            getattr(m, "keywords_rules", None),
            getattr(m, "search_variants", None),
            brand_hint=brand_hint,
        )
        for kw_set in keyword_sets:
            if keyword_set_matches(title_normalized, kw_set) and len(kw_set) > best_count:
                best_count = len(kw_set)
                best_id = m.id

    return best_id
