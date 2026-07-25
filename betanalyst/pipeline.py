"""Orchestration : Forebet -> Flashscore -> Poisson -> LM Studio -> rapport."""

from __future__ import annotations

import logging
from pathlib import Path

from betanalyst import demo, poisson
from betanalyst.config import AppConfig
from betanalyst.llm import LMStudioClient, LMStudioError
from betanalyst.models import (
    Analysis,
    BookmakerLine,
    ForebetPrediction,
    MatchBundle,
    MatchStats,
)
from betanalyst.sources import bookmakers, flashscore, forebet
from betanalyst.sources.bookmakers import BookmakerOdds
from betanalyst.sources.flashscore import FlashscoreUnavailable

log = logging.getLogger(__name__)


def _collect_stats(
    prediction: ForebetPrediction, cfg: AppConfig, *, use_flashscore: bool, offline: bool
) -> MatchStats:
    if offline:
        fixture = demo.stats_for(prediction.home_team, prediction.away_team)
        if fixture:
            return fixture
    elif use_flashscore:
        try:
            stats = flashscore.fetch_match_stats(
                prediction.home_team, prediction.away_team, cfg.scrape
            )
            stats.competition = stats.competition or prediction.competition
            stats.kickoff = stats.kickoff or prediction.kickoff
            return stats
        except (FlashscoreUnavailable, OSError, ValueError) as exc:
            log.warning("Flashscore indisponible pour %s : %s", prediction.home_team, exc)

    return MatchStats(
        home_team=prediction.home_team,
        away_team=prediction.away_team,
        kickoff=prediction.kickoff,
        competition=prediction.competition,
    )


def collect_odds(cfg: AppConfig, *, odds_csv: Path | None = None) -> list[BookmakerOdds]:
    """Cotes Unibet (API publique) completees par un fichier manuel (ParionsSport)."""
    entries: list[BookmakerOdds] = []
    try:
        entries.extend(bookmakers.fetch_unibet(cfg.scrape))
    except (OSError, ValueError) as exc:
        log.warning("Cotes Unibet indisponibles : %s", exc)
    if odds_csv:
        entries.extend(bookmakers.load_csv(odds_csv))
    return entries


def _lines_for(
    prediction: ForebetPrediction, entries: list[BookmakerOdds]
) -> list[BookmakerLine]:
    lines: list[BookmakerLine] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.bookmaker in seen:
            continue
        if bookmakers.fixture_matches(entry, prediction.home_team, prediction.away_team):
            lines.append(BookmakerLine(bookmaker=entry.bookmaker, odds=entry.odds))
            seen.add(entry.bookmaker)
    return lines


def build_bundles(
    predictions: list[ForebetPrediction],
    cfg: AppConfig,
    *,
    use_flashscore: bool = True,
    offline: bool = False,
    odds: list[BookmakerOdds] | None = None,
) -> list[MatchBundle]:
    """Assemble, pour chaque match, stats + prediction Forebet + Poisson + cotes."""
    bundles: list[MatchBundle] = []
    for prediction in predictions:
        stats = _collect_stats(prediction, cfg, use_flashscore=use_flashscore, offline=offline)
        bundles.append(
            MatchBundle(
                stats=stats,
                forebet=prediction,
                poisson=poisson.compute(stats),
                bookmakers=_lines_for(prediction, odds) if odds else [],
            )
        )
    return bundles


def filter_predictions(
    predictions: list[ForebetPrediction],
    entries: list[BookmakerOdds],
    *,
    only_bettable: bool,
    min_probability: float | None,
    min_odds: float | None,
) -> list[ForebetPrediction]:
    """Ne garde que les rencontres cotees et conformes aux seuils demandes."""
    kept: list[ForebetPrediction] = []
    for prediction in predictions:
        lines = _lines_for(prediction, entries)
        if only_bettable and not lines:
            log.info("Ecarte (non cote chez les bookmakers) : %s", prediction.home_team)
            continue

        probability = prediction.best_probability
        if min_probability is not None and (probability is None or probability < min_probability):
            continue

        if min_odds is not None:
            sign = prediction.pick
            available = [line.odds.get(sign) for line in lines if sign] if sign else []
            best = max([value for value in available if value], default=None)
            if best is None or best < min_odds:
                continue

        kept.append(prediction)
    return kept


def analyse_bundles(
    bundles: list[MatchBundle], cfg: AppConfig, *, use_llm: bool = True
) -> list[tuple[MatchBundle, Analysis | None]]:
    """Envoie chaque match au LLM local ; degrade sans bloquer si LM Studio est absent."""
    if not use_llm:
        return [(bundle, None) for bundle in bundles]

    client = LMStudioClient(cfg.lmstudio)
    try:
        available = client.list_models()
        if available and cfg.lmstudio.model not in available:
            log.warning(
                "Modele '%s' non charge dans LM Studio. Modeles disponibles : %s",
                cfg.lmstudio.model,
                ", ".join(available),
            )
    except LMStudioError as exc:
        log.error("%s", exc)
        return [(bundle, None) for bundle in bundles]

    results: list[tuple[MatchBundle, Analysis | None]] = []
    for index, bundle in enumerate(bundles, start=1):
        log.info("[%d/%d] Analyse de %s", index, len(bundles), bundle.label)
        try:
            results.append((bundle, client.analyse(bundle)))
        except LMStudioError as exc:
            log.error("Analyse impossible pour %s : %s", bundle.label, exc)
            results.append((bundle, None))
    return results


def parse_match_argument(text: str) -> ForebetPrediction:
    """Transforme "Lyon vs Rennes" (ou "Lyon - Rennes") en rencontre a analyser."""
    for separator in (" vs ", " VS ", " - ", " contre "):
        if separator in text:
            home, away = text.split(separator, 1)
            return ForebetPrediction(home_team=home.strip(), away_team=away.strip())
    raise ValueError(f"Format attendu 'Equipe A vs Equipe B', recu : {text!r}")


def run(
    cfg: AppConfig,
    *,
    use_flashscore: bool = True,
    use_llm: bool = True,
    use_cache: bool = True,
    offline: bool = False,
    forebet_html: Path | None = None,
    matches: list[str] | None = None,
    use_bookmakers: bool = True,
    only_bettable: bool = False,
    odds_csv: Path | None = None,
    min_probability: float | None = None,
    min_odds: float | None = None,
) -> list[tuple[MatchBundle, Analysis | None]]:
    if matches:
        predictions = [parse_match_argument(text) for text in matches]
    elif offline:
        predictions = demo.predictions()
    else:
        predictions = forebet.fetch_predictions(
            cfg.scrape, use_cache=use_cache, html_file=forebet_html
        )
    if not predictions:
        log.warning("Aucune prediction Forebet recuperee (structure du site modifiee ?)")
        return []

    odds = (
        collect_odds(cfg, odds_csv=odds_csv) if use_bookmakers and not offline else []
    )
    if odds:
        before = len(predictions)
        predictions = filter_predictions(
            predictions,
            odds,
            only_bettable=only_bettable,
            min_probability=min_probability,
            min_odds=min_odds,
        )
        log.info("%d rencontres retenues sur %d apres filtrage", len(predictions), before)
        if not predictions:
            log.warning("Aucune rencontre ne passe les filtres demandes")
            return []

    bundles = build_bundles(
        predictions, cfg, use_flashscore=use_flashscore, offline=offline, odds=odds
    )
    return analyse_bundles(bundles, cfg, use_llm=use_llm)
