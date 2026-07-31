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

from betbot.config import USER_AGENT, ScrapeConfig
from betbot.models import MatchStats, TeamForm
from betbot.sources.bookmakers import normalise, similarity

log = logging.getLogger(__name__)

SEARCH_URL = "https://s.flashscore.com/search/"
TEAM_URL = "https://www.flashscore.com/team/{slug}/{team_id}/results/"
FOOTBALL_SPORT_ID = 1
# La recherche renvoie aussi des joueurs (type 2), dont le libelle ressemble a un club :
# « Rousseau Thomas (Le Havre) » ne doit pas etre pris pour « Dunav Rousse ».
TEAM_PARTICIPANT = 1
_JSONP = re.compile(r"^[^(]*\((.*)\)[^)]*$", re.DOTALL)
# Le pays est accole au nom de l'equipe : « Everton (Chile) ».
_COUNTRY = re.compile(r"\s*\([^)]*\)\s*$")
# Equipes feminines, reserves et categories de jeunes : meme nom, autre effectif.
_OTHER_SQUAD = re.compile(r"\b(w|b|ii|u\d{2})\b")
_GLUED = re.compile(r"(?<=[a-z])(?=[A-Z])")
# En dessous, le nom trouve n'a plus grand-chose a voir avec celui cherche.
NAME_THRESHOLD = 0.6
OTHER_SQUAD_PENALTY = 0.3
WORD_COUNT_PENALTY = 0.05
COUNTRY_BONUS = 0.25
# Nombre d'homonymes gardes en reserve quand la page du premier n'affiche aucun match.
CANDIDATE_LIMIT = 3
# Longueur maximale d'un mot considere comme une abreviation (« Dyn. » pour Dynamo).
ABBREVIATION_WORD = 4

# Clubs que les grilles francaises nomment autrement que Flashscore : ni troncature ni
# accent ne permet de les rapprocher, seule une table les relie.
_ALIASES = {
    normalise(french): international
    for french, international in {
        "La Gantoise": "Gent",
        "The New Saints": "TNS",
        "Etoile Rouge Belgrade": "Crvena zvezda",
        "FC Copenhague": "Copenhagen",
        "Bale": "Basel",
        "Cologne": "Koln",
        "Naples": "Napoli",
        "La Corogne": "Deportivo La Coruna",
        "Seville FC": "Sevilla",
        "Milan AC": "AC Milan",
        "Neftci PFK": "Neftci Baku",
        # Maxline a quitte Rogachev pour Vitebsk : Flashscore a suivre, pas le bookmaker.
        "Max.Rogachev": "ML Vitebsk",
        "FK DAC 1904": "DAC Dunajska Streda",
    }.items()
}

# Transcriptions concurrentes d'un meme nom slave, d'un bookmaker a l'autre, et sigles
# de villes qu'aucune regle ne deplie : « NY » ne ressemble pas a « New York », mais la
# recherche Flashscore ne trouve rien sans l'ecrire en toutes lettres.
_SPELLINGS = {
    "dynamo": "dinamo",
    "dinamo": "dynamo",
    "kiev": "kyiv",
    "kyiv": "kiev",
    "salonique": "thessaloniki",
    "saint": "st",
    "ny": "new york",
    "nyc": "new york city",
}

# Flashscore nomme les pays en anglais, les bookmakers francais en francais. La table ne
# couvre que les pays dont les noms different assez pour ne pas se ressembler tels quels
# (« Portugal » ou « Chile » se reconnaissent sans traduction).
_COUNTRY_NAMES = {
    "allemagne": "germany",
    "angleterre": "england",
    "argentine": "argentina",
    "autriche": "austria",
    "belgique": "belgium",
    "bresil": "brazil",
    "bulgarie": "bulgaria",
    "chili": "chile",
    "chypre": "cyprus",
    "colombie": "colombia",
    "croatie": "croatia",
    "danemark": "denmark",
    "ecosse": "scotland",
    "equateur": "ecuador",
    "espagne": "spain",
    "estonie": "estonia",
    "etats unis": "usa",
    "finlande": "finland",
    "grece": "greece",
    "hongrie": "hungary",
    "irlande": "ireland",
    "islande": "iceland",
    "israel": "israel",
    "italie": "italy",
    "japon": "japan",
    "lettonie": "latvia",
    "lituanie": "lithuania",
    "moldavie": "moldova",
    "norvege": "norway",
    "pays bas": "netherlands",
    "pologne": "poland",
    "republique tcheque": "czech republic",
    "roumanie": "romania",
    "russie": "russia",
    "serbie": "serbia",
    "slovaquie": "slovakia",
    "slovenie": "slovenia",
    "suede": "sweden",
    "suisse": "switzerland",
    "tcheque": "czech republic",
    "turquie": "turkiye",
    "ukraine": "ukraine",
}


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


def _spaced(name: str) -> str:
    """Nom aere : points, tirets et mots colles separes (« Mac.Tel » -> « Mac Tel »)."""
    return " ".join(_GLUED.sub(" ", name.replace(".", " ").replace("-", " ")).split())


def alias(name: str) -> str | None:
    """Nom international d'un club que le bookmaker francise ou reduit a un sigle."""
    return _ALIASES.get(normalise(name))


def _respelled(name: str) -> str:
    """Nom avec l'autre transcription usuelle (« Dynamo Kiev » -> « Dinamo Kyiv »)."""
    words = [_SPELLINGS.get(word.lower(), word) for word in _spaced(name).split()]
    return " ".join(words)


def query_variants(name: str) -> list[str]:
    """Formes successives a essayer dans la recherche Flashscore.

    Les bookmakers tronquent et collent les noms (« Mac.Tel Aviv », « SherifTiraspol »),
    les francisent (« La Gantoise ») ou les transcrivent autrement (« Dynamo Kiev ») :
    on essaie l'alias connu, le nom brut, une version aeree, l'autre transcription, puis
    le mot le plus long, presque toujours le nom de la ville ou du club.
    """
    spaced = _spaced(name)
    variants = [alias(name) or "", name, spaced, _respelled(name)]
    words = [word for word in re.split(r"\W+", spaced) if len(word) > 3]
    if words:
        variants.append(max(words, key=len))
    return list(dict.fromkeys(variant for variant in variants if variant))


def country_hint(competition: str | None) -> str | None:
    """Nom de competition ramene a l'anglais, pour y reconnaitre un pays.

    « D1 Bresil » devient « d1 brazil », ce qui permet de distinguer deux clubs
    homonymes. Les pays dont le nom est identique dans les deux langues (Paraguay,
    Portugal) passent tels quels ; les competitions continentales ne designent aucun
    pays et ne departagent donc rien.
    """
    if not competition:
        return None
    text = normalise(competition)
    for french, english in _COUNTRY_NAMES.items():
        text = text.replace(french, english)
    return text


def _expanded(wanted: str, plain: str) -> tuple[str, str]:
    """Deux libelles ou chaque abreviation est remplacee par le mot complet d'en face.

    « Dyn. Kyiv » devient « Dynamo Kyiv » face a « Dynamo Kiev » : sans cela, un mot sur
    deux concorde et le bon club passe sous le seuil. L'abreviation peut etre un debut
    de mot (« Dyn. »), un mot contracte (« Utd » pour United) ou les initiales de
    plusieurs mots (« SL » pour Songshan Longmen). Une abreviation qui pourrait designer
    deux choses est laissee telle quelle plutot que devinee.
    """

    def grow(short: str, reference: str) -> str:
        words = reference.split()
        grown = []
        for word in short.split():
            expansions = _expansions(word, words) if len(word) <= ABBREVIATION_WORD else []
            grown.append(expansions[0] if len(expansions) == 1 else word)
        return " ".join(grown)

    return grow(wanted, plain), grow(plain, wanted)


def _expansions(short: str, words: list[str]) -> list[str]:
    """Formes completes possibles d'une abreviation parmi les mots d'un autre nom."""
    single = [word for word in words if len(word) > len(short) and _abbreviates(short, word)]
    return single + _initial_runs(short, words)


def _abbreviates(short: str, word: str) -> bool:
    """« Dyn » et « Utd » abregent « Dynamo » et « United » : memes lettres, dans l'ordre."""
    letters, target = normalise(short), normalise(word)
    if not letters or not target.startswith(letters[0]):
        return False
    position = 0
    for letter in letters:
        position = target.find(letter, position) + 1
        if position == 0:
            return False
    return True


def _initial_runs(short: str, words: list[str]) -> list[str]:
    """Suites de mots dont les initiales forment le sigle (« SL » : Songshan Longmen)."""
    letters = normalise(short)
    if len(letters) < 2:
        return []
    initials = [normalise(word)[:1] for word in words]
    return [
        " ".join(words[start : start + len(letters)])
        for start in range(len(words) - len(letters) + 1)
        if "".join(initials[start : start + len(letters)]) == letters
    ]


def _starts(word: str, prefix: str) -> bool:
    return normalise(word).startswith(normalise(prefix)) and bool(normalise(prefix))


def score_candidate(name: str, title: str, country: str | None = None) -> float:
    """Ressemblance entre le nom cherche et un resultat de recherche.

    Le pays entre parentheses est retire avant comparaison, et une equipe feminine,
    reserve ou de jeunes est penalisee : elle porte le nom du club sans en etre l'equipe.
    Quand le pays de la competition est connu, un candidat du bon pays est privilegie :
    c'est ce qui separe le Libertad d'Equateur de ses homonymes.
    """
    plain = _COUNTRY.sub("", title)
    found = _COUNTRY.search(title)
    origin = normalise(found.group(0)) if found else ""
    bonus = COUNTRY_BONUS if origin and country and origin in country else 0.0
    wanted = _spaced(alias(name) or name)
    score = max(
        similarity(*_expanded(wanted, plain)),
        similarity(*_expanded(_respelled(wanted), plain)),
    )
    if _OTHER_SQUAD.search(plain.lower()) and not _OTHER_SQUAD.search(wanted.lower()):
        score -= OTHER_SQUAD_PENALTY
    # A ressemblance egale, le nom comptant le meme nombre de mots l'emporte :
    # « Sheriff Tiraspol » plutot que « FC Tiraspol » pour « SherifTiraspol ».
    extra = abs(len(normalise(wanted).split()) - len(normalise(plain).split()))
    return score + bonus - WORD_COUNT_PENALTY * extra


def find_candidates(name: str, cfg: ScrapeConfig, *, country: str | None = None) -> list[Team]:
    """Equipes de football plausibles pour un nom, de la plus proche a la plus lointaine.

    Le premier resultat renvoye par Flashscore n'est pas toujours le bon : une recherche
    sur « Libertad Loja » propose d'abord le Libertad d'Asuncion. Les candidats sont donc
    notes, et plusieurs sont conserves : une page d'equipe homonyme peut n'afficher aucun
    match, auquel cas le suivant sert de repli.
    """
    scored: dict[str, tuple[float, Team]] = {}
    for query in query_variants(name):
        try:
            results = _search(query, cfg)
        except (requests.RequestException, ValueError) as exc:
            log.warning("Recherche Flashscore impossible pour '%s' : %s", query, exc)
            continue

        for item in results:
            if item.get("type") != "participants" or item.get("sport_id") != FOOTBALL_SPORT_ID:
                continue
            if item.get("participant_type_id") != TEAM_PARTICIPANT:
                continue
            identifier, slug = item.get("id"), item.get("url")
            if not (identifier and slug):
                continue
            title = str(item.get("title", name))
            score = score_candidate(name, title, country)
            scored[str(identifier)] = (score, Team(str(identifier), str(slug), title))

        if any(score >= NAME_THRESHOLD for score, _ in scored.values()):
            break

    ranked = sorted(scored.values(), key=lambda entry: entry[0], reverse=True)
    kept = [team for score, team in ranked if score >= NAME_THRESHOLD]
    if not kept:
        found = f" (meilleur candidat : {ranked[0][1].title})" if ranked else ""
        log.warning("Flashscore : '%s' introuvable%s", name, found)
        return []

    if ranked[0][0] < 1.0:
        log.info("Flashscore : '%s' identifie comme %s", name, kept[0].title)
    return kept[:CANDIDATE_LIMIT]


def find_team(name: str, cfg: ScrapeConfig, *, country: str | None = None) -> Team | None:
    """Meilleure equipe correspondant au nom fourni, ou None si aucune ne convient."""
    candidates = find_candidates(name, cfg, country=country)
    return candidates[0] if candidates else None


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
        from playwright.sync_api import Error as PlaywrightError
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
        try:
            browser = playwright.chromium.launch(
                headless=cfg.flashscore_headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except PlaywrightError as exc:  # pragma: no cover - depend de l'installation
            raise FlashscoreUnavailable(
                "Chromium n'est pas installe. Lance une fois : "
                "Bet.Bot.exe --install-chromium (ou playwright install chromium)"
            ) from exc
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


def team_with_results(
    candidates: list[Team], cfg: ScrapeConfig
) -> tuple[Team, list[PastMatch]]:
    """Premier homonyme dont la page affiche vraiment des matchs joues.

    « Vitoria BA » trouve d'abord un club amateur dont la page est vide : sans repli, le
    match serait analyse sans statistiques alors que le bon club existe.
    """
    last: FlashscoreUnavailable | None = None
    for team in candidates:
        try:
            matches = fetch_team_results(team, cfg)
        except FlashscoreUnavailable as exc:
            log.info("Flashscore : %s sans resultats affiches, essai du suivant", team.title)
            last = exc
            continue
        if matches:
            return team, matches
        last = FlashscoreUnavailable(f"Aucun match joue pour {team.title}")
    raise last or FlashscoreUnavailable("Aucun candidat exploitable sur Flashscore")


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


def fetch_match_stats(
    home_team: str, away_team: str, cfg: ScrapeConfig, *, competition: str | None = None
) -> MatchStats:
    """Assemble forme des deux equipes + confrontations directes."""
    country = country_hint(competition)
    home_choices = find_candidates(home_team, cfg, country=country)
    away_choices = find_candidates(away_team, cfg, country=country)
    missing = [
        name
        for name, choices in ((home_team, home_choices), (away_team, away_choices))
        if not choices
    ]
    if missing:
        raise FlashscoreUnavailable(
            f"Equipe introuvable sur Flashscore : {', '.join(missing)}. "
            "Le nom vient du bookmaker et peut etre tronque ; utilise --match "
            f'"{home_team} vs {away_team}" avec les noms complets pour forcer la recherche.'
        )

    home, home_matches = team_with_results(home_choices, cfg)
    away, away_matches = team_with_results(away_choices, cfg)

    return MatchStats(
        home_team=home_team,
        away_team=away_team,
        home_form=build_form(home.title.split(" (")[0], home_matches),
        away_form=build_form(away.title.split(" (")[0], away_matches),
        head_to_head=head_to_head(home_matches, away.title.split(" (")[0]),
        url=TEAM_URL.format(slug=home.slug, team_id=home.identifier),
    )
