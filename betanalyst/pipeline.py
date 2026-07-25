"""Orchestration : Forebet -> Flashscore -> Poisson -> LM Studio -> rapport."""

from __future__ import annotations

import logging
from pathlib import Path

from betanalyst import demo, poisson
from betanalyst.config import AppConfig
from betanalyst.llm import LMStudioClient, LMStudioError
from betanalyst.models import Analysis, ForebetPrediction, MatchBundle, MatchStats
from betanalyst.sources import flashscore, forebet
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


def build_bundles(
    predictions: list[ForebetPrediction],
    cfg: AppConfig,
    *,
    use_flashscore: bool = True,
    offline: bool = False,
) -> list[MatchBundle]:
    """Assemble, pour chaque match, stats + prediction Forebet + modele Poisson."""
    bundles: list[MatchBundle] = []
    for prediction in predictions:
        stats = _collect_stats(prediction, cfg, use_flashscore=use_flashscore, offline=offline)
        bundles.append(
            MatchBundle(stats=stats, forebet=prediction, poisson=poisson.compute(stats))
        )
    return bundles


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
    bundles = build_bundles(predictions, cfg, use_flashscore=use_flashscore, offline=offline)
    return analyse_bundles(bundles, cfg, use_llm=use_llm)
