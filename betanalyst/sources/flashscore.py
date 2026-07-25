"""Collecte des statistiques brutes sur Flashscore.

Deux etapes :
1. `s.flashscore.com/search` (JSON leger) pour retrouver l'identifiant de chaque equipe ;
2. la page "resultats" de l'equipe, rendue par Playwright, pour lire les derniers matchs.

La forme recente vient des 5 derniers resultats, les confrontations directes sont
deduites en croisant les resultats des deux equipes.

Si Playwright n'est pas installe ou si la page est inaccessible, on leve
`FlashscoreUnavailable` et le pipeline continue avec les seules donnees Forebet.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from betanalyst.config import USER_AGENT, ScrapeConfig
from betanalyst.models import MatchStats, TeamForm

log = logging.getLogger(__name__)

SEARCH_URL = "https://s.flashscore.com/search/"
TEAM_URL = "https://www.flashscore.com/team/{slug}/{team_id}/results/"
FOOTBALL_SPORT_ID = 1
_JSONP = re.compile(r"^[^(]*\((.*)\)[^)]*$", re.DOTALL)


class FlashscoreUnavailable(RuntimeError):
    """Playwright absent, equipe introuvable ou page inaccessible."""


@dataclass(frozen=True)
class Team:
    identifier: str
    slug: str
    title: str


@dataclass(frozen=True)
class PastMatch:
    date: str
    home: str
    away: str
    home_goals: int
    away_goals: int

    def summary(self) -> str:
        return f"{self.date} {self.home} {self.home_goals}-{self.away_goals} {self.away}"


def _search(query: str, cfg: ScrapeConfig) -> list[dict[str, Any]]:
    response = requests.get(
        SEARCH_URL,
        params={"q": query, "l": "1", "s": "1", "f": "1;1", "pid": "2", "sid": "1"},
        headers={"User-Agent": USER_AGENT, "Referer": "https://www.flashscore.com/"},
        timeout=cfg.request_timeout,
    )
    response.raise_for_status()
    body = response.text.strip()
    match = _JSONP.match(body)
    payload = json.loads(match.group(1) if match else body)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload.get("results", []) if isinstance(payload, dict) else []


def find_team(name: str, cfg: ScrapeConfig) -> Team | None:
    """Retrouve l'equipe de football correspondant le mieux au nom fourni."""
    try:
        results = _search(name, cfg)
    except (requests.RequestException, ValueError) as exc:
        log.warning("Recherche Flashscore impossible pour '%s' : %s", name, exc)
        return None

    for item in results:
        if item.get("type") != "participants" or item.get("sport_id") != FOOTBALL_SPORT_ID:
            continue
        identifier, slug = item.get("id"), item.get("url")
        if identifier and slug:
            return Team(str(identifier), str(slug), str(item.get("title", name)))
    return None


def _parse_results_page(page: Any, limit: int) -> list[PastMatch]:
    matches: list[PastMatch] = []
    for row in page.query_selector_all("div.event__match"):
        home = row.query_selector(".event__participant--home, .event__homeParticipant")
        away = row.query_selector(".event__participant--away, .event__awayParticipant")
        home_score = row.query_selector(".event__score--home")
        away_score = row.query_selector(".event__score--away")
        date = row.query_selector(".event__stageTime, .event__time")
        if not (home and away and home_score and away_score):
            continue

        home_text, away_text = home_score.inner_text().strip(), away_score.inner_text().strip()
        if not (home_text.isdigit() and away_text.isdigit()):
            continue

        matches.append(
            PastMatch(
                date=date.inner_text().strip() if date else "?",
                home=home.inner_text().strip(),
                away=away.inner_text().strip(),
                home_goals=int(home_text),
                away_goals=int(away_text),
            )
        )
        if len(matches) >= limit:
            break
    return matches


def fetch_team_results(team: Team, cfg: ScrapeConfig, *, limit: int = 10) -> list[PastMatch]:
    """Ouvre la page 'resultats' d'une equipe et lit ses derniers matchs joues."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depend de l'installation
        raise FlashscoreUnavailable(
            "Playwright n'est pas installe. Lance : "
            "pip install playwright && playwright install chromium"
        ) from exc

    url = TEAM_URL.format(slug=team.slug, team_id=team.identifier)
    log.info("Flashscore : %s", url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=cfg.flashscore_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page(user_agent=USER_AGENT, locale="fr-FR")
        try:
            page.goto(url, timeout=cfg.flashscore_timeout_ms, wait_until="domcontentloaded")
            try:
                page.wait_for_selector("div.event__match", timeout=cfg.flashscore_timeout_ms)
            except PlaywrightTimeout as exc:
                raise FlashscoreUnavailable(f"Aucun match affiche sur {url}") from exc
            return _parse_results_page(page, limit)
        finally:
            browser.close()


def build_form(team_name: str, matches: list[PastMatch], *, sample: int = 5) -> TeamForm:
    """Convertit une liste de matchs en forme recente (du point de vue de l'equipe)."""
    form = TeamForm(name=team_name)
    needle = team_name.lower()
    for match in matches[:sample]:
        is_home = needle in match.home.lower()
        scored = match.home_goals if is_home else match.away_goals
        conceded = match.away_goals if is_home else match.home_goals
        form.matches_played += 1
        form.goals_for += scored
        form.goals_against += conceded
        form.last_results.append("W" if scored > conceded else "D" if scored == conceded else "L")
    return form


def head_to_head(matches: list[PastMatch], opponent: str, *, limit: int = 5) -> list[str]:
    needle = opponent.lower()
    return [
        match.summary()
        for match in matches
        if needle in match.home.lower() or needle in match.away.lower()
    ][:limit]


def fetch_match_stats(home_team: str, away_team: str, cfg: ScrapeConfig) -> MatchStats:
    """Assemble forme des deux equipes + confrontations directes."""
    home = find_team(home_team, cfg)
    away = find_team(away_team, cfg)
    if not home or not away:
        raise FlashscoreUnavailable(
            f"Equipe introuvable sur Flashscore : {home_team} / {away_team}"
        )

    home_matches = fetch_team_results(home, cfg)
    away_matches = fetch_team_results(away, cfg)

    return MatchStats(
        home_team=home_team,
        away_team=away_team,
        home_form=build_form(home.title.split(" (")[0], home_matches),
        away_form=build_form(away.title.split(" (")[0], away_matches),
        head_to_head=head_to_head(home_matches, away.title.split(" (")[0]),
        url=TEAM_URL.format(slug=home.slug, team_id=home.identifier),
    )
