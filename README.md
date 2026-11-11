# Robot d'alerte Buyee

En continu (une vérification toutes les 3 minutes), ce robot cherche les nouvelles annonces Yahoo Auctions (avec PayPay Flea Market) et Mercari
(les deux sites que Buyee revend) pour tes mots-clés, et t'envoie sur Discord
uniquement celles qu'il n'a encore jamais vues, avec le lien Buyee direct.

Il tourne gratuitement sur GitHub (GitHub Actions) : ton ordinateur peut rester éteint.

## Pourquoi Discord et pas Instagram ?

Instagram n'offre aucun moyen officiel pour qu'un robot t'envoie des messages privés.
Les astuces non officielles demandent ton mot de passe et peuvent faire bloquer ton compte.
Discord, lui, a un « webhook » : un simple lien secret qui permet au robot de poster
dans un salon. Avec l'appli Discord sur ton téléphone, tu reçois une notification
comme pour un message Insta.

## Installation (environ 10 minutes)

### 1. Discord : créer le lien d'alerte
1. Installe Discord (téléphone + ordinateur) et connecte-toi.
2. À gauche, clique sur **+** → **Créer le mien** → **Pour moi et mes amis**, nomme le serveur « Alertes Buyee ».
3. Fais un clic droit sur le salon **#général** → **Modifier le salon** → **Intégrations** → **Webhooks** → **Nouveau webhook**.
4. Clique sur **Copier l'URL du webhook**. Garde ce lien secret (n'importe qui l'ayant peut écrire dans ton salon).

### 2. GitHub : héberger le robot
1. Crée un compte sur https://github.com si tu n'en as pas.
2. Crée un dépôt **public** nommé `buyee-robot-annonce` (bouton **New repository**). Public = minutes illimitées et gratuites ; ton lien Discord reste caché car il est rangé dans un « secret ».
3. Mets-y les fichiers de ce dossier (`robot.py`, `config.json`, `requirements.txt`,
   `README.md` et `.github/workflows/robot.yml`).
4. Dans le dépôt : **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
   - Name : `DISCORD_WEBHOOK_URL`
   - Secret : colle le lien du webhook Discord.
5. Onglet **Actions** → **Robot Buyee** → **Run workflow** pour le premier lancement.

Au premier lancement, tu reçois sur Discord « 🤖 Robot Buyee activé ! » : il a mémorisé
les annonces déjà en ligne. Ensuite il ne t'envoie que les nouvelles.

## Changer les mots-clés

Ouvre `config.json` sur GitHub, clique sur le crayon ✏️, modifie les listes `mots_cles` des salons, puis **Commit changes**.
Tu peux aussi désactiver Mercari en mettant `"surveiller_mercari": false`.

## Bon à savoir
- GitHub peut décaler un passage de quelques minutes quand ses serveurs sont chargés.
- Si les sites ne répondent plus pendant 3 h, le robot t'envoie un message ⚠️ sur Discord.
- Le robot enregistre sa mémoire (`seen.json`) dans le dépôt à chaque passage, c'est normal.
- Pour arrêter le robot : onglet **Actions** → **Robot Buyee** → **⋯** → **Disable workflow**.

## Un salon Discord par type d'annonce

Chaque salon a ses propres mots-clés de recherche (dans `config.json`, rubrique `salons`) :

| Salon | Mots-clés | Secret GitHub |
|---|---|---|
| #special | ポケモン ポケモンキッズ 色違いメガレックウザ · ポケモンキッズ 当選品 限定 · ポケモンキッズ 当選品 ピカチュウ · ポケモンキッズ ピカチュウ クリアバージョン | `DISCORD_WEBHOOK_SPECIAL` |
| #shiny | 指人形 色違い · ポケモンキッズ 色違い · ポケモンキッズ 当選品 | `DISCORD_WEBHOOK_SHINY` |
| #finger-puppet | ポケモン 指人形 | `DISCORD_WEBHOOK_FINGER_PUPPET` |
| #pokemon-kids | ポケモンキッズ | `DISCORD_WEBHOOK_POKEMON_KIDS` |

Une annonce trouvée par plusieurs salons n'est envoyée qu'une fois, dans le premier salon de la liste.
Quand tu ajoutes un mot-clé, les annonces déjà en ligne pour lui sont mémorisées sans être envoyées.
Si un secret manque, les annonces de ce salon vont dans #général. Les messages du robot (démarrage, panne) restent dans #général.
