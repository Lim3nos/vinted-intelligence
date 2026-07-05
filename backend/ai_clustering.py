"""
Clustering des titres d'annonces par modèle produit via Gemini Flash.
Circuit breaker : suspend les appels Gemini après trop d'erreurs consécutives.
"""

import os
import re
import json
import time
import logging
from typing import Optional

from google import genai
from google.genai import types as genai_types

from logger import log_to_db

logger = logging.getLogger("vinted.ai_clustering")

GEMINI_MAX_TITLES      = 300
GEMINI_MAX_TITLE_LEN   = 100
_CIRCUIT_OPEN_DURATION = 1800  # 30 minutes

# Circuit breaker state
_error_count: int = 0
_error_window_start: float = 0.0
_circuit_open_until: Optional[float] = None

_model = None


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


def _get_client():
    """Initialise et retourne le client Gemini (lazy init)."""
    global _model
    if _model is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY non configurée")
        _model = genai.Client(api_key=api_key)
    return _model


def _check_circuit_breaker() -> bool:
    """
    Vérifie l'état du circuit breaker.
    Retourne True si le circuit est OUVERT (appels suspendus), False sinon.
    """
    global _circuit_open_until, _error_count, _error_window_start

    now = time.monotonic()

    if _circuit_open_until is not None:
        if now < _circuit_open_until:
            logger.warning(
                "Circuit breaker Gemini ouvert — suspension des appels (%.0f s restantes)",
                _circuit_open_until - now,
            )
            return True
        else:
            # Réinitialiser après expiration
            _circuit_open_until = None
            _error_count = 0
            _error_window_start = now
            logger.info("Circuit breaker Gemini réinitialisé")

    return False


def _record_error() -> None:
    """Enregistre une erreur et ouvre le circuit si le seuil est dépassé."""
    global _error_count, _error_window_start, _circuit_open_until

    now = time.monotonic()

    # Fenêtre glissante de 10 minutes
    if now - _error_window_start > 600:
        _error_count = 0
        _error_window_start = now

    _error_count += 1

    if _error_count > 5:
        _circuit_open_until = now + _CIRCUIT_OPEN_DURATION
        log_to_db(
            "ERROR", "ai_clustering",
            f"Circuit breaker Gemini ouvert — {_error_count} erreurs en 10 min",
            {"error_count": _error_count},
        )


def _strip_markdown(text: str) -> str:
    """Supprime les balises ```json et ``` éventuelles de la réponse Gemini."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def cluster_listings_by_model(titles: list[str]) -> list[dict]:
    """
    Regroupe une liste de titres d'annonces en modèles distincts via Gemini Flash.

    Paramètres :
        titles : liste de titres normalisés (max GEMINI_MAX_TITLES)

    Retourne :
        list[dict] avec pour chaque groupe :
        {
            "model_name": str,
            "suggested_keywords": list[str],
            "indices": list[int]
        }
        Retourne [] en cas d'échec — ne jamais crasher.
    """
    if not titles:
        return []

    if _check_circuit_breaker():
        return []

    # Tronquer et numéroter les titres
    titles_trunc = [t[:GEMINI_MAX_TITLE_LEN] for t in titles[:GEMINI_MAX_TITLES]]
    titles_numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(titles_trunc))

    prompt = f"""Tu es un expert en mode et en revente de produits de grandes marques sur Vinted.

Voici une liste de titres d'annonces Vinted numérotés de 0 à N.
Regroupe ces titres par modèle de produit identique ou très similaire.

Règles strictes :
- Ignore les différences de casse, d'orthographe, et d'ordre des mots
- Deux titres décrivent le même modèle si la marque ET le nom du modèle sont identifiables et correspondent
- Regroupe dans un groupe "Autres" tout ce qui n'a pas de modèle précis identifiable
- Pour chaque groupe, propose un nom court et précis au format "Marque NomModele"
- Pour chaque groupe, propose 2 à 4 mots-clés en minuscules permettant d'identifier ce modèle

Réponds UNIQUEMENT avec un tableau JSON valide.
Utilise exclusivement des guillemets doubles.
Aucun texte avant ou après. Aucune balise markdown. Aucun commentaire.

Format attendu :
[{{"model_name": "Lemaire Castanet", "suggested_keywords": ["castanet", "lemaire"], "indices": [0, 3, 7]}}]

Titres à analyser :
{titles_numbered}"""

    for attempt in range(3):  # jusqu'à 3 tentatives
        try:
            client = _get_client()
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            raw = _strip_markdown(response.text)
            clusters = json.loads(raw)

            if not isinstance(clusters, list):
                raise ValueError("La réponse n'est pas un tableau JSON")

            validated = []
            for c in clusters:
                if isinstance(c, dict) and "model_name" in c and "indices" in c:
                    validated.append({
                        "model_name": str(c["model_name"]),
                        "suggested_keywords": [str(k) for k in c.get("suggested_keywords", [])],
                        "indices": [int(i) for i in c["indices"]],
                    })

            log_to_db(
                "INFO", "ai_clustering",
                f"Clustering OK — {len(validated)} groupes pour {len(titles_trunc)} titres",
                {"nb_clusters": len(validated), "nb_titles": len(titles_trunc)},
            )
            return validated

        except json.JSONDecodeError as e:
            if attempt < 2:
                logger.warning("Réponse Gemini non-JSON, retry: %s", e)
                prompt = (
                    "Ta réponse précédente n'était pas un JSON valide. "
                    "Réponds UNIQUEMENT avec le tableau JSON, sans aucun autre texte.\n\n"
                    + prompt
                )
            else:
                log_to_db("ERROR", "ai_clustering",
                          f"Parsing JSON Gemini échoué après {attempt+1} essais: {e}",
                          {"raw_snippet": locals().get("raw", "")[:300]})
                _record_error()
                return []

        except Exception as e:
            err_str = str(e)
            # 503 = surcharge temporaire — ne pas ouvrir le circuit breaker, juste retry
            if "503" in err_str and attempt < 2:
                wait = (attempt + 1) * 8
                logger.warning("Gemini 503 surcharge, retry dans %ds (essai %d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            log_to_db("ERROR", "ai_clustering",
                      f"Erreur appel Gemini: {e}",
                      {"attempt": attempt, "model": GEMINI_MODEL})
            _record_error()
            return []

    return []


def generate_search_variants(model_name: str, keywords: list, search_query: str = "") -> list:
    """
    Génère des variantes de requête de recherche pour un modèle produit via Gemini.

    Exemples : "Lemaire Castanet" → ["lemaire castagnette", "castanet ballerine lemaire", ...]
    Retourne [] si le circuit est ouvert ou en cas d'erreur.
    """
    if not model_name or _check_circuit_breaker():
        return []

    kw_str = ", ".join(keywords) if keywords else "(aucun)"
    context = f" (recherche parente : {search_query})" if search_query else ""

    prompt = f"""Tu es expert en mode et revente de pièces de créateurs sur Vinted et sites similaires.

Modèle produit : "{model_name}"{context}
Mots-clés existants : {kw_str}

Génère des variantes de requêtes de recherche pour trouver des annonces de CE produit exact
qui n'utilisent pas forcément le nom officiel ou la graphie standard.

Tiens compte de :
- Orthographes alternatives et fautes de frappe fréquentes
- Traductions (anglais, espagnol, italien)
- Diminutifs ou abréviations
- Façons alternatives de décrire la pièce (type de vêtement, matière, silhouette)

Réponds UNIQUEMENT avec un tableau JSON de chaînes de recherche. Maximum 6 variantes.
Pas de texte avant ni après. Pas de balises markdown.

Exemple pour "Lemaire Castanet" :
["lemaire castagnette", "lemaire castanet ballerine", "lemaire flat mule castanet"]

Tableau JSON :"""

    try:
        client = _get_client()
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw = _strip_markdown(resp.text)
        variants = json.loads(raw)
        if isinstance(variants, list):
            result = [str(v).strip() for v in variants if v and str(v).strip()][:6]
            log_to_db(
                "INFO", "ai_clustering",
                f"Variantes générées pour '{model_name}': {result}",
                {"model": model_name, "variants": result},
            )
            return result
    except Exception as e:
        log_to_db("WARNING", "ai_clustering", f"Erreur génération variantes '{model_name}': {e}")
    return []


def is_circuit_open() -> bool:
    """Retourne True si le circuit breaker Gemini est actuellement ouvert."""
    if _circuit_open_until is None:
        return False
    return time.monotonic() < _circuit_open_until
