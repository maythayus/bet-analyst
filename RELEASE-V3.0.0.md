# Bet.Bot V3.0.0

Bet.Bot croise les pronostics Forebet, les statistiques Flashscore et les cotes Unibet, les passe dans un modèle de Poisson et fait commenter le résultat par un LLM local (LM Studio). Il produit un rapport Markdown et JSON par journée, envoyable par courriel ou publiable sur WordPress.

Cette version change le cœur du programme : la sélection des paris devient un **consensus arbitré par la cote**, les **matchs pièges** sont écartés, et **aucun combiné n'est proposé sous une chance sur trois**.

## Nouveautés

### Sélection des paris
- **Plancher à une chance sur trois** : le ticket principal est réduit aux sélections les plus probables jusqu'à repasser au-dessus de 33 % ; les combinés 6, 8 et le combiné maximum disparaissent du rapport dès que leur probabilité globale passe dessous. `--min-combo-prob` remplace ce plancher.
- **Combiné maximum** : toutes les sélections valides du jour, une par match, remplace l'ancien combiné 4 BTTS mixte.
- **Consensus Forebet + modèle** sur les marchés que Forebet publie (les deux marquent oui/non, doubles chances) : 60 % Forebet, 40 % modèle, tiré vers le bas quand ils divergent. En cas de désaccord franc, **la cote arbitre** : la source la plus proche du marché l'emporte, sinon le marché est écarté.
- **Seuil unique de 55 %** pour toute sélection, cote minimale 1.20, une sélection par match.
- **Détecteur de matchs pièges** : score fermé attendu (0-0, 1-0, 0-1), défenses trop solides ou trop friables, face-à-face fermés, forte part de nuls, classement serré. Un « les deux marquent : oui » exige deux attaques à plus de 1,2 but/match.
- Les rencontres analysées sont choisies par **compétition connue et buts attendus**, et Flashscore n'est interrogé que pour les matchs **cotés chez Unibet**.

### Modèle
- **Forme sur vingt matchs pondérés**, corrigée du niveau des adversaires.
- Modèle de Poisson **au choix** : `forme` (défaut, probabilités tranchées) ou `marche` (calé sur les cotes dévigorisées, correction Dixon-Coles).
- Mise conseillée en **quart de Kelly**, plafonnée à 5 % du capital.
- **Suivi** (`--bilan`) : chaque pronostic est enregistré et confronté aux scores pour mesurer qui a raison entre Forebet, le modèle et le consensus.

### Données
- **Heure de coup d'envoi, compétition et pays** repris du calendrier Flashscore, plus du bookmaker.
- Flashscore : abréviations du bookmaker dépliées automatiquement (`Utd` → United, `SL` → sigles), homonymes essayés en cascade, alias des noms francisés et transcriptions slaves.
- Forebet : les cinq pages (1X2, les deux marquent, +/-2.5, double chance, mi-temps) sont lues en français comme en anglais ; toute page enregistrée depuis le navigateur est ramassée quel que soit son nom ; enregistrement des pages du jour avec vérification Cloudflare cliquée à la main.
- Cotes Unibet : 1X2, doubles chances, BTTS, seuils de buts et marchés combinés lus sur la page de chaque match ; journée complète avec `--today`.

### LLM (LM Studio)
- **Qwen3-14B Q4_K_M** devient le modèle par défaut (12 Go de VRAM, contexte 16 384, réponse 4 096 tokens). Le mode réflexion est coupé par défaut (`LMSTUDIO_THINKING=1` pour le réactiver), délai porté à 600 s.
- **Quatre questions fermées**, puis un **second appel « avocat du diable »** qui cherche ce qui contredit la première analyse.
- Le prompt est taillé pour tenir dans la fenêtre de contexte au lieu de faire rejeter le match.
- Le LLM **commente, il ne décide pas** : aucune probabilité ni aucun ticket ne dépend de lui.

### Rapport et diffusion
- Envoi du rapport par **courriel** et publication en **article WordPress** (brouillon), avec mots de passe d'application dans `.env`.
- Le rapport s'ouvre dans l'éditeur par défaut ; `--print` l'affiche dans la console.
- **Bet.Bot.exe** : build PyInstaller (`build-exe.cmd`), rapports à côté de l'exécutable, installation de Chromium.
- Lanceurs `analyse.cmd` et `analyse-quotidienne.cmd`.

## Changements de comportement à connaître
- Les combinés 6 et 8 n'apparaîtront quasiment plus : à 55 % par sélection, seuls les tickets de 1 ou 2 sélections tiennent au-dessus d'une chance sur trois. C'est voulu.
- Le rapport peut conclure « Aucune sélection à espérance positive aujourd'hui » : ne pas jouer est alors la recommandation.
- Le modèle par défaut LM Studio n'est plus DeepSeek-R1-Distill-Llama-8B : vérifier qu'aucun `LMSTUDIO_MODEL=deepseek…` ne traîne dans `.env`.

## Mise à jour
```powershell
git pull
python -m pip install -r requirements.txt
python -m betbot --demo --no-llm --no-open   # test hors ligne
```

Lint `ruff` propre, 166 tests unitaires.

## Avertissement
Les probabilités d'un combiné supposent les matchs indépendants et le modèle juste ; elles ne sont pas calibrées tant que le suivi n'a pas une centaine de pronostics réglés. Jouer comporte un risque de perte : ne misez jamais plus que ce que vous pouvez perdre.
