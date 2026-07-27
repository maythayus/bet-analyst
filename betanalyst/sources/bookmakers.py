"""Cotes des bookmakers francais.

Unibet expose une API JSON publique (`/lvs-api/`) qui liste les rencontres a venir,
mais avec le seul marche 1 N 2. Les autres marches (les deux equipes marquent, double
chance, et leurs combinaisons) sont rendus cote serveur dans la page detail de chaque
rencontre : `fetch_event_markets` les y lit.

ParionsSport (FDJ) utilise la meme technologie mais protege son API derriere
DataDome : on passe alors par un fichier CSV rempli a la main (`--odds-csv`), format
`match;1;X;2`.
"""

from __future__ import annotations

import csv
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from betanalyst.config import USER_AGENT, ScrapeConfig

log = logging.getLogger(__name__)

# Endpoint public utilise par le site Unibet France : les rencontres de football (p240)
# par pages de 50, dans l'ordre des coups d'envoi. Les autres tailles de page renvoient
# 404 ; seul `pageIndex` permet d'avancer dans le calendrier.
UNIBET_URL = (
    "https://www.unibet.fr/lvs-api/next/50/p240"
    "?lineId=1&originId=3&breakdownEventsIntoDays=true&showPromotions=true&pageIndex={page}"
)
# Au-dela, on sort largement de la journee en cours meme un jour de grosse affiche.
UNIBET_MAX_PAGES = 12
# L'API refuse les appels sans Referer/Origin du site (reponse 'Missing X-LVS-HSToken').
UNIBET_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.unibet.fr/paris-football",
    "Origin": "https://www.unibet.fr",
    "X-Requested-With": "XMLHttpRequest",
}
UNIBET_SITE = "https://www.unibet.fr"
WIN_DRAW_WIN = "WIN_DRAW_WIN"
SIMILARITY_THRESHOLD = 0.6
DRAW_LABELS = {"match nul", "nul", "draw", "x", "n"}
YES_LABELS = {"oui", "yes"}
NO_LABELS = {"non", "no"}

# Cartes de la page detail dont les cotes correspondent a un marche du modele. Les
# titres sont compares une fois les accents retires ; seul le temps reglementaire
# ("90 mins") est retenu, les mi-temps n'ont pas d'equivalent dans le modele.
DETAIL_MARKET_TITLES = re.compile(
    r"^(1 n 2"
    r"|double chance"
    r"|les 2 equipes marqueront.*"
    r"|resultat et les deux equipes marquent"
    r"|double chance et les 2 equipes marquent)"
    r" - 90 mins$"
)
# Doubles chances : la paire de signes, triee, donne le nom du marche.
DOUBLE_CHANCE = {("1", "N"): "1N", ("1", "2"): "12", ("2", "N"): "N2"}

# Suffixes et prefixes de club sans valeur discriminante. "City" et "United" en sont
# volontairement absents : ils distinguent des clubs d'une meme ville.
_NOISE = re.compile(r"\b(fc|cf|sc|ac|as|ss|us|sv|if|fk|sk|nk|hk|bk|afc|cd|ud|rc|rcd|club)\b")
TOKEN_THRESHOLD = 0.75
PREFIX_LENGTH = 4


def normalise(name: str) -> str:
    """Cle de comparaison insensible aux accents, ponctuations et suffixes de club."""
    text = unicodedata.normalize("NFKD", name.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def _same_token(left: str, right: str) -> bool:
    if left == right:
        return True
    shortest, longest = sorted((left, right), key=len)
    if len(shortest) >= PREFIX_LENGTH and longest.startswith(shortest):
        return True
    return SequenceMatcher(None, left, right).ratio() >= TOKEN_THRESHOLD


def similarity(left: str, right: str) -> float:
    """Score de 0 a 1 entre deux libelles d'equipe, une fois normalises.

    Le score compte la part des mots du libelle le plus court retrouves dans l'autre :
    « Rennes » correspond a « Stade Rennais », mais « Manchester City » ne correspond
    pas a « Manchester United », dont un mot sur deux seulement concorde.
    """
    left_key, right_key = normalise(left), normalise(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return 1.0

    shortest, longest = sorted((left_key.split(), right_key.split()), key=len)
    matched = sum(1 for token in shortest if any(_same_token(token, other) for other in longest))
    return matched / len(shortest)


def teams_match(left: str, right: str) -> bool:
    """Vrai si deux libelles d'equipe designent probablement le meme club."""
    return similarity(left, right) >= SIMILARITY_THRESHOLD


@dataclass
class BookmakerOdds:
    """Cotes proposees par un bookmaker pour une rencontre."""

    bookmaker: str
    home_team: str
    away_team: str
    odds: dict[str, float] = field(default_factory=dict)
    kickoff: str | None = None
    competition: str | None = None
    url: str | None = None  # page detail, seule a exposer les marches combines

    @property
    def complete(self) -> bool:
        return all(self.odds.get(key) for key in ("1", "X", "2"))


def deaccent(text: str) -> str:
    """Minuscules sans accents, pour comparer des libelles francais."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def slugify(text: str) -> str:
    """Reproduit les URL d'Unibet : « D1 Lettonie » -> « d1-lettonie »."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", deaccent(text))).strip("-")


def _sign_for(part: str, home: str, away: str) -> str | None:
    """Traduit un morceau de libelle en signe 1, N ou 2."""
    if part in DRAW_LABELS:
        return "N"
    if teams_match(part, home):
        return "1"
    if teams_match(part, away):
        return "2"
    return None


def market_name(label: str, home: str, away: str) -> str | None:
    """Nom de marche du modele correspondant a un libelle Unibet.

    Les libelles melangent noms d'equipes, signes et suffixe « et Oui/Non » :
    « Nul / 2 et Oui » devient « N2 et oui », « FK RFS / Non » devient « 2 et non ».
    Retourne None pour les libelles sans equivalent dans le modele.
    """
    pieces = re.split(r"\s*/\s*|\s+et\s+", deaccent(label))
    parts = [part.strip() for part in pieces if part.strip()]
    if not parts:
        return None

    btts = None
    if parts[-1] in YES_LABELS | NO_LABELS:
        btts = "oui" if parts[-1] in YES_LABELS else "non"
        parts = parts[:-1]

    signs = [sign for part in parts if (sign := _sign_for(part, home, away))]
    if len(signs) != len(parts):
        return None

    if not signs:
        return f"Les deux marquent : {btts}" if btts else None
    if len(signs) == 1:
        base = signs[0]
    elif len(signs) == 2:
        base = DOUBLE_CHANCE.get(tuple(sorted(signs)))
    else:
        base = None

    if not base:
        return None
    return f"{base} et {btts}" if btts else base


def parse_event_markets(html: str, home: str, away: str) -> dict[str, float]:
    """Lit les marches de la page detail d'une rencontre Unibet.

    Chaque carte porte un titre (« Double chance et les 2 equipes marquent - 90 Mins »)
    et des selections dont le libelle est soit un en-tete de ligne, soit un label dans
    le bouton. Seules les cartes ayant un equivalent dans le modele sont lues.
    """
    soup = BeautifulSoup(html, "html.parser")
    odds: dict[str, float] = {}

    for card in soup.select("div.psel-market-card"):
        title = card.select_one(".psel-title-market__label")
        if not title or not DETAIL_MARKET_TITLES.match(deaccent(title.get_text(strip=True))):
            continue

        for outcome in card.select("psel-outcome"):
            price = _price_text(outcome.select_one(".psel-outcome__data"))
            if price is None:
                continue
            label = outcome.select_one(".psel-outcome__label")
            if label is None:
                row = outcome.find_parent("tr")
                label = row.select_one("th") if row else None
            if label is None:
                continue
            market = market_name(label.get_text(strip=True), home, away)
            if market:
                odds[market] = price
    return odds


def _price_text(node) -> float | None:
    return _price(node.get_text(strip=True)) if node else None


_session: requests.Session | None = None


def _unibet_session(cfg: ScrapeConfig) -> requests.Session:
    """Session ouverte sur la page football du site.

    Appelee a froid, l'API repond « Missing X-LVS-HSToken » ; elle accepte les appels
    une fois la page publique visitee, exactement comme le fait un navigateur.
    """
    global _session
    if _session is not None:
        return _session

    session = requests.Session()
    headers = dict(UNIBET_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml"
    try:
        session.get(f"{UNIBET_SITE}/paris-football", headers=headers, timeout=cfg.request_timeout)
    except requests.RequestException as exc:
        log.debug("Unibet : page d'accueil injoignable (%s)", exc)
    _session = session
    return session


def _unibet_get(url: str, cfg: ScrapeConfig, *, html: bool = False) -> requests.Response | None:
    """GET sur Unibet, avec une seconde tentative apres reouverture de la session."""
    global _session
    headers = dict(UNIBET_HEADERS)
    if html:
        headers["Accept"] = "text/html,application/xhtml+xml"

    for attempt in (1, 2, 3):
        try:
            response = _unibet_session(cfg).get(url, headers=headers, timeout=cfg.request_timeout)
        except requests.RequestException as exc:
            log.warning("Unibet injoignable : %s", exc)
            return None
        if response.status_code == 200:
            return response
        if response.status_code == 401 and attempt < 3:
            _session = None  # session expiree ou refusee : on en rouvre une
            time.sleep(attempt)
            continue
        log.warning("Unibet : HTTP %s sur %s (%s)", response.status_code, url, response.text[:120])
        return None
    return None


def fetch_event_markets(entry: BookmakerOdds, cfg: ScrapeConfig) -> dict[str, float]:
    """Recupere les marches combines d'une rencontre depuis sa page Unibet."""
    if not entry.url:
        return {}
    response = _unibet_get(entry.url, cfg, html=True)
    if response is None:
        return {}
    markets = parse_event_markets(response.text, entry.home_team, entry.away_team)
    log.info("Unibet : %d marches lus pour %s", len(markets), entry.home_team)
    return markets


def fixture_matches(entry: BookmakerOdds, home_team: str, away_team: str) -> bool:
    """Vrai si la ligne du bookmaker correspond a la rencontre, dans le bon sens.

    L'appariement inverse est evalue aussi : dans un derby, « Man City vs Man Utd »
    ressemble fortement a « Man Utd vs Man City », et seul l'ordre le mieux note est
    retenu.
    """
    direct = similarity(entry.home_team, home_team), similarity(entry.away_team, away_team)
    if min(direct) < SIMILARITY_THRESHOLD:
        return False
    reverse = similarity(entry.home_team, away_team), similarity(entry.away_team, home_team)
    return sum(direct) > sum(reverse)


def _price(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _parse_unibet_page(payload: dict) -> list[BookmakerOdds]:
    items = payload.get("items", {})
    events = {key: val for key, val in items.items() if key.startswith("e")}
    markets = {
        key: val
        for key, val in items.items()
        if key.startswith("m") and val.get("style") == WIN_DRAW_WIN
    }
    outcomes: dict[str, list[dict]] = {}
    for key, val in items.items():
        if key.startswith("o"):
            outcomes.setdefault(val.get("parent", ""), []).append(val)

    results: list[BookmakerOdds] = []
    for market_key, market in markets.items():
        event = events.get(market.get("parent", ""))
        if not event:
            continue
        home, away = event.get("a"), event.get("b")
        if not home or not away:
            continue

        odds: dict[str, float] = {}
        ordered = sorted(outcomes.get(market_key, []), key=lambda o: o.get("pos", 0))
        for index, outcome in enumerate(ordered):
            price = _price(outcome.get("price"))
            label = (outcome.get("desc") or "").strip()
            if price is None:
                continue
            if label.lower() in DRAW_LABELS:
                odds["X"] = price
            elif teams_match(label, home):
                odds["1"] = price
            elif teams_match(label, away):
                odds["2"] = price
            elif len(ordered) == 3:  # libelle inattendu : on se rabat sur la position
                odds[("1", "X", "2")[index]] = price

        if odds:
            results.append(
                BookmakerOdds(
                    bookmaker="Unibet",
                    home_team=home,
                    away_team=away,
                    odds=odds,
                    kickoff=parse_kickoff(event.get("start")),
                    competition=event.get("pdesc"),
                    url=_event_url(market.get("parent", ""), event),
                )
            )
    return results


def parse_kickoff(raw: str | None) -> str | None:
    """Convertit l'horaire du listing (`2607271500`) en `2026-07-27 15:00`."""
    if not raw or len(raw) != 10 or not raw.isdigit():
        return raw or None
    try:
        return datetime.strptime(raw, "%y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def _event_url(event_key: str, event: dict) -> str | None:
    """URL de la page detail, deduite du chemin et de l'identifiant de la rencontre.

    Le listing ne fournit pas de lien : Unibet le compose a partir du pays, de la
    competition, de l'identifiant numerique et du nom de la rencontre, par exemple
    `/paris-football/lettonie/d1-lettonie/3360127/fk-tukums-2000-vs-fk-rfs`.
    """
    path = event.get("path") or {}
    country, league, desc = path.get("Category"), path.get("League"), event.get("desc")
    identifier = event_key.lstrip("e")
    if not (country and league and desc and identifier.isdigit()):
        return None
    return (
        f"{UNIBET_SITE}/paris-football/{slugify(country)}/{slugify(league)}"
        f"/{identifier}/{slugify(desc)}"
    )


def kickoff_day(kickoff: str | None) -> str | None:
    """Jour du coup d'envoi (`AAAA-MM-JJ`) d'un horaire deja normalise."""
    return kickoff[:10] if kickoff else None


def fetch_unibet(cfg: ScrapeConfig, *, today_only: bool = False) -> list[BookmakerOdds]:
    """Recupere les rencontres de football cotees chez Unibet France.

    Par defaut, la premiere page suffit (les 50 prochains coups d'envoi). Avec
    `today_only`, les pages sont enchainees tant qu'elles contiennent des rencontres du
    jour, puis les rencontres des jours suivants sont ecartees.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    entries: list[BookmakerOdds] = []
    for page in range(UNIBET_MAX_PAGES if today_only else 1):
        response = _unibet_get(UNIBET_URL.format(page=page), cfg)
        if response is None:
            break
        found = _parse_unibet_page(response.json())
        if not found:
            break
        if today_only:
            kept = [entry for entry in found if kickoff_day(entry.kickoff) == day]
            entries.extend(kept)
            if len(kept) < len(found):  # la page deborde sur les jours suivants
                break
        else:
            entries.extend(found)

    if today_only:
        log.info("Unibet : %d rencontres cotees aujourd'hui", len(entries))
    else:
        log.info("Unibet : %d rencontres cotees", len(entries))
    return entries


def load_csv(path: Path, bookmaker: str = "ParionsSport") -> list[BookmakerOdds]:
    """Lit un fichier de cotes saisi a la main.

    Deux formats acceptes, separateur `;` ou `,` :
    - `match;1;X;2` : les trois cotes du resultat sec ;
    - `match;marche;cote` : n'importe quel marche (`1N et oui`, `12`, ...).
    """
    if not path.is_file():
        raise FileNotFoundError(f"Fichier de cotes introuvable : {path}")

    rows: list[BookmakerOdds] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        for row in csv.reader(handle, delimiter=delimiter):
            if len(row) < 3 or not row[0].strip():
                continue
            label = row[0].strip()
            if label.lower().startswith(("match", "rencontre")):
                continue
            for separator in (" vs ", " - ", "\u2013", "/"):
                if separator in label:
                    home, away = label.split(separator, 1)
                    break
            else:
                log.warning("Ligne ignoree (format 'A vs B' attendu) : %s", label)
                continue
            if len(row) >= 4 and _price(row[2]) is not None:
                odds = {
                    key: value
                    for key, value in zip(
                        ("1", "X", "2"), (_price(cell) for cell in row[1:4]), strict=False
                    )
                    if value
                }
            else:  # format 'match;marche;cote'
                price = _price(row[2])
                if price is None:
                    log.warning("Cote illisible pour %s : %s", label, row[2])
                    continue
                odds = {row[1].strip(): price}
            rows.append(
                BookmakerOdds(
                    bookmaker=bookmaker,
                    home_team=home.strip(),
                    away_team=away.strip(),
                    odds=odds,
                )
            )
    log.info("%s : %d rencontres lues depuis %s", bookmaker, len(rows), path.name)
    return rows


def find(entries: list[BookmakerOdds], home_team: str, away_team: str) -> BookmakerOdds | None:
    """Retrouve la cote d'une rencontre malgre les differences de nommage."""
    for entry in entries:
        if fixture_matches(entry, home_team, away_team):
            return entry
    return None
