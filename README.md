<img width="248" height="674" alt="Capture d’écran 2026-08-02 155300" src="https://github.com/user-attachments/assets/e54aac81-aa42-4a25-ad9e-a507f2095b73" />
<img width="263" height="314" alt="Capture d’écran 2026-08-02 155320" src="https://github.com/user-attachments/assets/f261dcf5-303a-413e-ac19-95f37dba66a6" />
﻿# Bet.Bot

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
   avis extérieur, pas comme une vérité. Les cinq pages enregistrées sont
   <https://www.forebet.com/fr/pronostics-pour-aujourd-hui> et ses marchés
   `/chaque-equipe-marque`, `/moins-plus-2-5-de-buts`, `/chance-double`, `/mi-temps`.
2. **Flashscore** — les statistiques brutes : vingt derniers matchs de chaque équipe
   (adversaire, lieu et score de chacun), confrontations directes, classement de la compétition
   (position, points, buts encaissés sur la saison), et l'heure, la compétition et le
   pays de la rencontre.
3. **Modèle de Poisson** — un calcul maison qui déduit la probabilité de chaque marché
   d'une matrice de scores exacts (voir [Le modèle](#le-modèle-poisson)).
4. **Unibet** — les cotes réellement proposées : 1 N 2 du listing, puis « les deux
   équipes marquent », doubles chances et combinés lus sur la page de chaque match.

Le tout est ensuite résumé par un **LLM local** (par défaut
`qwen/qwen3-14b` dans LM Studio ; `--model` pour en changer), dont le rôle
est de commenter les désaccords entre sources — pas d'inventer un pronostic.

Sont analysés **les matchs pariables, et eux seuls** : dès que les cotes Unibet sont
récupérées, une rencontre du listing Forebet qu'Unibet ne cote pas n'est pas envoyée à
Flashscore — elle n'entrerait dans aucun ticket. `--from-unibet` part directement de la
grille des cotes ; `--match` reste analysé même sans cote (`--only-bettable` pour
l'écarter aussi).

## Le modèle Poisson

Le modèle construit une **matrice des scores exacts** (0-0 à 8-8) à partir de deux
nombres : les buts attendus de chaque équipe. Toutes les probabilités affichées — 1 N 2,
doubles chances, plus/moins de buts, « les deux marquent », marchés combinés — sont des
sommes de cases de cette matrice, donc cohérentes entre elles par construction.

Deux estimations de ces buts attendus sont disponibles, au choix (`--poisson`) :

| Modèle | Ce qu'il fait | Ce qu'il donne |
| --- | --- | --- |
| `forme` (défaut) | deux lois de Poisson nourries par les vingt derniers matchs, les cotes ne servent qu'à mesurer l'écart | probabilités tranchées, beaucoup de valeur affichée, une partie étant de l'erreur d'estimation |
| `marche` | matrice calée sur les cotes dont la marge a été retirée, forme en ajustement borné | reproduit le marché à ~2 points près, donc presque jamais de pari à espérance positive |

Le modèle `forme` est celui des premières versions : c'est lui qui produit les combinés
à forte cote. Le rapport affiche pour chaque match son **écart maximal au marché** ;
au-delà d'une dizaine de points, cet écart mesure d'abord l'incertitude du modèle, pas
une occasion. `--poisson marche` donne la lecture prudente du même match.

Corrections apportées à un Poisson d'école :

- **Dixon-Coles**, sur les deux modèles : deux lois de Poisson indépendantes
  sous-estiment les scores serrés (0-0, 1-0, 0-1, 1-1). Le modèle leur rend leur poids,
  ce qui corrige surtout le « les deux marquent : non », auparavant sous-estimé de près
  de 16 points.
- **Calage sur les cotes**, sur `marche` : quand un groupe d'issues exhaustif est coté
  (1 N 2, une ligne de buts, BTTS oui/non), la marge du bookmaker est retirée puis les
  buts attendus sont ajustés pour reproduire ces probabilités. La forme récente ne sert
  plus qu'à un écart borné (25 % de poids, 12 % d'amplitude maximale).

### Comment la forme est mesurée

Une moyenne de buts sur cinq matchs était le principal défaut du modèle : elle comptait
à l'identique un match d'il y a une semaine et un d'il y a trois mois, une réception du
dernier et un déplacement chez le leader, un match à domicile et un à l'extérieur.
Quatre corrections sont appliquées (`betbot/strength.py`), dans cet ordre :

| Correction | Ce qu'elle change |
| --- | --- |
| **Vingt matchs, pondérés par l'ancienneté** | chaque match plus vieux pèse 0.93 fois le précédent : de la matière sans que la saison passée dicte la forme du jour |
| **Domicile / extérieur** | les matchs joués dans le même contexte que la rencontre à venir comptent 1.6 fois plus |
| **Force des adversaires** | les buts marqués sont divisés par la perméabilité de la défense affrontée, les encaissés par le tranchant de l'attaque affrontée, d'après le classement de la saison (facteur borné entre 0.65 et 1.55) |
| **Ancrage sur la saison** | la mesure est rapprochée des buts de la saison entière, d'autant plus fort que l'échantillon est mince (prior de 6 matchs) |

Aucune de ces corrections n'invente d'information : elles rendent les probabilités plus
**justes**, pas plus élevées. Un match indécis le reste, et un adversaire absent du
classement (coupe, autre division) n'est pas corrigé au hasard.

Chaque match indique donc l'origine de son estimation :

| Source affichée | Signification |
| --- | --- |
| `forme recente` | modèle par défaut : vingt matchs de forme corrigée, sans référence aux cotes |
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

### Forebet et le modèle réunis, à partir de 55 %

Sur les cinq marchés que Forebet publie lui-même — « les deux marquent » oui et non, et
les trois doubles chances `1N`, `N2`, `12` — les deux estimations sont **réunies en une
seule probabilité** (`betbot/consensus.py`) : Forebet pèse 60 %, le modèle 40 %. Deux
sources indépendantes qui se rejoignent valent mieux que chacune isolément, et le seuil
de sélection est de **55 %** sur ce consensus — le même que partout ailleurs.

À une condition, qui est tout l'intérêt du dispositif : les deux doivent se rejoindre.
Le désaccord est traité en trois temps, du plus informé au plus prudent.

**1. La tolérance dépend de l'endroit où tombe l'écart.** Dix points entre 45 % et 55 %
font basculer la décision — l'un dit non, l'autre dit oui ; les mêmes dix points entre
80 % et 90 % disent la même chose. La tolérance vaut donc environ **14 points autour de
50 %** et jusqu'à **28 points aux extrêmes**, au lieu d'un seuil unique de 20 points
appliqué partout.

**2. La confiance se dégrade au lieu de casser net.** Dans la tolérance, la moyenne
pondérée est tirée vers la **plus basse** des deux estimations, à proportion de l'écart :
à écart nul elle est intacte, à la limite il ne reste que la valeur prudente. Le rapport
affiche cette confiance en pourcentage. Plus aucun effet de seuil où 19 points passent et
21 points sautent.

**3. Au-delà, la cote arbitre.** Le bookmaker est l'acteur le mieux informé des trois :
la source la plus proche de la **probabilité implicite de sa cote** (marge retirée grâce
à l'issue complémentaire) l'emporte, à condition d'en être plus proche de 5 points au
moins. Sa probabilité brute est retenue telle quelle, avec une confiance nulle et la
mention `cote arbitre`. Si la cote se situe entre les deux, elle ne désigne personne et
**le marché est écarté des combinés**, comme avant.

Le rapport affiche côte à côte la probabilité Forebet, celle du modèle, la probabilité
implicite de la cote, la valeur retenue et la confiance ; le JSON reprend le détail
(`consensus` par marché : `forebet`, `modele`, `cote_implicite`, `retenu`, `ecart`,
`tolerance`, `source`, `confiance`, `desaccord`). Les valeurs brutes ne sont jamais
remplacées : masquer l'écart donnerait une assurance que ni l'une ni l'autre des sources
n'a.

Quand Forebet ne publie pas un marché (mi-temps, seuils de buts, scores), le modèle
décide seul, au même seuil de 55 %.

### Suivi des résultats : mesurer qui a raison

Les 60/40 et les tolérances ci-dessus sont des **suppositions**, et rien ne permettait de
les vérifier. Chaque analyse enregistre désormais ses pronostics dans `out/suivi.jsonl`
(un par ligne : les trois estimations, la valeur retenue, la cote, l'heure du match), et
`--bilan` les confronte aux scores réels lus sur Flashscore :

```powershell
python -m betbot --bilan
```

Le bilan mesure séparément Forebet, le modèle, la valeur retenue et la cote : la
probabilité moyenne **annoncée** face à la part réellement **réalisée** (positif = la
source se surestime), et le **score de Brier**, qui récompense à la fois la justesse et la
franchise — annoncer 50 % partout ne rapporte rien. Un match introuvable ou reporté reste
simplement non réglé, il sera repris au bilan suivant ; les marchés de mi-temps ne sont
pas jugés, le score final ne suffit pas à les trancher.

En dessous de **100 pronostics réglés**, ces écarts ne se distinguent pas du hasard et le
bilan le dit explicitement : c'est une mesure, pas encore une conclusion, et les poids du
consensus ne changeront qu'une fois l'échantillon suffisant. `--no-suivi` désactive
l'enregistrement.

### Ce que vaut le seuil choisi

Les seuils se multiplient : plus il est bas, plus il y a de matchs retenus, et moins le
combiné a de chances de passer.

| Seuil par sélection | Combi 4 | Combi 6 | Combi 8 |
| --- | --- | --- | --- |
| 55 % (actuel) | 9,2 % (1 fois sur 11) | 2,8 % (1 sur 36) | 0,8 % (1 sur 119) |
| 60 % | 13 % (1 sur 8) | 4,7 % (1 sur 21) | 1,7 % (1 sur 60) |
| 65 % | 18 % (1 sur 6) | 7,5 % (1 sur 13) | 3,2 % (1 sur 31) |
| 70 % | 24 % (1 sur 4) | 12 % (1 sur 8) | 5,8 % (1 sur 17) |

Un seuil élevé ne rend pas pour autant un ticket gagnant : il le rend plus probable et
moins payant. Ce qui décide sur la durée est l'écart entre la probabilité et la cote,
pas la probabilité seule — un 75 % coté 1.25 est un mauvais pari, un 60 % coté 2.00 en
est un bon, à condition que le modèle ait raison. Et un seuil bas expose à l'erreur du
modèle : sa marge étant de plusieurs points, une sélection annoncée à 55 % peut être
réellement à 50 %. Les seuils vivent dans `betbot/combo.py`
(`MIN_LEG_PROBABILITY`, `FOREBET_MIN_PROBABILITY`).

### Matchs pièges

Une probabilité élevée ne dit rien de la **fragilité** d'une rencontre. Bet.Bot écarte
des combinés les sélections que le contexte contredit, et explique pourquoi dans une
section « Matchs pièges » sous chaque match :

| Signal | Ce qu'il écarte |
| --- | --- |
| Score pronostiqué par Forebet à 0-0, 1-0 ou 0-1 | « les deux marquent : oui », `12` |
| Deux défenses à moins de 1.0 but encaissé par match (classement de la saison) | « les deux marquent : oui » |
| Une attaque à moins de 1.2 but marqué par match | « les deux marquent : oui » |
| Confrontations directes à moins de 2 buts par match | « les deux marquent : oui » |
| Classement serré (3 places ou moins) | « les deux marquent : oui », `12` |
| Une défense à 1.7 but encaissé et plus | « les deux marquent : non » |
| Confrontations directes à plus de 3 buts | « les deux marquent : non » |
| Beaucoup de nuls (30 % et plus, au classement ou en face à face) | `12` |
| Adversaire mieux classé de 8 places et plus | `1N` ou `N2` du moins bien classé |
| Défense friable (1.7+) face à une attaque à 1.6 but et plus | `1N` ou `N2` |

Ce sont des **heuristiques**, pas une mesure : « tension » et « solidité défensive » sont
approchées par des chiffres (position, buts encaissés, part de nuls, buts en face à
face), rien de plus. Un match signalé n'annonce pas l'issue contraire, il dit que la
sélection est moins sûre que sa probabilité ne le laisse croire. Et quand il ne reste pas
assez de sélections valables, **aucun ticket n'est produit** : c'est un résultat
acceptable, pas une panne.

### Ce que ça ne prouve pas

Une sélection affichée n'est **jamais une validation ni une certitude** : c'est un
pronostic, issu de sites de prédiction et d'un modèle statistique qui se trompent tous
les deux régulièrement.

- Une probabilité de 80 % veut dire que l'issue **ne se produit pas une fois sur cinq** ;
  se tromper n'est pas une anomalie, c'est prévu par le calcul.
- Une « valeur » positive ne signifie rien en soi : elle suppose que mon modèle, bâti
  sur vingt matchs de forme et un classement, soit mieux calibré qu'Unibet, qui dispose
  de bien plus de données. C'est rarement le cas.
- Un consensus Forebet + modèle reste deux estimations, pas une vérification : deux
  sources peuvent se tromper ensemble, et l'accord entre elles ne rend pas un résultat
  garanti. Quand rien ne passe les seuils, **ne pas jouer** est la conclusion prévue.
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

Modèle de référence : **Qwen3-14B** (14 milliards de paramètres), chargé dans LM Studio
sous l'identifiant `qwen/qwen3-14b`.

| Quantification | Taille du fichier | VRAM à prévoir | Commentaire |
| --- | --- | --- | --- |
| `Q4_K_M` | ~9 Go | **12 Go** | le bon compromis, celui utilisé ici |
| `Q3_K_M` | ~7 Go | 10 Go | si la VRAM manque ; qualité en retrait |
| `Q5_K_M` | ~10.5 Go | 14 Go | ne tient plus entièrement sur 12 Go |

La VRAM indiquée inclut le contexte : à 16384 tokens, le cache occupe environ 1.5 Go
en plus du fichier. Sur une **RTX 5070 (12 Go)**, `Q4_K_M` avec 16384 tokens tient,
GPU offload au maximum. Qwen3 réfléchit dans un bloc `<think>` avant de répondre (retiré
du rapport) : ces tokens comptent dans la réponse, d'où un `max_tokens` à 4096.

| Composant | Minimum | Confortable |
| --- | --- | --- |
| GPU | 12 Go de VRAM (RTX 4070, 5070) pour Qwen3-14B `Q4_K_M` | 16 Go et plus |
| RAM système | 16 Go | 32 Go |
| Disque | 15 Go libres (modèle + Chromium + caches) | 25 Go |
| Connexion | indispensable (Unibet, Flashscore) | — |

**Sans GPU suffisant**, deux voies :

- Laisser LM Studio décharger une partie des couches sur le CPU : ça fonctionne, mais
  comptez plusieurs minutes par match plutôt que quelques secondes.
- Prendre un Qwen3 plus léger, `qwen/qwen3-8b` en `Q4_K_M` (~5 Go, 8 Go de VRAM) ou
  `qwen/qwen3-4b` (~2.5 Go), avec `python -m betbot --model <identifiant>` : même
  format de réponse, commentaire moins fin.

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

1. Télécharge **Qwen3-14B** en `Q4_K_M` (~9 Go, tient dans les 12 Go de la 5070). Si
   LM Studio l'affiche sous un autre identifiant que `qwen/qwen3-14b`, passe-le par
   `--model` ou `LMSTUDIO_MODEL`.
2. Charge-le avec un contexte de 16384 tokens et **GPU offload au maximum**.
3. Onglet **Developer** → **Start Server** (par défaut `http://localhost:1234`).

Un serveur local n'exige aucune clé. S'il en demande une (serveur distant, LM Studio
exposé sur le réseau), passe-la par `--api-key` ou par la variable `LMSTUDIO_API_KEY` :
ne l'écris jamais dans le dépôt.

Le point 2 n'est pas un détail : LM Studio charge par défaut un contexte bien plus court
que ce que le modèle accepte ; trop court, le serveur refuse la rencontre (`request
exceeds the available context size`) et le rapport sort **sans commentaire pour ce
match** — Bet.Bot le signale désormais dans le journal.
Bet.Bot n'envoie plus au modèle que ce dont le commentaire a besoin — le classement de
la compétition et le détail des vingt matchs restent côté modèle de Poisson, qui les
utilise pleinement — mais un contexte de 16384 reste la marge saine.

```powershell
$env:LMSTUDIO_API_KEY = "<ta-cle>"
$env:LMSTUDIO_BASE_URL = "http://<hote>:1234/v1"   # si le serveur n'est pas local
```

### Deux appels par match : questions fermées, puis avocat du diable

Le LLM ne commente plus librement. Son analyse se termine par **quatre réponses
fermées** — décision (`jouer`, `eviter`, `ne pas jouer`), marché retenu, source la moins
crédible (`forebet`, `modele`, `marche`, `aucune`), risque principal — et une confiance
sur 10, affichées dans un tableau « Réponses fermées » et dans le JSON (`verdict`). Une
réponse hors des choix proposés est ignorée plutôt qu'affichée.

Un **second appel** lui remet ensuite les mêmes données et sa propre analyse, avec pour
seule consigne de chercher ce qui la contredit. Sa contre-analyse est jointe au rapport,
le verdict ressort `maintenu` ou `renverse`, et la confiance révisée remplace la
première. C'est la seule façon utile de « donner du temps » au LLM : deux appels courts
plutôt qu'un long. Rien de tout ça ne touche une probabilité — le LLM n'en calcule pas,
et les combinés ne le consultent pas.

Le second appel double le temps LLM par match ; `--no-contre-analyse` (ou
`LMSTUDIO_SECOND_PASS=0`) le désactive.

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

### Enregistrer les pages Forebet automatiquement

`--save-forebet` remplace le Ctrl+S quotidien : un navigateur ouvre les cinq pages du
jour, à quelques secondes d'intervalle, et enregistre le HTML dans le dossier du projet,
que l'analyse ramasse ensuite toute seule.

```powershell
python -m betbot --save-forebet --from-unibet --today   # enregistre puis analyse
python -m betbot --save-forebet-only                    # enregistre seulement
```

Le navigateur utilisé est le **Chrome installé sur la machine** (Chromium de Playwright à
défaut), avec un profil dédié conservé dans `.cache\chrome-forebet` : les cookies
Cloudflare restent valides d'un jour sur l'autre.

**La première exécution se fait à la main, fenêtre visible** : Forebet peut afficher la
vérification Cloudflare, à valider d'un clic ; le script attend 90 secondes. Les jours
suivants, le profil passe généralement sans rien demander, et
`--save-forebet-headless` permet alors de masquer la fenêtre. En mode masqué, une
vérification qui réapparaît arrête la sauvegarde avec le message correspondant plutôt que
d'enregistrer la page d'attente à la place des pronostics.

Aucune protection n'est contournée : ni solveur de challenge, ni proxy tournant, ni faux
navigateur. Ce sont des violations des conditions du site, elles cassent à chaque mise à
jour de Cloudflare et finissent par faire bannir ton adresse IP. Si la vérification
persiste, ouvre Forebet dans ton navigateur habituel, valide-la, et relance — ou reste au
Ctrl+S manuel, qui fonctionne toujours.

### Analyse quotidienne sans surveillance

`analyse-quotidienne.cmd` enchaîne : enregistrement des pages Forebet, analyse de la
journée, diffusion (courriel et WordPress selon le `.env`), sans ouvrir de fenêtre de
texte à la fin. Pour le programmer chaque matin à 8 h :

```powershell
schtasks /create /tn "Bet.Bot" /tr "C:\Users\MI_K4\OneDrive\Documents\Bureau\bet-bot\analyse-quotidienne.cmd" /sc daily /st 08:00
```

Lance-le une première fois à la main : c'est le moment de valider la vérification
Cloudflare, une fois pour toutes.

### Partager le rapport : courriel et WordPress

Les identifiants ne se tapent pas en ligne de commande : copie `.env.exemple` en `.env`
à la racine du projet et remplis-le. Ce fichier est ignoré par Git.

```powershell
copy .env.exemple .env
notepad .env
```

**Courriel.** Le rapport part en HTML dans le corps du message, avec le `.md` et le
`.json` en pièces jointes.

```powershell
python -m betbot --from-unibet --today --mail-to kaelmi@gmail.com
python -m betbot --from-unibet --today          # BETBOT_MAIL_TO du .env suffit
python -m betbot --from-unibet --today --no-mail # ne rien envoyer ce coup-ci
```

Gmail refuse le mot de passe du compte : il faut un **mot de passe d'application**
(https://myaccount.google.com/apppasswords), à mettre dans `BETBOT_MAIL_PASSWORD`. Un
autre fournisseur se configure avec `BETBOT_MAIL_HOST` / `BETBOT_MAIL_PORT` (587 pour
STARTTLS, 465 pour SSL).

**WordPress.** Le Markdown est converti en HTML et publié en article via l'API REST.

```powershell
python -m betbot --from-unibet --today --wordpress
python -m betbot --from-unibet --today --wordpress --wordpress-status publish
```

L'article part en **brouillon** par défaut : un pronostic se relit avant d'être publié.
`BETBOT_WP_PASSWORD` attend un **mot de passe d'application** WordPress (Utilisateurs →
Profil → Mots de passe d'application), pas le mot de passe du compte, et le site doit
être en HTTPS. `BETBOT_WP_CATEGORIES` accepte des identifiants de catégories séparés par
des virgules.

Une diffusion qui échoue n'interrompt pas l'analyse : le rapport reste écrit dans `out/`
et l'erreur est affichée.

**Publier sur un site public** implique des obligations légales en France (mention ANJ,
interdiction aux mineurs) : garde l'avertissement du rapport en tête d'article.

### Dossier réseau

`--output` accepte un partage Windows, sans autre réglage :

```powershell
python -m betbot --from-unibet --today --output \\NARCISSOT-M\out
```

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

### Quelles rencontres passent quand il y en a trop

`--matches N` ne prend plus les N premières du listing : les rencontres sont d'abord
classées (`betbot/priority.py`), puis coupées au plafond.

| Critère | Ordre |
| --- | --- |
| **Compétition** | grandes divisions et Ligue des champions d'abord, puis les autres compétitions connues (C3, C4, Eredivisie, Championship, coupes nationales…), puis le reste |
| **Buts attendus** | à compétition équivalente, la moyenne de buts publiée par Forebet décide ; sans elle, sa probabilité de plus de 2.5 buts |

Le premier critère est la seule façon honnête d'approcher les « équipes connues » : un
club l'est parce qu'il joue dans une grande division, et ce sont aussi les compétitions
où Flashscore publie un classement complet, donc celles où le modèle est le mieux nourri.
Ce classement décide de **ce qui est analysé**, pas de ce qui est joué : les seuils, les
matchs pièges et les cotes tranchent ensuite comme avant. À égalité, l'ordre de la source
est conservé, donc deux analyses de la même journée retiennent les mêmes rencontres.

Les noms d'équipes du bookmaker sont abrégés (« Mac.Tel Aviv », « SherifTiraspol »,
« Universit Cluj ») : la recherche Flashscore les déplie, écarte les joueurs, les
équipes féminines, les réserves et les U19, et se sert du pays de la compétition pour
départager les homonymes (le Libertad d'Équateur et celui du Paraguay). Quand le nom
retenu diffère de celui du bookmaker, la ligne `Flashscore : 'X' identifie comme Y` le
signale. Quand la page du club retenu n'affiche aucun match joué (homonyme amateur, club
inactif), les candidats suivants sont essayés avant d'abandonner ; si aucun ne convient,
le match est analysé sans statistiques et un avertissement le dit.

**Heure, compétition et pays** viennent du calendrier Flashscore de l'équipe recevante,
et non du bookmaker : l'en-tête de la page donne le pays et le nom complet de la
compétition (« Angleterre | Premier League ») là où Forebet n'affiche qu'un code (`ECL`,
`Kz2`). Ils s'affichent sous le titre de chaque match et sont repris dans le JSON
(`country`, `competition`, `kickoff`). Quand la rencontre n'y figure pas, l'heure et la
compétition du bookmaker sont conservées plutôt qu'inventées.

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

### Combinés 6, 8 et maximum

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

Vient ensuite le **combiné maximum** : toutes les sélections valides du jour, une par
match, sans limite de taille — tant qu'une rencontre offre une sélection au-dessus de
55 %, cotée au moins 1.20 et non signalée comme piège, elle entre. Il faut au moins deux
sélections ; et quand le jour en offre exactement 6 ou 8, il n'est pas répété, le combiné
fixe correspondant étant déjà le maximum.

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

1. Ouvre <https://www.forebet.com/fr/pronostics-pour-aujourd-hui> dans ton
   navigateur habituel (Firefox ou Chrome).
2. **Ctrl+S**, type « Page Web, complète » (ou « HTML seul », les deux marchent).
3. **Renomme le fichier `Forebet.htm`** et place-le **à la racine du projet**, à côté de
   `analyse.cmd`. Renommé, il est repris automatiquement, sans avoir à taper de chemin.
4. Lance `analyse.cmd` : il affiche « Forebet.htm trouve » et croise les pronostics avec
   les cotes Unibet du jour.

À refaire chaque jour : le fichier de la veille ne contient plus les matchs du jour, et
plus aucun d'eux n'est alors pariable.

Si tu préfères le garder ailleurs, indique son chemin (guillemets obligatoires s'il
contient des espaces) :

```powershell
python -m betbot --forebet-html "C:\Users\<toi>\Desktop\Pronostics de football pour aujourd'hui _ Forebet.htm" --today --only-bettable
```

### Pages Forebet par marché (1X2, les deux marquent, +/-2.5, double chance, mi-temps)

Forebet publie une page par marché. Enregistre-les de la même façon (Ctrl+S) **dans le
dossier du projet, sans les renommer** : Bet.Bot ramasse tout seul
**tout fichier `.htm`/`.html` dont le nom contient « Forebet »** dans le dossier courant
et dans celui de `Bet.Bot.exe`, et affiche « Page Forebet trouvee : ... » pour chacun. Peu
importe donc que le navigateur nomme le fichier d'après le marché
(`Mi-temps _ Forebet Pronostics pour aujourd'hui.htm`) ou d'après le site
(`Pronostics Mi-temps _ Forebet Football.htm`) : le marché est reconnu au **titre de la
page**, pas au nom du fichier, et une ligne `... : page half time` le confirme dans les
logs. Un HTML étranger ramassé par erreur est signalé et sauté, sans priver l'analyse des
autres marchés. Rien d'autre à faire, que tu passes par `analyse.cmd`, `python -m betbot`
ou l'exécutable.

En ligne de commande, chaque fichier se passe à `--forebet-market-html`, option
répétable :

```powershell
python -m betbot --from-unibet --today `
  --forebet-market-html "Pronostics 1X2 _ Forebet Football.htm" `
  --forebet-market-html "Pronostics Chaque equipe marque _ Forebet Football.htm" `
  --forebet-market-html "Pronostics Moins-Plus 2.5 de buts _ Forebet Football.htm" `
  --forebet-market-html "Pronostics Chance double _ Forebet Football.htm" `
  --forebet-market-html "Pronostics Mi-temps _ Forebet Football.htm"
```

Pages reconnues (le type est déduit du titre de la page, l'ordre des fichiers est libre ;
les titres anglais des anciennes pages `/en/` sont lus aussi) :

| Page Forebet | Marchés ajoutés au rapport |
| --- | --- |
| <https://www.forebet.com/fr/pronostics-pour-aujourd-hui> | 1, N, 2 du temps réglementaire, score exact et moyenne de buts |
| <https://www.forebet.com/fr/pronostics-pour-aujourd-hui/chaque-equipe-marque> | Les deux marquent : oui / non |
| <https://www.forebet.com/fr/pronostics-pour-aujourd-hui/moins-plus-2-5-de-buts> | Plus de 2.5 buts / Moins de 2.5 buts |
| <https://www.forebet.com/fr/pronostics-pour-aujourd-hui/chance-double> | 1N, 12, N2 |
| <https://www.forebet.com/fr/pronostics-pour-aujourd-hui/mi-temps> | 1, N, 2 de la 1re mi-temps |

La page <https://www.forebet.com/fr/pronostics-pour-aujourd-hui>
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
| `LMSTUDIO_MODEL` | `qwen/qwen3-14b` | modèle chargé |
| `LMSTUDIO_MAX_TOKENS` | `4096` | longueur maximale de la réponse, bloc `<think>` compris |
| `LMSTUDIO_TEMPERATURE` | `0` | déterminisme (à laisser à 0) |
| `LMSTUDIO_SECOND_PASS` | `1` | `0` pour supprimer le second appel « avocat du diable » |
| `LMSTUDIO_THINKING` | `0` | `1` pour réactiver le bloc `<think>` de Qwen3 (beaucoup plus lent, coupé par `/no_think` sinon) |
| `LMSTUDIO_TIMEOUT` | `600` | secondes d'attente par appel avant « Read timed out » |
| `MAX_MATCHES` | `10` | nombre de matchs |
| `FLASHSCORE_HEADLESS` | `1` | `0` pour voir le navigateur |
| `FOREBET_URL` | page « predictions for today » | listing à scraper |
| `BETBOT_MAIL_HOST` / `BETBOT_MAIL_PORT` | `smtp.gmail.com` / `587` | serveur SMTP |
| `BETBOT_MAIL_USER` / `BETBOT_MAIL_PASSWORD` | — | compte d'envoi et mot de passe d'application |
| `BETBOT_MAIL_TO` | — | destinataires, séparés par des virgules |
| `BETBOT_WP_SITE` | — | racine du site WordPress (HTTPS) |
| `BETBOT_WP_USER` / `BETBOT_WP_PASSWORD` | — | identifiant et mot de passe d'application WordPress |
| `BETBOT_WP_STATUS` | `draft` | `draft`, `publish` ou `private` |

Ces variables se posent une fois pour toutes dans un fichier `.env` à la racine du
projet (voir `.env.exemple`), lu au démarrage et ignoré par Git.

## Architecture

| Fichier | Rôle |
| --- | --- |
| `betbot/sources/forebet.py` | scraping du listing Forebet (requests + BeautifulSoup) |
| `betbot/sources/flashscore.py` | recherche des équipes (API JSON) + derniers résultats via Playwright |
| `betbot/sources/bookmakers.py` | cotes Unibet (listing 1 N 2 + marchés de la page match), CSV manuel, appariement des noms d'équipes |
| `betbot/poisson.py` | buts attendus, 1X2, over 2.5, BTTS, score le plus probable |
| `betbot/llm.py` | client LM Studio + prompt système « analyste rigoureux » |
| `betbot/share.py` | conversion du rapport en HTML, envoi SMTP, publication WordPress |
| `betbot/sources/forebet_pages.py` | enregistrement des pages Forebet du jour avec un navigateur |
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
