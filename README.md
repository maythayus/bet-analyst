# Bet.Bot

Pipeline local d'aide à l'analyse de paris sportifs :

```
Forebet (probabilités)    ┐
Flashscore (stats brutes) ├─> modèle de Poisson ─> LLM local (LM Studio) ─> rapport Markdown
Unibet (cotes réelles)    ┘
```

Rien ne sort de ta machine : le LLM tourne dans LM Studio, sur le GPU.

## Ce que fait l'outil, et ce qu'il ne fait pas

Il **croise quatre sources** pour chaque rencontre pariable :

1. **Forebet** — les probabilités publiées par un site de pronostics, prises comme un
   avis extérieur, pas comme une vérité. URLS pour le fichiers Forebet.htm = https://www.forebet.com/en/football-tips-and-predictions-for-today/predictions-both-to-score
2. **Flashscore** — les statistiques brutes : cinq derniers matchs de chaque équipe,
   buts marqués et encaissés, confrontations directes.
3. **Modèle de Poisson** — un calcul maison qui déduit la probabilité de chaque marché
   d'une matrice de scores exacts (voir [Le modèle](#le-modèle-poisson)).
4. **Unibet** — les cotes réellement proposées : 1 N 2 du listing, puis « les deux
   équipes marquent », doubles chances et combinés lus sur la page de chaque match.

Le tout est ensuite résumé par un **LLM local** (par défaut
`deepseek-r1-distill-llama-8b` dans LM Studio ; `--model` pour en changer), dont le rôle
est de commenter les désaccords entre sources — pas d'inventer un pronostic.

Sont analysés **tous les matchs pariables**, c'est-à-dire ceux pour lesquels une cote
Unibet existe (`--only-bettable`, ou `--from-unibet` pour partir directement de la
grille des cotes).

## Le modèle Poisson

Le modèle construit une **matrice des scores exacts** (0-0 à 8-8) à partir de deux
nombres : les buts attendus de chaque équipe. Toutes les probabilités affichées — 1 N 2,
doubles chances, plus/moins de buts, « les deux marquent », marchés combinés — sont des
sommes de cases de cette matrice, donc cohérentes entre elles par construction.

Deux estimations de ces buts attendus sont disponibles, au choix (`--poisson`) :

| Modèle | Ce qu'il fait | Ce qu'il donne |
| --- | --- | --- |
| `forme` (défaut) | deux lois de Poisson nourries par les cinq derniers matchs, les cotes ne servent qu'à mesurer l'écart | probabilités tranchées, beaucoup de valeur affichée, une partie étant de l'erreur d'estimation |
| `marche` | matrice calée sur les cotes dont la marge a été retirée, forme en ajustement borné | reproduit le marché à ~2 points près, donc presque jamais de pari à espérance positive |

Le modèle `forme` est celui des premières versions : c'est lui qui produit les combinés
à forte cote. Le rapport affiche pour chaque match son **écart maximal au marché** ;
au-delà d'une dizaine de points, cet écart mesure d'abord l'incertitude du modèle, pas
une occasion. `--poisson marche` donne la lecture prudente du même match.

Deux corrections disponibles par rapport à un Poisson d'école, actives sur `marche` :

- **Dixon-Coles** : deux lois de Poisson indépendantes sous-estiment les scores serrés
  (0-0, 1-0, 0-1, 1-1). Le modèle leur rend leur poids, ce qui corrige surtout le
  « les deux marquent : non », auparavant sous-estimé de près de 16 points.
- **Calage sur les cotes** : quand un groupe d'issues exhaustif est coté (1 N 2, une
  ligne de buts, BTTS oui/non), la marge du bookmaker est retirée puis les buts attendus
  sont ajustés pour reproduire ces probabilités. La forme récente ne sert plus qu'à un
  écart borné (25 % de poids, 12 % d'amplitude maximale).

Chaque match indique donc l'origine de son estimation :

| Source affichée | Signification |
| --- | --- |
| `forme recente` | modèle par défaut : cinq matchs de forme, sans référence aux cotes |
| `cotes` | calée sur le marché, aucune donnée de forme exploitable |
| `cotes + forme` | calée sur le marché, légèrement inclinée par la forme récente |
| `forme seule` | `--poisson marche` sans aucune cote : ordre de grandeur, rien de plus |

Ce que vaut chacun : mesuré sur 250 matchs de rapports réels, l'ancien modèle s'écartait
des probabilités du marché de **11,5 points en moyenne**, avec des dérapages absurdes
(Coleraine donné gagnant à 70 % contre HJK Helsinki quand le marché le donnait à 24 % ;
résultat 0-3). Cinq matchs de forme, sans correction du niveau de la ligue ni de
l'adversaire, ne suffisent pas à estimer une équipe. Le modèle recalé tombe à
**2,2 points** d'écart moyen.

Les deux lectures ont donc leur intérêt : le modèle de forme propose des tickets, le
modèle de marché dit ce que le bookmaker en pense. **Un modèle calé sur les cotes ne
trouve presque jamais de pari à espérance positive**, puisqu'il reproduit le marché
diminué de la marge — c'est le comportement attendu, pas un défaut, et sa mise
conseillée vaut « ne pas jouer » la plupart du temps.

Les combinés s'adaptent au modèle choisi. Avec `marche`, les sélections sont classées
par espérance de gain. Avec `forme`, elles le sont par probabilité décroissante : ce
modèle s'écartant du marché par construction, trier ses sélections par valeur
reviendrait à choisir celles où il se trompe le plus. Dans les deux cas, une seule
sélection par match, aucune cote en dessous de 1.20, et la mise conseillée est un quart
du critère de Kelly plafonné à 5 % du capital.

### Ce que ça ne prouve pas

Une sélection affichée n'est **jamais une validation ni une certitude** : c'est un
pronostic, issu de sites de prédiction et d'un modèle statistique qui se trompent tous
les deux régulièrement.

- Une probabilité de 80 % veut dire que l'issue **ne se produit pas une fois sur cinq** ;
  se tromper n'est pas une anomalie, c'est prévu par le calcul.
- Une « valeur » positive ne signifie rien en soi : elle suppose que mon modèle, bâti
  sur cinq matchs de forme, soit mieux calibré qu'Unibet, qui dispose de bien plus de
  données. C'est rarement le cas.
- Le modèle ignore l'essentiel de ce qui décide un match : blessures, suspensions,
  turnover, météo, enjeu, motivation, arbitrage.
- Les probabilités d'un ticket combiné supposent les matchs **indépendants**, ce qu'ils
  ne sont jamais totalement : la probabilité réelle est plus basse que celle affichée.
- Les cotes bougent en permanence : celles du rapport valent pour l'instant où il a été
  généré.

### Risques du jeu

Le pari sportif est un jeu d'argent, et un jeu d'argent **est conçu pour être perdant
à long terme** : la marge du bookmaker (5 à 8 % sur le 1 N 2) est prélevée sur chaque
mise, gagnante ou perdante. Aucun logiciel ne supprime cette marge.

- Ne mise **que ce que tu peux perdre entièrement**, jamais un argent nécessaire (loyer,
  factures, crédit), jamais de l'argent emprunté.
- Ne cherche jamais à « se refaire » après une perte : c'est le mécanisme qui transforme
  une mauvaise soirée en dette.
- Même avec un vrai avantage, la variance impose des séries de dix pertes d'affilée.
  Une mise unitaire au-delà de 1 à 2 % de ta bankroll finit par tout emporter.
- Les tickets gagnants montrés sur les réseaux sociaux sont une sélection d'images : les
  perdants ne sont pas publiés, et beaucoup de ces comptes vendent un abonnement.
- Le jeu peut devenir une addiction. Si tu joues plus que prévu, si tu caches tes mises,
  si tu y penses en permanence : **09 74 75 13 13** (Joueurs Info Service, appel non
  surtaxé, 8h-2h) ou [joueurs-info-service.fr](https://www.joueurs-info-service.fr/).
- Interdiction volontaire de jeux possible auprès de l'ANJ : [anj.fr](https://anj.fr/).
- Jeu interdit aux mineurs.

> Aucun modèle, statistique ou LLM, ne prédit un résultat sportif de manière fiable.
> Cet outil est une aide à la décision, pas un oracle. Joue de façon responsable.

## Matériel

Le seul élément exigeant est le LLM local. Le reste (scraping, Poisson, rapports) tourne
sur n'importe quelle machine : quelques centaines de Mo de RAM et aucun GPU.

Modèle de référence : **DeepSeek-R1-Distill-Llama-8B** (8 milliards de paramètres),
chargé dans LM Studio.

| Quantification | Taille du fichier | VRAM à prévoir | Commentaire |
| --- | --- | --- | --- |
| `Q4_K_M` | ~4.9 Go | **8 Go** | le bon compromis, celui utilisé ici |
| `Q5_K_M` | ~5.7 Go | 10 Go | légèrement meilleur, à partir de 12 Go de VRAM |
| `Q8_0` | ~8.5 Go | 12 Go | gain marginal pour cet usage |

La VRAM indiquée inclut le contexte : à 16384 tokens, le cache occupe environ 1 Go
en plus du fichier. Sur une **RTX 5070 (12 Go)**, `Q4_K_M` avec 16384 tokens tient
largement, GPU offload au maximum, et un match s'analyse en quelques secondes.

| Composant | Minimum | Confortable |
| --- | --- | --- |
| GPU | 8 Go de VRAM (RTX 3060, 4060) | 12 Go et plus (RTX 4070, 5070) |
| RAM système | 8 Go | 16 Go |
| Disque | 10 Go libres (modèle + Chromium + caches) | 20 Go |
| Connexion | indispensable (Unibet, Flashscore) | — |

**Sans GPU suffisant**, deux voies :

- Laisser LM Studio décharger une partie des couches sur le CPU : ça fonctionne, mais
  comptez plusieurs minutes par match plutôt que quelques secondes.
- Prendre un modèle plus léger, par exemple `qwen2.5-7b-instruct` en `Q4_K_M` (~4.7 Go)
  ou `llama-3.2-3b-instruct` en `Q4_K_M` (~2 Go), avec
  `python -m betbot --model <identifiant>`.

**Sans LLM du tout**, `--no-llm` produit le rapport complet — probabilités, cotes, valeur,
ticket — sans le commentaire rédigé. C'est la partie chiffrée, et elle ne dépend pas du
modèle : le LLM ne fait que commenter, il ne calcule rien.

## Prérequis

| Élément | Version | À quoi ça sert |
| --- | --- | --- |
| **Python** | 3.10 ou plus (testé en 3.13) | fait tourner l'outil |
| **Git** | — | récupérer le projet et ses mises à jour (`git pull`) |
| **Chromium** (via Playwright) | installé par `playwright install chromium` | lire les statistiques Flashscore |
| **LM Studio** | facultatif | l'analyse rédigée par le LLM local ; sans lui, le rapport est généré quand même, mais sans commentaire |
| **Un navigateur** | Firefox ou Chrome | enregistrer la page Forebet avec Ctrl+S |

Les dépendances Python, listées dans `requirements.txt` :

| Paquet | Version minimale | Rôle |
| --- | --- | --- |
| `requests` | 2.32.0 | requêtes HTTP : Forebet, API Unibet, LM Studio |
| `beautifulsoup4` | 4.12.3 | lecture du HTML : Forebet, pages de match Unibet |
| `playwright` | 1.45.0 | navigateur piloté, pour Flashscore |

Rien d'autre n'est nécessaire : le modèle de Poisson et les calculs de cotes n'utilisent
que la bibliothèque standard (pas de numpy ni de pandas).

## Installation (Windows, RTX 5070)

```powershell
git clone https://github.com/maythayus/bet-analyst.git
cd bet-analyst
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium   # nécessaire uniquement pour Flashscore
```

Vérifie que tout est en place, sans réseau ni LLM :

```powershell
python -m betbot --demo --no-llm
```

Le venv doit être réactivé (`.\.venv\Scripts\Activate.ps1`) à chaque nouvelle fenêtre
PowerShell — sinon Python ne trouve pas Playwright. `analyse.cmd` s'en charge tout seul.

Pour mettre à jour :

```powershell
git pull
pip install -r requirements.txt   # au cas où une dépendance aurait été ajoutée
```

## Côté LM Studio

1. Télécharge le modèle **DeepSeek-R1-Distill-Llama-8B** en quantification `Q4_K_M`
   (~5 Go, tient largement dans les 12 Go de VRAM de la 5070).
2. Charge-le avec un contexte de 16384 tokens et **GPU offload au maximum**.
3. Onglet **Developer** → **Start Server** (par défaut `http://localhost:1234`).

Un serveur local n'exige aucune clé. S'il en demande une (serveur distant, LM Studio
exposé sur le réseau), passe-la par `--api-key` ou par la variable `LMSTUDIO_API_KEY` :
ne l'écris jamais dans le dépôt.

```powershell
$env:LMSTUDIO_API_KEY = "<ta-cle>"
$env:LMSTUDIO_BASE_URL = "http://<hote>:1234/v1"   # si le serveur n'est pas local
```

## Utilisation

```powershell
# Test hors ligne, sans réseau ni LLM : vérifie que tout fonctionne
python -m betbot --demo --no-llm

# Test hors ligne avec le LLM (valide la connexion à LM Studio)
python -m betbot --demo

# Un match précis : Flashscore + Poisson + LLM, sans passer par Forebet
python -m betbot --match "Lyon vs Rennes"

# Analyse du jour : 5 matchs, Forebet + Flashscore + LLM
python -m betbot --matches 5

# Sans Flashscore (plus rapide, Forebet + Poisson uniquement)
python -m betbot --matches 10 --no-flashscore
```

Options utiles : `--model`, `--base-url`, `--api-key`, `--temperature`, `--output`,
`--no-cache`, `-v`.

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

À la fin de chaque analyse, le rapport Markdown s'ouvre tout seul dans l'application
associée aux fichiers `.md` sous Windows (Bloc-notes par défaut). `--no-open` supprime
cette ouverture, par exemple dans une tâche planifiée.

### Version exécutable (`Bet.Bot.exe`)

`build-exe.cmd` fabrique un `dist\Bet.Bot.exe` autonome (~47 Mo), qui tourne sans Python
installé. Les rapports sont écrits dans un dossier `out\` **à côté de l'exécutable**.

```powershell
.\build-exe.cmd                 # construit dist\Bet.Bot.exe
.\dist\Bet.Bot.exe --install-chromium   # une seule fois : navigateur pour Flashscore
.\dist\Bet.Bot.exe --from-unibet --today --combo 4 --print
```

Le navigateur Chromium (~300 Mo) n'est pas embarqué : il s'installe une fois avec
`--install-chromium`, sinon utilise `--no-flashscore` pour se passer des statistiques.

Par défaut, seuls les 50 prochains coups d'envoi sont récupérés. `--today` enchaîne les
pages du listing pour couvrir **toutes les rencontres de la journée** ; sans `--matches`,
elles sont toutes analysées (compte plusieurs minutes, Flashscore ouvre deux pages par
match). Lancé tard le soir, quand les matchs du jour sont joués, il bascule
automatiquement sur la prochaine journée cotée et l'indique dans les logs.

Les noms d'équipes du bookmaker sont abrégés (« Mac.Tel Aviv », « SherifTiraspol »,
« Universit Cluj ») : la recherche Flashscore les déplie, écarte les joueurs, les
équipes féminines, les réserves et les U19, et se sert du pays de la compétition pour
départager les homonymes (le Libertad d'Équateur et celui du Paraguay). Quand le nom
retenu diffère de celui du bookmaker, la ligne `Flashscore : 'X' identifie comme Y` le
signale. Quand la page du club retenu n'affiche aucun match joué (homonyme amateur, club
inactif), les candidats suivants sont essayés avant d'abandonner ; si aucun ne convient,
le match est analysé sans statistiques et un avertissement le dit.

```powershell
# toute la journée, en partant des cotes
python -m betbot --from-unibet --today --combo 4 --combo-market "Les deux marquent : oui"

# la même journée lue par le modèle calé sur les cotes (lecture prudente)
python -m betbot --from-unibet --today --combo 4 --poisson marche

# analyser directement les prochaines rencontres cotées chez Unibet
python -m betbot --from-unibet --matches 10 --combo 4 --combo-market "Les deux marquent : oui"

# ne garder que les matchs cotés chez un bookmaker
python -m betbot --matches 20 --only-bettable

# pronostic Forebet à au moins 95 %, avec une cote d'au moins 1.5 sur ce pronostic
python -m betbot --matches 20 --only-bettable --min-prob 95 --min-odds 1.5

# uniquement les matchs offrant une cote entre 1.65 et 1.95, tries par valeur
python -m betbot --matches 20 --only-bettable --odds-range 1.65 1.95

# ticket combine de 4 "les deux marquent", les plus probables du jour
python -m betbot --matches 20 --combo 4 --combo-market "Les deux marquent : oui"

# ... et seulement s'il a au moins 25 % de chances de passer
python -m betbot --matches 20 --combo 4 --min-combo-prob 25

# sans ouvrir la page de chaque match (pas de cote BTTS ni de combines)
python -m betbot --matches 20 --only-bettable --no-detailed-odds

# sans les cotes
python -m betbot --matches 5 --no-bookmakers
```

`--combo N` place en tête du rapport un ticket construit avec les N sélections les
plus probables (une par match), sa probabilité de passer et la **cote minimale à
exiger** pour que le pari ait une espérance positive. Les sélections y sont affichées
**du coup d'envoi le plus tôt au plus tard**, et le rapport indique l'**heure limite du
pari** : celle du premier match, au-delà de laquelle le combiné n'est plus jouable.
`--min-combo-prob PCT` retire les
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
python -m betbot --matches 20 --only-bettable --odds-csv cotes.csv
```

### Combinés 6 et 8 sélections

À la suite du ticket `--combo`, le rapport ajoute automatiquement deux combinés longs à
**marchés mélangés** : 6 puis 8 sélections, une seule par match (les issues d'une même
rencontre ne se combinent pas chez le bookmaker). Chaque sélection est prise parmi les
marchés **réellement cotés** chez Unibet — double chance, « les deux équipes marquent »
oui/non, plus/moins de buts, résultat + buts, BTTS 1re mi-temps.

Sont toujours écartées les sélections que le modèle juge à moins de 55 %, et les cotes
inférieures à 1.20 (« plus de 0.5 but » à 1.02 est la sélection la plus probable de
n'importe quel match, et la moins intéressante à jouer). Le classement dépend ensuite du
modèle :

- `--poisson marche` : par meilleure espérance `cote × probabilité`, en écartant les
  écarts au marché supérieurs à +25 %, qui trahissent presque toujours une faiblesse du
  modèle (équipe mal identifiée, statistiques absentes) plutôt qu'une aubaine ;
- `--poisson forme` (défaut) : par probabilité décroissante, sans plafond de valeur —
  ce modèle ignorant les cotes, sa plus grosse valeur affichée est son plus gros écart
  d'estimation.

Chaque combiné affiche l'heure limite de validation, la probabilité estimée, la cote
cumulée, la cote équitable, la valeur théorique et le gain pour 10 EUR misés. Ces mêmes
chiffres sont repris dans le JSON, clé `combines`.

> Un combiné de 8 sélections à ~60 % chacune ne passe qu'environ **une fois sur 40** : le
> gain affiché est une projection mathématique, pas un gain « réalisable ». Le calcul
> suppose de plus les matchs indépendants, ce qu'ils ne sont jamais totalement, et le
> bookmaker dispose de plus de données que ce modèle.

### Marchés calculés

À partir de la matrice des scores exacts, le modèle donne la probabilité de chaque
marché courant : `1`, `N`, `2`, doubles chances `1N` / `12` / `N2`, « les deux équipes
marquent » oui/non, les combinés `1N et oui`, `12 et oui`, `N2 et oui`, `1N et non`…, les
seuils de buts `Plus de 2.5 buts` / `Moins de 2.5 buts` (de 0.5 à 4.5) et leurs
croisements `1N et plus de 2.5 buts`, `Les deux marquent : oui et plus de 2.5 buts`,
ainsi que « les deux marquent » sur la **1re mi-temps** (estimée avec 45 % des buts
attendus). Le rapport affiche pour chacun la **cote équitable** (celle en dessous de
laquelle le pari perd de l'argent si le modèle a raison) et la compare à la cote
proposée.

### Forebet : enregistrer la page à la main (Ctrl+S)

Forebet est derrière Cloudflare, qui bloque aussi bien la requête HTTP directe que
Chromium piloté par Playwright. **La seule méthode qui fonctionne** est d'enregistrer la
page depuis ton navigateur, chaque jour :

1. Ouvre <https://www.forebet.com/en/football-tips-and-predictions-for-today> dans ton
   navigateur habituel (Firefox ou Chrome).
2. **Ctrl+S**, type « Page Web, complète » (ou « HTML seul », les deux marchent).
3. **Renomme le fichier `Forebet.htm`** et place-le **à la racine du projet**, à côté de
   `analyse.cmd`. Le fichier téléchargé s'appelle par défaut
   `Football Predictions for Today _ Forebet.htm` : renommé, il est repris
   automatiquement, sans avoir à taper de chemin.
4. Lance `analyse.cmd` : il affiche « Forebet.htm trouve » et croise les pronostics avec
   les cotes Unibet du jour.

À refaire chaque jour : le fichier de la veille ne contient plus les matchs du jour, et
plus aucun d'eux n'est alors pariable.

Si tu préfères le garder ailleurs, indique son chemin (guillemets obligatoires s'il
contient des espaces) :

```powershell
python -m betbot --forebet-html "C:\Users\<toi>\Desktop\Football Predictions for Today _ Forebet.htm" --today --only-bettable
```

### Pages Forebet par marché (1X2, les deux marquent, +/-2.5, double chance, mi-temps)

Forebet publie une page par marché. Enregistre-les de la même façon (Ctrl+S) **dans le
dossier du projet, sans les renommer** : Bet.Bot ramasse tout seul les fichiers
`Predictions*.htm` du dossier courant (et de celui de `Bet.Bot.exe`), et affiche « Page
Forebet trouvee : ... » pour chacun. Rien d'autre à faire, que tu passes par
`analyse.cmd`, `python -m betbot` ou l'exécutable.

En ligne de commande, chaque fichier se passe à `--forebet-market-html`, option
répétable :

```powershell
python -m betbot --from-unibet --today `
  --forebet-market-html "Predictions 1X2 _ Today Forebet Football.htm" `
  --forebet-market-html "Predictions Both to score _ Today Forebet Football.htm" `
  --forebet-market-html "Predictions Under_Over 2.5 goals _ Today Forebet Football.htm" `
  --forebet-market-html "Predictions Double chance _ Today Forebet Football.htm" `
  --forebet-market-html "Predictions Half Time (HT) _ Today Forebet Football.htm"
```

Pages reconnues (le type est déduit du titre de la page, l'ordre des fichiers est libre) :

| Page Forebet | Marchés ajoutés au rapport |
| --- | --- |
| `predictions-1x2` | 1, N, 2 du temps réglementaire, score exact et moyenne de buts |
| `both-to-score` | Les deux marquent : oui / non |
| `under-over-25-goals` | Plus de 2.5 buts / Moins de 2.5 buts |
| `double-chance-predictions` | 1N, 12, N2 |
| `predictions-ht` | 1, N, 2 de la 1re mi-temps |

La page <https://www.forebet.com/en/football-tips-and-predictions-for-today/predictions-1x2>
est la plus utile des cinq avec `--from-unibet` : en partant du listing du bookmaker,
c'est la seule source du pronostic Forebet lui-même. Sans elle, la ligne « Forebet » du
tableau de comparaison reste vide et il ne reste que le modèle face au marché.

Ces probabilités apparaissent dans une section **Marchés (pages Forebet)** de chaque
match, à côté de celles du modèle et de la cote Unibet, et dans le JSON sous
`forebet.markets`. Forebet ne donnant qu'un pourcentage par rencontre, le marché
complémentaire est déduit (« oui » à 22 % quand « non » est à 78 %).

Deux limites à garder en tête : les pronostics des pages de mi-temps n'ont pas
d'équivalent dans le modèle (la colonne « proba modèle » reste vide, et ils ne servent
donc pas aux combinés), et un accord entre Forebet et le modèle ne valide rien — deux
estimations peuvent se tromper ensemble. Un désaccord marqué, en revanche, est un bon
signal de prudence.

Sinon, on se passe complètement de Forebet (Flashscore + Poisson + LLM) :

```powershell
python -m betbot --match "Lyon vs Rennes" --match "Getafe vs Athletic Bilbao"
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
| `betbot/sources/forebet.py` | scraping du listing Forebet (requests + BeautifulSoup) |
| `betbot/sources/flashscore.py` | recherche des équipes (API JSON) + derniers résultats via Playwright |
| `betbot/sources/bookmakers.py` | cotes Unibet (listing 1 N 2 + marchés de la page match), CSV manuel, appariement des noms d'équipes |
| `betbot/poisson.py` | buts attendus, 1X2, over 2.5, BTTS, score le plus probable |
| `betbot/llm.py` | client LM Studio + prompt système « analyste rigoureux » |
| `betbot/pipeline.py` | orchestration, dégradation propre si une source manque |
| `betbot/report.py` | rapport Markdown + export JSON |

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
