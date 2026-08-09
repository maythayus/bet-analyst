"""Suivi des pronostics et de leurs resultats reels.

C'est la piece qui manquait le plus a Bet.Bot : les poids de `betbot.consensus` (Forebet
a 60 %, le modele a 40 %) et ses tolerances sont des suppositions, et rien ne permettait
de les verifier. Ce module enregistre chaque pronostic dans un fichier, puis le confronte
au score reel pour mesurer qui a raison, marche par marche.

Deux mesures sont produites :

- l'ecart entre **annonce et realise** : une source qui annonce 70 % sur cent selections
  doit en gagner environ soixante-dix ; en gagner quarante-cinq n'est pas de la malchance,
  c'est un modele mal calibre ;
- le **score de Brier**, moyenne des carres d'erreur, qui recompense a la fois la justesse
  et la franchise : annoncer 50 % partout ne rapporte rien.

Aucun de ces chiffres ne dit ce qui va se passer : ils disent ce que valent les
estimations passees. Sur moins d'une centaine de pronostics regles, ils ne veulent
d'ailleurs pas grand-chose, et le rapport le signale.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from betbot import consensus
from betbot.combo import FOREBET_MARKETS
from betbot.models import MatchBundle
from betbot.names import teams_match

log = logging.getLogger(__name__)

# Un pronostic par ligne, en JSON : le fichier reste lisible et se complete sans etre
# reecrit, meme si une analyse est interrompue.
HISTORY_FILE = "suivi.jsonl"
# En dessous de ce nombre de pronostics regles, une difference entre deux sources n'est
# pas distinguable du hasard : le bilan l'affiche mais previent.
MEANINGFUL_SAMPLE = 100


class PlayedResult(Protocol):
    """Un match joue, tel que `betbot.sources.flashscore.PastMatch` le decrit."""

    date: str
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class Prediction:
    """Un pronostic sur un marche, et le resultat reel quand il est connu."""

    date: str
    match: str
    home_team: str
    away_team: str
    market: str
    # Les trois estimations, conservees separement : c'est tout l'interet du suivi.
    forebet: float | None = None
    model: float | None = None
    implied: float | None = None
    kept: float | None = None
    source: str | None = None
    odds: float | None = None
    kickoff: str | None = None
    # None tant que le score n'est pas connu, puis True si l'issue s'est produite.
    won: bool | None = None
    score: str | None = None

    @property
    def settled(self) -> bool:
        return self.won is not None


def _over_under(market: str, total: int) -> bool | None:
    """Issue d'un marche de buts : « Plus de 2.5 buts », « Moins de 1.5 buts »."""
    for prefix, wins in (("Plus de ", True), ("Moins de ", False)):
        if market.startswith(prefix) and market.endswith(" buts"):
            line = market[len(prefix) : -len(" buts")]
            try:
                threshold = float(line)
            except ValueError:
                return None
            return (total > threshold) is wins
    return None


def outcome(market: str, home_goals: int, away_goals: int) -> bool | None:
    """Le marche s'est-il realise sur ce score ? None quand le marche n'est pas evaluable.

    Les marches de mi-temps et les marches combines ne sont pas juges ici : le score final
    ne suffit pas a les trancher, et deviner serait pire que de ne rien dire.
    """
    both_scored = home_goals > 0 and away_goals > 0
    total = home_goals + away_goals
    simple = {
        "Les deux marquent : oui": both_scored,
        "Les deux marquent : non": not both_scored,
        "1": home_goals > away_goals,
        "N": home_goals == away_goals,
        "2": away_goals > home_goals,
        "1N": home_goals >= away_goals,
        "N2": away_goals >= home_goals,
        "12": home_goals != away_goals,
    }
    if market in simple:
        return simple[market]
    return _over_under(market, total)


def predictions_from(bundle: MatchBundle, *, day: str) -> list[Prediction]:
    """Pronostics a enregistrer pour une rencontre, un par marche que Forebet publie."""
    detail = consensus.summary(bundle)
    prices = bundle.market_prices()
    found: list[Prediction] = []
    for market in FOREBET_MARKETS:
        line = detail.get(market)
        if line is None:
            continue
        values = {
            key: line.get(key) if isinstance(line.get(key), float) else None
            for key in ("forebet", "modele", "cote_implicite", "retenu")
        }
        source = line.get("source")
        found.append(
            Prediction(
                date=day,
                match=bundle.label,
                home_team=bundle.stats.home_team,
                away_team=bundle.stats.away_team,
                market=market,
                forebet=values["forebet"],
                model=values["modele"],
                implied=values["cote_implicite"],
                kept=values["retenu"],
                source=source if isinstance(source, str) else None,
                odds=prices.get(market),
                kickoff=bundle.stats.kickoff,
            )
        )
    return found


def record(bundles: list[MatchBundle], output_dir: Path, *, day: str) -> Path:
    """Ajoute les pronostics du jour au fichier de suivi et renvoie son chemin."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / HISTORY_FILE
    lines = [
        json.dumps(asdict(prediction), ensure_ascii=False)
        for bundle in bundles
        for prediction in predictions_from(bundle, day=day)
    ]
    if lines:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return path


def load(path: Path) -> list[Prediction]:
    """Relit le fichier de suivi, en ignorant les lignes illisibles."""
    if not path.exists():
        return []
    found: list[Prediction] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Ligne de suivi illisible, ignoree")
            continue
        fields = {key: data.get(key) for key in Prediction.__dataclass_fields__}
        found.append(Prediction(**fields))
    return found


def settle(prediction: Prediction, home_goals: int, away_goals: int) -> Prediction:
    """Renseigne le resultat d'un pronostic. Inchange si le marche n'est pas evaluable."""
    won = outcome(prediction.market, home_goals, away_goals)
    if won is None:
        return prediction
    return replace(prediction, won=won, score=f"{home_goals}-{away_goals}")


def save(predictions: list[Prediction], path: Path) -> None:
    """Reecrit le fichier de suivi, resultats compris."""
    path.write_text(
        "\n".join(json.dumps(asdict(item), ensure_ascii=False) for item in predictions) + "\n",
        encoding="utf-8",
    )


def settle_all(
    predictions: list[Prediction], results_for: Callable[[str], list[PlayedResult]]
) -> tuple[list[Prediction], int]:
    """Renseigne tous les pronostics dont le score est retrouve. Renvoie le nombre regle.

    `results_for` recoit un nom d'equipe et rend ses matchs joues : le detail de la source
    (Flashscore, ou un fichier en test) reste dehors, ce module ne fait que rapprocher les
    noms et juger les marches.
    """
    cache: dict[str, list[PlayedResult]] = {}
    updated: list[Prediction] = []
    settled = 0
    for prediction in predictions:
        if prediction.settled:
            updated.append(prediction)
            continue
        if prediction.home_team not in cache:
            cache[prediction.home_team] = results_for(prediction.home_team)
        played = _played_match(prediction, cache[prediction.home_team])
        if played is None:
            updated.append(prediction)
            continue
        judged = settle(prediction, played.home_goals, played.away_goals)
        if judged.settled:
            settled += 1
        updated.append(judged)
    return updated, settled


def _played_match(prediction: Prediction, results: list[PlayedResult]) -> PlayedResult | None:
    """Retrouve la rencontre parmi les resultats d'une equipe, par les deux noms."""
    for played in results:
        if teams_match(played.home, prediction.home_team) and teams_match(
            played.away, prediction.away_team
        ):
            return played
    return None


@dataclass(frozen=True)
class Calibration:
    """Ce que vaut une source sur les pronostics deja regles."""

    source: str
    sample: int
    announced: float  # probabilite moyenne annoncee, en %
    realised: float  # part de selections gagnees, en %
    brier: float  # 0 = parfait, 0.25 = autant dire « une chance sur deux »

    @property
    def bias(self) -> float:
        """Ecart annonce - realise, en points : positif quand la source se surestime."""
        return round(self.announced - self.realised, 2)

    @property
    def meaningful(self) -> bool:
        return self.sample >= MEANINGFUL_SAMPLE


def _calibration(source: str, pairs: list[tuple[float, bool]]) -> Calibration | None:
    if not pairs:
        return None
    announced = sum(probability for probability, _ in pairs) / len(pairs)
    realised = 100 * sum(1 for _, won in pairs if won) / len(pairs)
    brier = sum((probability / 100 - float(won)) ** 2 for probability, won in pairs) / len(pairs)
    return Calibration(
        source=source,
        sample=len(pairs),
        announced=round(announced, 2),
        realised=round(realised, 2),
        brier=round(brier, 4),
    )


def calibrations(predictions: list[Prediction]) -> list[Calibration]:
    """Compare Forebet, le modele, leur consensus et la cote sur les pronostics regles."""
    settled = [item for item in predictions if item.settled and item.won is not None]
    sources: dict[str, list[tuple[float, bool]]] = {
        "Forebet": [(item.forebet, bool(item.won)) for item in settled if item.forebet is not None],
        "modele": [(item.model, bool(item.won)) for item in settled if item.model is not None],
        "retenu": [(item.kept, bool(item.won)) for item in settled if item.kept is not None],
        "cote": [(item.implied, bool(item.won)) for item in settled if item.implied is not None],
    }
    found = [_calibration(name, pairs) for name, pairs in sources.items()]
    return [item for item in found if item is not None]


def markdown(predictions: list[Prediction]) -> str:
    """Bilan lisible : ce que chaque source a annonce, et ce qui s'est produit."""
    rows = calibrations(predictions)
    settled = sum(1 for item in predictions if item.settled)
    if not rows:
        return (
            f"Aucun pronostic regle sur {len(predictions)} enregistres : lance `--bilan` "
            "apres que les matchs se sont joues."
        )
    lines = [
        f"# Bilan de {settled} pronostics regles (sur {len(predictions)} enregistres)",
        "",
        "| Source | Pronostics | Annonce | Realise | Ecart | Brier |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in rows:
        lines.append(
            f"| {item.source} | {item.sample} | {item.announced:.1f} % | "
            f"{item.realised:.1f} % | {item.bias:+.1f} pts | {item.brier:.4f} |"
        )
    lines += [
        "",
        "L'ecart est ce qui compte : positif, la source se surestime. Le score de Brier "
        "compare les sources entre elles, plus bas est meilleur (0.25 equivaut a annoncer "
        "50 % partout).",
    ]
    if not all(item.meaningful for item in rows):
        lines += [
            "",
            f"**Moins de {MEANINGFUL_SAMPLE} pronostics regles** : ces ecarts ne se "
            "distinguent pas encore du hasard, et ne suffisent pas a changer les poids "
            "du consensus.",
        ]
    return "\n".join(lines)
