# betanalyst

Pipeline local d'aide à l'analyse de paris sportifs :

```
Forebet (probabilités)    ┐
Flashscore (stats brutes) ├─> modèle de Poisson ─> LLM local (LM Studio) ─> rapport Markdown
Unibet (cotes réelles)    ┘
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

### Cotes et matchs réellement pariables

Les cotes 1 N 2 des 50 prochains matchs sont récupérées automatiquement sur l'API
publique d'Unibet France, puis appariées aux prédictions Forebet malgré les
différences de nommage (« Stade Rennais » ↔ « Rennes »).

Ce listing ne contient que le 1 N 2. Pour chaque match retenu par les filtres, la page
Unibet de la rencontre est ensuite ouverte : elle fournit les cotes **les deux équipes
marquent** (oui / non), les **doubles chances** et leurs **combinaisons** (`1N et oui`,
`N2 et non`…). C'est une requête par match, d'où `--no-detailed-odds` si tu veux aller
plus vite sans ces marchés.

Si la page Forebet enregistrée date d'un autre jour, aucun de ses matchs ne figure plus
dans les 50 prochains coups d'envoi d'Unibet et le filtrage ne garde rien. `--from-unibet`
inverse alors la source : les matchs viennent des cotes, donc ils sont pariables par
construction (sans pronostic Forebet, le modèle de Poisson et Flashscore suffisent).

### Lancement en un clic

`analyse.cmd`, à la racine du projet, active le venv s'il existe, lance l'analyse des
prochaines rencontres cotées et **affiche le rapport dans la console** :

```text
analyse.cmd                                   analyse par défaut, ticket de 4 « les deux marquent »
analyse.cmd --matches 20 --odds-range 1.65 1.95 --print    tes propres options
```

En ligne de commande, `--print` affiche le rapport en plus de l'écrire dans `out/`.

```powershell
# analyser directement les prochaines rencontres cotées chez Unibet
python -m betanalyst --from-unibet --matches 10 --combo 4 --combo-market "Les deux marquent : oui"

# ne garder que les matchs cotés chez un bookmaker
python -m betanalyst --matches 20 --only-bettable

# pronostic Forebet à au moins 95 %, avec une cote d'au moins 1.5 sur ce pronostic
python -m betanalyst --matches 20 --only-bettable --min-prob 95 --min-odds 1.5

# uniquement les matchs offrant une cote entre 1.65 et 1.95, tries par valeur
python -m betanalyst --matches 20 --only-bettable --odds-range 1.65 1.95

# ticket combine de 4 "les deux marquent", les plus probables du jour
python -m betanalyst --matches 20 --combo 4 --combo-market "Les deux marquent : oui"

# ... et seulement s'il a au moins 25 % de chances de passer
python -m betanalyst --matches 20 --combo 4 --min-combo-prob 25

# sans ouvrir la page de chaque match (pas de cote BTTS ni de combines)
python -m betanalyst --matches 20 --only-bettable --no-detailed-odds

# sans les cotes
python -m betanalyst --matches 5 --no-bookmakers
```

`--combo N` place en tête du rapport un ticket construit avec les N sélections les
plus probables (une par match), sa probabilité de passer et la **cote minimale à
exiger** pour que le pari ait une espérance positive. `--min-combo-prob PCT` retire les
sélections les moins probables jusqu'à ce que le ticket atteigne le seuil demandé, et
n'affiche rien si même deux sélections n'y suffisent pas.

Exemple réel à 4 sélections : 83.9 % × 74.5 % × 74.2 % × 52.3 % = **24 %**, soit une fois
sur quatre, et une cote minimale de 4.12. Avec `--min-combo-prob 40`, le ticket est
ramené à 3 sélections : 46 % et cote minimale 2.16. C'est le compromis à connaître —
chaque sélection ajoutée gonfle le gain affiché et divise la chance de le toucher.

Avec `--odds-range`, le rapport s'ouvre sur une section **Sélection** : un tableau
récapitulatif donnant, pour chaque match, le marché coté le plus intéressant, sa cote,
la probabilité du modèle et la **valeur** (`cote × probabilité − 1`, l'espérance de gain
par euro misé si le modèle a raison). En dessous de +5 % l'écart est dans le bruit du
modèle ; au-dessus de +20 %, il faut suspecter une donnée manquante plutôt qu'une
aubaine.

> Une probabilité de 95 % correspond mécaniquement à une cote d'environ 1.05. Un
> pronostic à 95 % assorti d'une cote élevée signifie que Forebet et le bookmaker sont
> en désaccord profond : c'est un signal de méfiance, pas une opportunité garantie.

ParionsSport (FDJ) protège son API par un captcha DataDome ; ses cotes se saisissent
donc à la main dans un fichier, au choix `match;1;X;2` ou `match;marché;cote` :

```text
Lyon vs Rennes;1.80;3.60;4.20
Lyon vs Rennes;1N et oui;2.35
```

```powershell
python -m betanalyst --matches 20 --only-bettable --odds-csv cotes.csv
```

### Marchés calculés

À partir de la matrice des scores exacts, le modèle donne la probabilité de chaque
marché courant : `1`, `N`, `2`, doubles chances `1N` / `12` / `N2`, « les deux équipes
marquent » oui/non, et les combinés `1N et oui`, `12 et oui`, `N2 et oui`, `1N et non`…
Le rapport affiche pour chacun la **cote équitable** (celle en dessous de laquelle le
pari perd de l'argent si le modèle a raison) et la compare à la cote proposée.

### Si Forebet renvoie un contrôle anti-bot

Forebet est derrière Cloudflare, qui bloque aussi bien la requête HTTP directe que
Chromium piloté par Playwright. **La méthode qui fonctionne** est d'enregistrer la page
depuis ton navigateur habituel (Ctrl+S, « Page Web, complète ») puis :

```powershell
# guillemets obligatoires : le nom du fichier contient des espaces
python -m betanalyst --forebet-html "C:\Users\<toi>\Desktop\Football Predictions for Today _ Forebet.htm"
```

Sinon, on se passe complètement de Forebet (Flashscore + Poisson + LLM) :

```powershell
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
| `betanalyst/sources/bookmakers.py` | cotes Unibet (listing 1 N 2 + marchés de la page match), CSV manuel, appariement des noms d'équipes |
| `betanalyst/poisson.py` | buts attendus, 1X2, over 2.5, BTTS, score le plus probable |
| `betanalyst/llm.py` | client LM Studio + prompt système « analyste rigoureux » |
| `betanalyst/pipeline.py` | orchestration, dégradation propre si une source manque |
| `betanalyst/report.py` | rapport Markdown + export JSON |

## Fiabilité : ce que fait le pipeline

- **Température 0** → analyses reproductibles, pas d'invention de chiffres.
- **Quatre sources croisées** (stats réelles, Forebet, Poisson, cotes) : le prompt force
  le modèle à signaler les désaccords plutôt qu'à trancher au hasard.
- **Écart modèle / marché** calculé pour chaque signe : une cote n'a d'intérêt que si la
  probabilité du modèle dépasse la probabilité implicite de cette cote.
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
- Le scraping direct de Forebet est bloqué par Cloudflare (testé en IP datacenter et en
  IP résidentielle, headless et fenêtre visible) : utilise `--forebet-html`. Le parser
  lui-même est validé, il extrait bien les prédictions d'une page enregistrée.
- Comptez environ 5 s par équipe : 10 matchs = 20 pages Flashscore à charger.
- La forme récente inclut les matchs amicaux : en pré-saison, les moyennes de buts sont
  à prendre avec des pincettes (le modèle les régularise, mais ne les distingue pas).
- Les cotes `X` et `2` ne sont pas toujours présentes sur le listing Forebet ; elles
  peuvent être complétées à la main dans le JSON.
- L'API Unibet n'expose que les 50 prochaines rencontres ; les marchés combinés viennent
  de la page de chaque rencontre, dont la structure HTML peut changer sans préavis.
- Seul le temps réglementaire (« 90 Mins ») est lu : les marchés de mi-temps n'ont pas
  d'équivalent dans le modèle.
- ParionsSport n'est pas accessible automatiquement (captcha DataDome) : saisie manuelle.
- Usage strictement personnel : respecte les CGU des deux sites et le `robots.txt`
  (délai de politesse de 2 s et cache local d'1 h intégrés).
