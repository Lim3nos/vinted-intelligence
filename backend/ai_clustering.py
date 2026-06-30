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

    for attempt in range(2):  # 1 essai + 1 retry
        try:
            client = _get_client()
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            raw = _strip_markdown(response.text)

            clusters = json.loads(raw)

            if not isinstance(clusters, list):
                raise ValueError("La réponse n'est pas un tableau JSON")

            # Valider la structure minimale
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
            if attempt == 0:
                logger.warning("Réponse Gemini non-JSON, retry avec prompt de correction: %s", e)
                prompt = (
                    "Ta réponse précédente n'était pas un JSON valide. "
                    "Réponds UNIQUEMENT avec le tableau JSON, sans aucun autre texte.\n\n"
                    + prompt
                )
            else:
                log_to_db(
                    "ERROR", "ai_clustering",
                    f"Parsing JSON Gemini échoué après retry: {e}",
                    {"raw_response": response.text[:500] if 'response' in dir() else ""},
                )
                _record_error()
                return []

        except Exception as e:
            log_to_db(
                "ERROR", "ai_clustering",
                f"Erreur appel Gemini: {e}",
                {"attempt": attempt},
            )
            _record_error()
            return []

    return []


def is_circuit_open() -> bool:
    """Retourne True si le circuit breaker Gemini est actuellement ouvert."""
    if _circuit_open_until is None:
        return False
    return time.monotonic() < _circuit_open_until
