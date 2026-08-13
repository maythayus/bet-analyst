"""Choix des rencontres a analyser quand la journee en compte plus que le plafond.

Sans regle, le plafond coupait simplement dans l'ordre du listing : une rencontre de
troisieme division islandaise passait devant un match de Premier League. Deux criteres
les classent maintenant, dans cet ordre :

- la **competition**, qui est la seule facon honnete d'approcher les « equipes connues » :
  un club est connu parce qu'il joue dans une grande division ou une coupe europeenne, et
  ces competitions sont aussi celles ou Flashscore fournit un classement complet, donc ou
  le modele est le mieux nourri ;
- le **rendement en buts attendu**, tire de la moyenne de buts publiee par Forebet ou, a
  defaut, de sa probabilite de plus de 2.5 buts.

Ce classement ne dit rien de la valeur d'un pari : il decide seulement de ce qui est
analyse en premier quand il faut trancher.
"""

from __future__ import annotations

from betbot.models import ForebetPrediction

# Competitions par niveau de notoriete, en minuscules et sans accents. Les marqueurs sont
# cherches dans le nom de competition tel que l'ecrit le bookmaker ou Forebet, qui
# abregent volontiers ("Angleterre - Premier League", "ENG PL", "UEFA CL").
TIERS: tuple[tuple[str, ...], ...] = (
    (
        "premier league",
        "laliga",
        "la liga",
        "serie a",
        "bundesliga",
        "ligue 1",
        "champions league",
        "ligue des champions",
        "coupe du monde",
        "world cup",
        "euro",
    ),
    (
        "europa league",
        "conference league",
        "eredivisie",
        "primeira liga",
        "liga portugal",
        "championship",
        "liga mx",
        "brasileirao",
        "serie b",
        "laliga 2",
        "ligue 2",
        "2. bundesliga",
        "jupiler",
        "super lig",
        "mls",
        "coupe de france",
        "fa cup",
        "copa del rey",
        "coppa italia",
        "dfb",
    ),
)
# Niveau attribue a une competition absente des listes : apres les connues, avant rien.
UNKNOWN_TIER = len(TIERS)
# Moyenne de buts prise par defaut quand Forebet n'en publie pas : ni avantage ni
# penalite, c'est la moyenne courante d'un match de football.
NEUTRAL_GOALS = 2.6
OVER_25 = "Plus de 2.5 buts"


def _fold(text: str) -> str:
    return text.lower().replace("-", " ").strip()


def competition_tier(competition: str | None) -> int:
    """Niveau de notoriete de la competition : 0 est le plus connu."""
    if not competition:
        return UNKNOWN_TIER
    name = _fold(competition)
    for tier, markers in enumerate(TIERS):
        if any(marker in name for marker in markers):
            return tier
    return UNKNOWN_TIER


def expected_goals(prediction: ForebetPrediction) -> float | None:
    """Buts attendus dans la rencontre, d'apres Forebet.

    La moyenne publiee par Forebet est preferee ; sinon la probabilite de plus de 2.5
    buts est ramenee sur la meme echelle, une rencontre donnee a 70 % au-dessus de 2.5
    etant plus prolifique qu'une donnee a 30 %.
    """
    if prediction.avg_goals is not None:
        return prediction.avg_goals
    over = prediction.markets.get(OVER_25)
    if over is None:
        return None
    return 2.5 + (over - 50) / 25


def rank_key(prediction: ForebetPrediction) -> tuple[int, float]:
    """Cle de tri : competition connue d'abord, puis rendement en buts decroissant."""
    goals = expected_goals(prediction)
    return (
        competition_tier(prediction.competition),
        -(goals if goals is not None else NEUTRAL_GOALS),
    )


def prioritise(
    predictions: list[ForebetPrediction], limit: int | None = None
) -> list[ForebetPrediction]:
    """Trie les rencontres par interet, puis coupe au plafond demande.

    Le tri est stable : a competition et rendement egaux, l'ordre de la source est
    conserve, donc deux analyses de la meme journee retiennent les memes rencontres.
    """
    ordered = sorted(predictions, key=rank_key)
    return ordered[:limit] if limit is not None else ordered
