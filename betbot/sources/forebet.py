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
