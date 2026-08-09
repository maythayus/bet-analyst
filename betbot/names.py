"""Rapprochement des noms d'equipes d'une source a l'autre.

Un bookmaker ecrit « Etoile Rouge Belgrade », Flashscore « Crvena zvezda », Forebet
« Crvena Zvezda Belgrade », et un classement « Cr. zvezda ». Comparer ces libelles est un
prealable a tout le reste : sans cela, les cotes ne se collent pas aux statistiques, et un
adversaire ne se retrouve pas au classement.

Ces fonctions sont volontairement isolees ici : elles ne dependent ni du modele, ni du
reseau, et sont utilisees par les scrapers comme par le calcul de la force des equipes.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Suffixes et prefixes de club sans valeur discriminante. "City" et "United" en sont
# volontairement absents : ils distinguent des clubs d'une meme ville.
_NOISE = re.compile(
    r"\b(fc|cf|sc|ac|as|ss|us|sv|if|fk|sk|nk|hk|bk|afc|cd|ud|rc|rcd|club"
    r"|pfk|pfc|ofk|mfk|msk|bsc|vfb|vfl|fsv|tsv|tsg|ssc)\b"
)
# Annee de fondation accolee au nom : « FK DAC 1904 » et « DAC Dunajska Streda ».
_FOUNDED = re.compile(r"\b(1[89]|20)\d{2}\b")
TOKEN_THRESHOLD = 0.75
ABBREVIATION_LENGTH = 2
PREFIX_LENGTH = 4
# En dessous, deux libelles ne designent probablement pas le meme club.
SIMILARITY_THRESHOLD = 0.6


def normalise(name: str) -> str:
    """Cle de comparaison insensible aux accents, ponctuations et suffixes de club."""
    text = unicodedata.normalize("NFKD", name.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = _FOUNDED.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def _same_token(left: str, right: str) -> bool:
    if left == right:
        return True
    shortest, longest = sorted((left, right), key=len)
    if longest.startswith(shortest) and (
        len(shortest) >= PREFIX_LENGTH or len(shortest) <= ABBREVIATION_LENGTH
    ):
        # « U. Cluj » pour Universitatea Cluj : une initiale suivie d'un point est une
        # abreviation courante des grilles de cotes comme de Flashscore.
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
