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
import unicodedata
from typing import Optional

MIN_KEYWORD_LENGTH = 2


def _strip_accents(text: str) -> str:
    """
    Retire les accents (é -> e, etc.). collector.py::normalize_title() fait de
    même sur les titres avant stockage — les mots-clés doivent subir la même
    normalisation, sinon un mot-clé accentué ("débardeur") ne matche jamais un
    titre normalisé ("debardeur", accent déjà supprimé au stockage).
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

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
    {"sifflet", "whistle", "fischietto", "pfeife"},
    {"wool", "laine", "lana", "wolle"},
    {"trousers", "pantalon", "pants", "pantaloni", "hose"},
    {"bag", "sac", "borsa"},
    {"shoes", "chaussures", "chaussure", "scarpe"},
    {"jacket", "veste", "giacca"},
    {"dress", "robe", "vestito"},
    {"shirt", "chemise", "camicia"},
    {"blouse", "chemisier", "blusa", "camicetta"},
    {"tank", "debardeur"},
    {"body", "bodysuit", "justaucorps"},
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
        kw_clean = _canonicalize(_strip_accents(kw.strip().lower()))
        if len(kw_clean) < MIN_KEYWORD_LENGTH:
            continue
        if kw_clean in seen:
            continue
        seen.add(kw_clean)
        cleaned.append(kw_clean)
    return cleaned


def cap_keywords_for_matching(keywords: list, brand_hint: Optional[str] = None, max_words: int = 2) -> list:
    """
    Réduit une liste de mots-clés suggérés (ex: par le clustering Gemini) à un
    jeu minimal de `max_words` mots pour servir de base ET strict aux futurs
    matchings (keywords_rules).

    Cause du bug corrigé ici : Gemini propose souvent une liste qui mélange un
    mot distinctif ET plusieurs traductions/synonymes de la même idée (ex:
    ["carre","hermes","seta","echarpe"] — "seta"=soie en italien, "echarpe"
    est un quasi-synonyme de "carre"). Si tous ces mots sont exigés ensemble
    (ET strict), un titre normal n'en contient jamais qu'un ou deux à la fois
    et le modèle ne matche presque plus rien. En limitant la base à 2 mots
    (marque + le mot le plus distinctif), le modèle reste précis sans exiger
    une coexistence irréaliste. Les mots écartés restent disponibles pour
    generate_search_variants, qui les traite correctement comme des
    alternatives (OU), pas comme des exigences supplémentaires (ET).
    """
    cleaned = sanitize_keywords(keywords)
    if not cleaned:
        return cleaned

    # brand_hint peut être un nom de marque à plusieurs mots (ex: "isabel
    # marant") — chercher si UN de ses mots apparaît dans les mots-clés
    # suggérés, pas la phrase entière telle quelle.
    brand_words = set(_words_from_phrase(brand_hint)) if brand_hint else set()

    result = []
    for kw in cleaned:
        if kw in brand_words:
            result.append(kw)
            break
    for kw in cleaned:
        if len(result) >= max_words:
            break
        if kw not in result:
            result.append(kw)
    return result[:max_words]


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


def keyword_set_matches(title_normalized: str, kw_set: list, item_brand: Optional[str] = None) -> bool:
    """
    Vérifie si un jeu de mots-clés matche entièrement un titre : tous les mots
    du jeu (déjà canonicalisés par build_keyword_sets/sanitize_keywords)
    doivent être présents dans le titre une fois celui-ci lui-même
    canonicalisé — voir SYNONYM_GROUPS pour la logique de canonicalisation.

    `item_brand` : marque de l'annonce telle que confirmée par Vinted
    (champ `brand_title` de l'API, indépendant du texte du titre). De
    nombreux vendeurs ne retapent pas le nom de la marque dans le titre —
    sans ceci, un mot-clé de marque (ex. "lemaire") ne matcherait jamais
    ces annonces alors que Vinted les identifie bien comme telles. Ajouté
    aux mots du titre avant vérification, jamais utilisé seul.
    """
    if not kw_set:
        return False
    title_words = set(_words_from_phrase(title_normalized))
    if item_brand:
        title_words |= set(_words_from_phrase(item_brand))
    return all(kw in title_words for kw in kw_set)


def build_model_keyword_sets(models: list, brand_hint: Optional[str] = None) -> list:
    """
    Précalcule les jeux de mots-clés (voir build_keyword_sets) de chaque
    modèle UNE SEULE FOIS — à utiliser avant une boucle sur des annonces,
    plutôt que de laisser match_model()/build_keyword_sets() les reconstruire
    à chaque annonce pour chaque modèle.

    Le résultat de build_keyword_sets ne dépend que de keywords_rules/
    search_variants/brand_hint d'un modèle donné — identique pour toutes les
    annonces d'un même snapshot. Sans précalcul, ce coût (parsing JSON,
    tokenisation regex, canonicalisation) est payé O(items × modèles) au lieu
    de O(modèles) : négligeable sur quelques dizaines d'annonces, mais devenu
    le principal goulot d'étranglement mesuré en prod sur une grosse recherche
    (20 000+ annonces à chaque cycle, depuis le découpage par tranches de
    prix) — le run_snapshot correspondant tournait des heures sans avancer,
    tout le temps CPU passé à recalculer inutilement les mêmes jeux de
    mots-clés annonce après annonce.

    Retourne une liste de (model_id, keyword_sets).
    """
    return [
        (
            m.id,
            build_keyword_sets(
                getattr(m, "keywords_rules", None),
                getattr(m, "search_variants", None),
                brand_hint=brand_hint,
            ),
        )
        for m in models
    ]


def match_model_precomputed(
    title_normalized: str,
    precomputed_sets: list,
    item_brand: Optional[str] = None,
) -> Optional[int]:
    """
    Comme match_model(), mais reçoit des jeux de mots-clés déjà calculés (voir
    build_model_keyword_sets) au lieu de les reconstruire pour cette annonce —
    mêmes règles de matching (OR de groupes ET, le plus spécifique gagne),
    juste sans le recalcul redondant. À utiliser dans toute boucle sur
    plusieurs annonces d'un même lot de modèles.
    """
    best_id: Optional[int] = None
    best_count = 0

    for model_id, keyword_sets in precomputed_sets:
        for kw_set in keyword_sets:
            if keyword_set_matches(title_normalized, kw_set, item_brand=item_brand) and len(kw_set) > best_count:
                best_count = len(kw_set)
                best_id = model_id

    return best_id


def match_model(
    title_normalized: str,
    models: list,
    brand_hint: Optional[str] = None,
    item_brand: Optional[str] = None,
) -> Optional[int]:
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
    `item_brand` : marque Vinted confirmée de cette annonce précise (voir
    keyword_set_matches) — comble le mot-clé de marque si absent du titre.

    Pratique pour un match ponctuel (un seul appel). Pour matcher plusieurs
    annonces contre le même jeu de modèles, précalculer une fois avec
    build_model_keyword_sets() et appeler match_model_precomputed() en boucle
    — voir le commentaire de build_model_keyword_sets pour le coût évité.
    """
    return match_model_precomputed(
        title_normalized, build_model_keyword_sets(models, brand_hint), item_brand=item_brand
    )
