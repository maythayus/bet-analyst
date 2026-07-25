# betanalyst

Pipeline local d'aide à l'analyse de paris sportifs :

```
Forebet (probabilités)  ┐
Flashscore (stats brutes) ├─> modèle de Poisson ─> LLM local (LM Studio) ─> rapport Markdown
                          ┘
```

Rien ne sort de ta machine : le LLM tourne dans LM Studio, sur le GPU.

> Aucun modèle, statistique ou LLM, ne prédit un résultat sportif de manière fiable.
> Cet outil est une aide à la décision, pas un oracle. Joue de façon responsable.

## Installation (Windows, RTX 5070)

```powershell
cd bet-analyst
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # nécessaire uniquement pour Flashscore
```

## Côté LM Studio

1. Télécharge le modèle **DeepSeek-R1-Distill-Llama-8B** en quantification `Q4_K_M`
   (~5 Go, tient largement dans les 12 Go de VRAM de la 5070).
2. Charge-le avec un contexte de 16384 tokens et **GPU offload au maximum**.
3. Onglet **Developer** → **Start Server** (par défaut `http://localhost:1234`).

## Utilisation

```powershell
# Test hors ligne, sans réseau ni LLM : vérifie que tout fonctionne
python -m betanalyst --demo --no-llm

# Test hors ligne avec le LLM (valide la connexion à LM Studio)
python -m betanalyst --demo

# Un match précis : Flashscore + Poisson + LLM, sans passer par Forebet
python -m betanalyst --match "Lyon vs Rennes"

# Analyse du jour : 5 matchs, Forebet + Flashscore + LLM
python -m betanalyst --matches 5

# Sans Flashscore (plus rapide, Forebet + Poisson uniquement)
python -m betanalyst --matches 10 --no-flashscore
```

Options utiles : `--model`, `--base-url`, `--temperature`, `--output`, `--no-cache`, `-v`.

### Si Forebet renvoie un contrôle anti-bot

Forebet est derrière Cloudflare. Le script tente d'abord une requête HTTP simple, puis
rebascule automatiquement sur Chromium (profil persistant : le cookie `cf_clearance`
est réutilisé ensuite). Si le contrôle bloque quand même :

```powershell
# 1) résoudre le contrôle à la main une fois, fenêtre visible
$env:FLASHSCORE_HEADLESS="0"; python -m betanalyst --matches 5

# 2) ou enregistrer la page depuis ton navigateur (Ctrl+S) et la relire
python -m betanalyst --forebet-html C:\chemin\forebet.html

# 3) ou se passer complètement de Forebet
python -m betanalyst --match "Lyon vs Rennes" --match "Getafe vs Athletic Bilbao"
```

Les rapports sont écrits dans `out/` : `rapport-<date>.md` (lisible) et
`donnees-<date>.json` (données brutes + analyse, réutilisable).

## Configuration par variables d'environnement

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | API LM Studio |
| `LMSTUDIO_MODEL` | `deepseek-r1-distill-llama-8b` | modèle chargé |
| `LMSTUDIO_TEMPERATURE` | `0` | déterminisme (à laisser à 0) |
| `MAX_MATCHES` | `10` | nombre de matchs |
| `FLASHSCORE_HEADLESS` | `1` | `0` pour voir le navigateur |
| `FOREBET_URL` | page « predictions for today » | listing à scraper |

## Architecture

| Fichier | Rôle |
| --- | --- |
| `betanalyst/sources/forebet.py` | scraping du listing Forebet (requests + BeautifulSoup) |
| `betanalyst/sources/flashscore.py` | recherche des équipes (API JSON) + derniers résultats via Playwright |
| `betanalyst/poisson.py` | buts attendus, 1X2, over 2.5, BTTS, score le plus probable |
| `betanalyst/llm.py` | client LM Studio + prompt système « analyste rigoureux » |
| `betanalyst/pipeline.py` | orchestration, dégradation propre si une source manque |
| `betanalyst/report.py` | rapport Markdown + export JSON |

## Fiabilité : ce que fait le pipeline

- **Température 0** → analyses reproductibles, pas d'invention de chiffres.
- **Trois sources croisées** (stats réelles, Forebet, Poisson) : le prompt force le
  modèle à signaler les désaccords plutôt qu'à trancher au hasard.
- **Probabilités implicites des cotes** calculées en retirant la marge du bookmaker,
  pour comparer au marché.
- **Niveau de confiance /10** exigé sur chaque affirmation, et section « données
  manquantes » obligatoire.

## Tests

```powershell
python -m unittest discover -s tests
python -m ruff check .
```

## Limites connues

- Le HTML de Forebet et de Flashscore change régulièrement : le parsing est défensif
  mais les sélecteurs de `sources/` sont à réajuster si un site se réorganise.
- Le scraping Flashscore a été validé en conditions réelles ; celui de Forebet n'a pas
  pu l'être depuis la machine de développement (Cloudflare bloque l'IP), d'où les trois
  contournements ci-dessus.
- La forme récente inclut les matchs amicaux : en pré-saison, les moyennes de buts sont
  à prendre avec des pincettes (le modèle les régularise, mais ne les distingue pas).
- Les cotes `X` et `2` ne sont pas toujours présentes sur le listing Forebet ; elles
  peuvent être complétées à la main dans le JSON.
- Usage strictement personnel : respecte les CGU des deux sites et le `robots.txt`
  (délai de politesse de 2 s et cache local d'1 h intégrés).
