"""Client LM Studio (API locale compatible OpenAI) et prompts d'analyse."""

from __future__ import annotations

import json
import logging
import re

import requests

from betanalyst.config import LMStudioConfig
from betanalyst.models import Analysis, MatchBundle

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
"""

USER_TEMPLATE = """Analyse la rencontre suivante.

Donnees (JSON) :
```json
{payload}
```

Rappels : les probabilites Forebet sont en %, celles du modele de Poisson aussi.
Les moyennes de buts portent sur les 5 derniers matchs disponibles.
`poisson.markets` donne la probabilite de chaque marche combine ; `bookmakers` et
`best_odds` donnent les cotes reellement disponibles ; `value_gap_poisson_vs_market`
donne l'ecart en points entre le modele et le marche pour 1, X et 2.
"""

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


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
            raise LMStudioError(f"Echec de l'appel a LM Studio : {exc}") from exc
        return response.json()["choices"][0]["message"]["content"]

    def analyse(self, bundle: MatchBundle) -> Analysis:
        payload = json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, default=str)
        raw = self.chat(SYSTEM_PROMPT, USER_TEMPLATE.format(payload=payload))
        cleaned = _THINK_BLOCK.sub("", raw).strip()
        log.info("Analyse LLM terminee pour %s", bundle.label)
        return Analysis(match=bundle.label, markdown=cleaned, raw=raw, model=self.cfg.model)
