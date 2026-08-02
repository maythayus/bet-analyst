"""Collecte des predictions mathematiques de Forebet.

Le HTML de Forebet change regulierement : le parsing est volontairement
defensif (plusieurs selecteurs testes, valeurs manquantes tolerees).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from betbot.config import ScrapeConfig
from betbot.models import ForebetPrediction
from betbot.sources.http import FetchError, fetch_html

log = logging.getLogger(__name__)

BASE = "https://www.forebet.com"
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    match = _NUMBER.search(text.replace("\xa0", " "))
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


def _first_text(row: Tag, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = row.select_one(selector)
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)
    return None


def _parse_probabilities(row: Tag) -> tuple[float | None, float | None, float | None]:
    container = row.select_one(".fprc")
    if container is None:
        return None, None, None
    values = [_to_float(span.get_text()) for span in container.select("span")]
    values = [v for v in values if v is not None]
    if len(values) >= 3:
        return values[0], values[1], values[2]
    return None, None, None


def _parse_odds(row: Tag) -> dict[str, float]:
    odds: dict[str, float] = {}
    node = row.select_one(".lscrsp, .odd_shw, .haodd")
    value = _to_float(node.get_text()) if node else None
    if value:
        odds["1"] = value
    return odds


def parse_predictions(html: str, limit: int | None = None) -> list[ForebetPrediction]:
    """Extrait les predictions d'une page de listing Forebet."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("div.rcnt")
    predictions: list[ForebetPrediction] = []

    for row in rows:
        home = _first_text(row, [".homeTeam span", ".homeTeam", ".hom"])
        away = _first_text(row, [".awayTeam span", ".awayTeam", ".awy"])
        if not home or not away:
            continue

        prob_home, prob_draw, prob_away = _parse_probabilities(row)
        link = row.select_one("a[href]")
        href = link["href"] if isinstance(link, Tag) and link.has_attr("href") else None

        predictions.append(
            ForebetPrediction(
                home_team=home,
                away_team=away,
                kickoff=_first_text(row, [".date_bah", ".date", ".stime"]),
                competition=_first_text(row, [".shortTag", ".tnmscn a", ".leag"]),
                prob_home=prob_home,
                prob_draw=prob_draw,
                prob_away=prob_away,
                predicted_score=_first_text(row, [".ex_sc", ".predict_y", ".ex"]),
                avg_goals=_to_float(_first_text(row, [".avg_sc", ".avgsc"])),
                odds=_parse_odds(row),
                url=f"{BASE}{href}" if href and href.startswith("/") else href,
            )
        )
        if limit and len(predictions) >= limit:
            break

    log.info("Forebet : %d predictions extraites", len(predictions))
    return predictions


# La mi-temps n'a pas d'equivalent dans le modele : ces marches restent informatifs.
HALF_TIME_MARKETS = ("1 (1re mi-temps)", "N (1re mi-temps)", "2 (1re mi-temps)")

# Pages Forebet consacrees a un seul marche : le titre de la page les distingue, et
# `forepr` donne le pronostic dont `fpr` est la probabilite. Le marche complementaire
# vaut 100 moins cette probabilite, les deux issues etant exclusives.
_TWO_WAY_PAGES: dict[str, tuple[str, str, str]] = {
    # titre -> (pronostic positif, marche correspondant, marche complementaire)
    "both to score": ("yes", "Les deux marquent : oui", "Les deux marquent : non"),
    "under/over 2.5 goals": ("over", "Plus de 2.5 buts", "Moins de 2.5 buts"),
}
# Doubles chances : Forebet ecrit indifferemment 1X ou X1, le modele n'ecrit que 1N.
_DOUBLE_CHANCE = {"1x": "1N", "x1": "1N", "x2": "N2", "2x": "N2", "12": "12", "21": "12"}


def _market_probabilities(row: Tag, page: str) -> dict[str, float]:
    """Probabilites du marche traite par la page, pour une ligne de rencontre."""
    if page == "1x2":
        # La page 1X2 donne les trois issues du temps reglementaire, comme le listing.
        home, draw, away = _parse_probabilities(row)
        if None in (home, draw, away):
            return {}
        return dict(zip(("1", "N", "2"), (home, draw, away), strict=True))

    pick = _first_text(row, [".forepr"])
    probability = _to_float(_first_text(row, [".fpr"]))
    if not pick or probability is None:
        return {}

    pick = pick.strip().lower()
    if page in _TWO_WAY_PAGES:
        positive, market, opposite = _TWO_WAY_PAGES[page]
        if pick.startswith(positive):
            return {market: probability, opposite: round(100 - probability, 2)}
        return {market: round(100 - probability, 2), opposite: probability}

    if page == "double chance":
        market = _DOUBLE_CHANCE.get(pick)
        return {market: probability} if market else {}

    if page == "half time":
        # La mi-temps est donnee en 1 X 2 : la colonne du pronostic ne suffit pas, les
        # trois probabilites sont lues dans l'ordre.
        home, draw, away = _parse_probabilities(row)
        if None in (home, draw, away):
            return {}
        return dict(zip(HALF_TIME_MARKETS, (home, draw, away), strict=True))

    return {}


def market_page_kind(html: str | bytes) -> str | None:
    """Marche traite par une page Forebet specialisee, d'apres son titre."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    for key in (*_TWO_WAY_PAGES, "double chance", "half time", "1x2"):
        if key in title:
            return key
    return None


def parse_market_page(html: str | bytes) -> tuple[str, list[ForebetPrediction]]:
    """Lit une page Forebet dediee a un marche (both to score, under/over 2.5...).

    Retourne le marche reconnu et une prediction par rencontre, dont seul `markets`
    est renseigne : ces pages n'affichent pas les probabilites 1 X 2 du temps
    reglementaire, sauf la page de mi-temps qui les donne pour la premiere periode.
    """
    page = market_page_kind(html)
    if page is None:
        raise FetchError(
            "Page Forebet non reconnue : attendu une page « 1X2 », « Both to score », "
            "« Under/Over 2.5 goals », « Double chance » ou « Half Time (HT) »."
        )

    soup = BeautifulSoup(html, "html.parser")
    predictions: list[ForebetPrediction] = []
    for row in soup.select("div.rcnt"):
        home = _first_text(row, [".homeTeam span", ".homeTeam", ".hom"])
        away = _first_text(row, [".awayTeam span", ".awayTeam", ".awy"])
        markets = _market_probabilities(row, page) if home and away else {}
        if not markets:
            continue
        prediction = ForebetPrediction(
            home_team=home,
            away_team=away,
            kickoff=_first_text(row, [".date_bah", ".date", ".stime"]),
            competition=_first_text(row, [".shortTag", ".tnmscn a", ".leag"]),
            markets=markets,
        )
        if page == "1x2":
            # Cette page est le listing complet de la journee : elle porte aussi le
            # pronostic Forebet lui-meme, score exact et moyenne de buts compris.
            prediction.prob_home = markets["1"]
            prediction.prob_draw = markets["N"]
            prediction.prob_away = markets["2"]
            prediction.predicted_score = _first_text(row, [".ex_sc", ".predict_y", ".ex"])
            prediction.avg_goals = _to_float(_first_text(row, [".avg_sc", ".avgsc"]))
        predictions.append(prediction)

    log.info("Forebet (%s) : %d rencontres lues", page, len(predictions))
    return page, predictions


def read_market_pages(paths: list[Path]) -> list[ForebetPrediction]:
    """Fusionne plusieurs pages Forebet specialisees, une entree par rencontre."""
    merged: dict[tuple[str, str], ForebetPrediction] = {}
    for path in paths:
        if not path.is_file():
            raise FetchError(
                f"Fichier introuvable : {path}. Verifie le chemin exact "
                "(guillemets obligatoires s'il contient des espaces)."
            )
        # Les pages enregistrees depuis un navigateur ne sont pas toujours en UTF-8 :
        # BeautifulSoup deduit l'encodage du meta charset quand on lui passe les octets.
        _, predictions = parse_market_page(path.read_bytes())
        for prediction in predictions:
            key = (prediction.home_team, prediction.away_team)
            existing = merged.get(key)
            if existing is None:
                merged[key] = prediction
            else:
                existing.markets.update(prediction.markets)
                copy_forecast(prediction, existing)
    return list(merged.values())


def copy_forecast(source: ForebetPrediction, target: ForebetPrediction) -> None:
    """Reporte le pronostic 1 X 2 de Forebet, sans ecraser une valeur deja connue.

    Seule la page 1X2 porte ces champs ; les autres pages n'ont que leur marche.
    """
    target.prob_home = target.prob_home if target.prob_home is not None else source.prob_home
    target.prob_draw = target.prob_draw if target.prob_draw is not None else source.prob_draw
    target.prob_away = target.prob_away if target.prob_away is not None else source.prob_away
    target.predicted_score = target.predicted_score or source.predicted_score
    target.avg_goals = target.avg_goals if target.avg_goals is not None else source.avg_goals


def fetch_predictions(
    cfg: ScrapeConfig, *, use_cache: bool = True, html_file: Path | None = None
) -> list[ForebetPrediction]:
    """Recupere les predictions du jour, ou relit une page Forebet sauvegardee a la main."""
    if html_file:
        if not html_file.is_file():
            raise FetchError(
                f"Fichier introuvable : {html_file}. Verifie le chemin exact "
                "(guillemets obligatoires s'il contient des espaces)."
            )
        log.info("Lecture de %s", html_file)
        html = html_file.read_text(encoding="utf-8", errors="replace")
    else:
        html = fetch_html(cfg.forebet_url, cfg, use_cache=use_cache, wait_selector="div.rcnt")
    return parse_predictions(html, limit=cfg.max_matches)
