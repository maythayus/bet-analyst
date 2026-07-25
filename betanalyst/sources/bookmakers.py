"""Cotes 1X2 des bookmakers francais.

Unibet expose une API JSON publique (`/lvs-api/`) qui liste les rencontres a venir
avec leurs marches. ParionsSport (FDJ) utilise la meme technologie mais protege son
API derriere DataDome : on passe alors par un fichier CSV rempli a la main
(`--odds-csv`), format `match;1;X;2`.
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import requests

from betanalyst.config import USER_AGENT, ScrapeConfig

log = logging.getLogger(__name__)

# Endpoint public utilise par le site Unibet France : les 50 prochaines rencontres de
# football (p240). Les autres tailles de page renvoient 404, il n'y a pas de pagination.
UNIBET_URL = (
    "https://www.unibet.fr/lvs-api/next/50/p240"
    "?lineId=1&originId=3&breakdownEventsIntoDays=true&showPromotions=true&pageIndex=0"
)
# L'API refuse les appels sans Referer/Origin du site (reponse 'Missing X-LVS-HSToken').
UNIBET_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.unibet.fr/paris-football",
    "Origin": "https://www.unibet.fr",
    "X-Requested-With": "XMLHttpRequest",
}
WIN_DRAW_WIN = "WIN_DRAW_WIN"
SIMILARITY_THRESHOLD = 0.6
DRAW_LABELS = {"match nul", "nul", "draw", "x", "n"}

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
    """Cotes 1X2 proposees par un bookmaker pour une rencontre."""

    bookmaker: str
    home_team: str
    away_team: str
    odds: dict[str, float] = field(default_factory=dict)
    kickoff: str | None = None
    competition: str | None = None

    @property
    def complete(self) -> bool:
        return all(self.odds.get(key) for key in ("1", "X", "2"))


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
                    kickoff=event.get("start"),
                    competition=event.get("pdesc"),
                )
            )
    return results


def fetch_unibet(cfg: ScrapeConfig) -> list[BookmakerOdds]:
    """Recupere les prochaines rencontres de football cotees chez Unibet France."""
    response = requests.get(UNIBET_URL, headers=UNIBET_HEADERS, timeout=cfg.request_timeout)
    if response.status_code != 200:
        log.warning(
            "Unibet : HTTP %s, cotes indisponibles (%s)",
            response.status_code,
            response.text[:120],
        )
        return []
    entries = _parse_unibet_page(response.json())
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
