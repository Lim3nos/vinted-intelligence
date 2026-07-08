"""
Nettoyage et validation des mots-clés de matching produit (keywords_rules).

Cause racine du bug des "modèles parasités" : un mot-clé vide ou trop court
matche n'importe quel titre ("" est une sous-chaîne de toute chaîne en
Python), ce qui fait que _match_model() assigne alors TOUTES les annonces
d'une recherche au modèle concerné, quel que soit leur contenu réel.
"""

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
