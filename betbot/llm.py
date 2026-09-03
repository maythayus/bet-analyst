"""Client LM Studio (API locale compatible OpenAI) et prompts d'analyse."""

from __future__ import annotations

import json
import logging
import re

import requests

from betbot.config import LMStudioConfig
from betbot.models import Analysis, MatchBundle, Verdict

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es un analyste de paris sportifs rigoureux.

Regles absolues :
- Tu n'utilises QUE les donnees fournies dans le message. Tu n'inventes aucun chiffre.
- Si une donnee manque, tu l'ecris explicitement au lieu de la deviner.
- Tu raisonnes etape par etape, puis tu conclus.
- Chaque affirmation est assortie d'une confiance de 1 a 10.
- Tu confrontes systematiquement quatre sources : les stats Flashscore, la prediction
  Forebet, le modele de Poisson et les cotes des bookmakers. Tu signales les desaccords.
- Une cote n'a d'interet que si la probabilite du modele depasse la probabilite
  implicite de cette cote. Tu le verifies avant de recommander un marche.
- Tu couvres les marches combines fournis (1N, 12, N2, les deux marquent, et leurs
  combinaisons) et pas seulement le resultat sec.
- Tu rappelles qu'aucune prediction n'est certaine.

Format de reponse (Markdown, sans preambule) :
### Donnees manquantes
### Lecture des statistiques
### Confrontation Forebet / Poisson
### Verdict
| Marche | Probabilite estimee | Confiance /10 |
### Risques

Puis tu reponds a quatre questions fermees, dans un unique bloc JSON qui termine ta
reponse (rien apres lui) :
```json
{"decision": "jouer" | "eviter" | "ne pas jouer",
 "marche": "le marche retenu, ou null",
 "source_moins_credible": "forebet" | "modele" | "marche" | "aucune",
 "risque_principal": "une phrase",
 "confiance": 1 a 10}
```
- decision : « jouer » si un marche vaut la mise, « eviter » si la rencontre est piegeuse,
  « ne pas jouer » si aucune source ne se detache.
- source_moins_credible : celle dont le chiffre s'ecarte le plus des deux autres.
"""

REBUTTAL_SYSTEM_PROMPT = """Tu es l'avocat du diable d'un analyste de paris sportifs.

On te donne les donnees d'une rencontre et l'analyse qu'un confrere en a tiree. Ta tache
n'est pas de la refaire : c'est de chercher, dans les donnees seulement, ce qui la
contredit. Tu n'inventes aucun chiffre ; si rien ne la contredit, tu le dis.

Format de reponse (Markdown, sans preambule) :
### Ce qui contredit l'analyse
### Ce qu'elle a neglige

Puis un unique bloc JSON qui termine ta reponse :
```json
{"objection": "la contradiction la plus forte, en une phrase",
 "verdict_maintenu": true | false,
 "confiance_revisee": 1 a 10}
```
- verdict_maintenu vaut false seulement si une donnee fournie renverse la decision.
"""

USER_TEMPLATE = """Analyse la rencontre suivante.

Donnees (JSON) :
```json
{payload}
```

Rappels : les probabilites Forebet sont en %, celles du modele de Poisson aussi.
`flashscore.*.recent_matches` ne montre que les derniers matchs, alors que le modele en a
utilise davantage : ne conclus pas d'une absence dans cette liste. `poisson.markets`
donne la probabilite de chaque marche combine ; `best_odds` et `market_odds` donnent les
cotes reellement disponibles ; `value_gap_poisson_vs_market` donne l'ecart en points
entre le modele et le marche pour 1, X et 2.
"""

REBUTTAL_TEMPLATE = """Donnees de la rencontre (JSON) :
```json
{payload}
```

Analyse du confrere :
{analysis}

Cherche ce qui, dans les donnees, contredit son verdict.
"""

# Message du serveur local quand le prompt depasse la fenetre du modele charge.
_CONTEXT_MARKERS = ("context size", "context length", "n_ctx")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_DECISIONS = ("jouer", "eviter", "ne pas jouer")
_SOURCES = ("forebet", "modele", "marche", "aucune")


def _fold(text: str) -> str:
    return text.lower().replace("é", "e").replace("è", "e").strip()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _score(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(1, min(10, round(value)))


def _last_json(text: str) -> dict[str, object] | None:
    """Dernier bloc JSON de la reponse ; un LLM qui bavarde apres n'annule pas tout."""
    for block in reversed(_JSON_BLOCK.findall(text)):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def strip_verdict(markdown: str) -> str:
    """Le Markdown sans son bloc JSON final, qui est lu a part."""
    blocks = list(_JSON_BLOCK.finditer(markdown))
    if not blocks:
        return markdown.strip()
    last = blocks[-1]
    return (markdown[: last.start()] + markdown[last.end() :]).strip()


def parse_verdict(markdown: str) -> Verdict | None:
    """Reponses fermees du LLM. Une valeur hors des choix proposes est ignoree plutot
    qu'affichee : « peut-etre » n'est pas une decision."""
    data = _last_json(markdown)
    if data is None:
        return None
    decision = _text(data.get("decision"))
    source = _text(data.get("source_moins_credible"))
    return Verdict(
        decision=_fold(decision) if decision and _fold(decision) in _DECISIONS else None,
        market=_text(data.get("marche")),
        least_credible=_fold(source) if source and _fold(source) in _SOURCES else None,
        main_risk=_text(data.get("risque_principal")),
        confidence=_score(data.get("confiance")),
    )


def parse_rebuttal(markdown: str) -> tuple[bool | None, int | None]:
    """(verdict maintenu, confiance revisee) du second passage."""
    data = _last_json(markdown)
    if data is None:
        return None, None
    upheld = data.get("verdict_maintenu")
    return (upheld if isinstance(upheld, bool) else None), _score(data.get("confiance_revisee"))


class LMStudioError(RuntimeError):
    """Le serveur LM Studio est injoignable ou renvoie une erreur."""


class LMStudioClient:
    def __init__(self, cfg: LMStudioConfig) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        )

    def list_models(self) -> list[str]:
        try:
            response = self.session.get(f"{self.cfg.base_url}/models", timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LMStudioError(
                f"LM Studio injoignable sur {self.cfg.base_url}. "
                "Ouvre l'onglet 'Developer' de LM Studio et demarre le serveur local."
            ) from exc
        return [item["id"] for item in response.json().get("data", [])]

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        try:
            response = self.session.post(
                f"{self.cfg.base_url}/chat/completions",
                data=json.dumps(payload),
                timeout=self.cfg.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LMStudioError(self._failure(exc)) from exc
        return response.json()["choices"][0]["message"]["content"]

    def _failure(self, exc: requests.RequestException) -> str:
        """Message d'echec, en distinguant la fenetre de contexte du reste.

        Un contexte trop court fait rejeter la rencontre entiere : le rapport sort alors
        sans commentaire, sans que rien n'explique pourquoi.
        """
        body = exc.response.text.lower() if exc.response is not None else ""
        if any(marker in body for marker in _CONTEXT_MARKERS):
            return (
                "LM Studio a refuse l'analyse : le prompt depasse la fenetre de contexte "
                "du modele charge. Augmente « Context Length » dans LM Studio (16384 "
                "suffit largement) puis recharge le modele."
            )
        return f"Echec de l'appel a LM Studio : {exc}"

    def analyse(self, bundle: MatchBundle) -> Analysis:
        payload = json.dumps(
            bundle.to_prompt_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        raw = self.chat(SYSTEM_PROMPT, USER_TEMPLATE.format(payload=payload))
        cleaned = _THINK_BLOCK.sub("", raw).strip()
        verdict = parse_verdict(cleaned)
        if verdict is None:
            log.warning("Le LLM n'a pas repondu aux questions fermees pour %s", bundle.label)
        analysis = Analysis(
            match=bundle.label,
            markdown=strip_verdict(cleaned),
            raw=raw,
            model=self.cfg.model,
            verdict=verdict,
        )
        log.info("Analyse LLM terminee pour %s", bundle.label)
        if self.cfg.second_pass:
            self._rebut(analysis, payload)
        return analysis

    def _rebut(self, analysis: Analysis, payload: str) -> None:
        """Second appel : le LLM attaque sa propre analyse avec les memes donnees.

        Deux appels courts valent mieux qu'un long : c'est la seule facon utile de lui
        « donner du temps ». Une objection qui renverse le verdict abaisse la confiance ;
        elle ne modifie aucune probabilite, le LLM n'en calcule pas.
        """
        raw = self.chat(
            REBUTTAL_SYSTEM_PROMPT,
            REBUTTAL_TEMPLATE.format(payload=payload, analysis=analysis.markdown),
        )
        cleaned = _THINK_BLOCK.sub("", raw).strip()
        upheld, revised = parse_rebuttal(cleaned)
        analysis.rebuttal = strip_verdict(cleaned)
        analysis.upheld = upheld
        analysis.raw = f"{analysis.raw}\n\n---\n\n{raw}"
        if analysis.verdict and revised is not None:
            analysis.verdict.confidence = revised
        log.info(
            "Contre-analyse terminee pour %s : verdict %s",
            analysis.match,
            {True: "maintenu", False: "renverse", None: "non tranche"}[upheld],
        )
