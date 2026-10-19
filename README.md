# SCP Foundation — site de blogs privé

Site web en français, thème SCP / terminal de confinement / dossiers classifiés / horreur. Les visiteurs lisent les articles publiés. Deux identifiants Discord autorisés peuvent créer, modifier, publier et supprimer les articles.

## Fonctionnalités

- Pages : accueil, archives, dossiers SCP, rapports d'incident, journaux de recherche, histoires et annonces.
- Recherche, filtres, fiches article et navigation responsive.
- Éditeur Markdown : titres, gras, italique, citations, code et caractères Unicode spéciaux.
- Téléversement d'images PNG, JPG, WEBP et GIF (8 Mo max).
- Brouillons et publications, modification et suppression.
- Connexion Discord OAuth2 avec liste blanche d'identifiants Discord vérifiée côté serveur.
- Base de données SQLite, adaptée au développement local.

## Lancer le site en local

Python 3.11 ou plus récent est recommandé.

### Windows (PowerShell)

```powershell
cd scp_foundation_site
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Ouvre `.env` et renseigne la configuration Discord. Pour démarrer le serveur :

```powershell
python app.py
```

Puis ouvre http://127.0.0.1:5000.

### macOS / Linux

```bash
cd scp_foundation_site
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Configurer la connexion Discord

1. Ouvre le [Discord Developer Portal](https://discord.com/developers/applications) et crée une application.
2. Dans **OAuth2**, ajoute exactement `http://127.0.0.1:5000/auth/callback` comme Redirect URI pour le développement local.
3. Copie le Client ID et le Client Secret dans `.env`.
4. Récupère les identifiants numériques Discord des deux éditeurs. Dans Discord, active le **Mode développeur** dans Paramètres > Avancés, puis clic droit sur le profil > Copier l'identifiant utilisateur.
5. Dans `.env`, renseigne :
   - `DISCORD_CLIENT_ID`
   - `DISCORD_CLIENT_SECRET`
   - `DISCORD_REDIRECT_URI=http://127.0.0.1:5000/auth/callback`
   - `ADMIN_DISCORD_IDS=ID_NUMERIQUE_1,ID_NUMERIQUE_2`
   - `FLASK_SECRET_KEY` : une longue valeur aléatoire privée.
6. Redémarre le serveur, puis ouvre **Accès éditeur** et connecte-toi avec un compte autorisé.

**Ne partage jamais le Client Secret ni le fichier `.env`.** Le site n'a pas besoin d'un bot Discord : il utilise OAuth2 pour identifier les comptes. Le serveur vérifie leur ID à chaque requête d'administration.

## Déploiement en ligne

- Héberge l'application sur un service compatible Python/Flask.
- Configure toutes les variables d'environnement dans les paramètres secrets de l'hébergeur, pas dans le code.
- Utilise HTTPS et une URL de callback HTTPS identique dans Discord et dans `DISCORD_REDIRECT_URI`.
- Utilise un stockage persistant pour `scp_site.db` et `static/uploads/`. Les disques éphémères peuvent effacer les articles et images au redémarrage.
- Définis `FLASK_SECRET_KEY` avec une valeur aléatoire stable et privée.
- Utilise un serveur WSGI de production (par exemple Gunicorn) et ne mets jamais `FLASK_DEBUG=1` en production.
- Avant une ouverture publique, configure sauvegardes, limites de requêtes, journalisation et politique de contenu.

## Important

- Le projet fournit un vrai contrôle d'accès serveur sur les routes d'administration, mais il faut le déployer et le configurer correctement avant d'y publier des données privées.
- Les articles sont affichés en Markdown. Le rendu utilise l'extension Markdown standard et n'autorise pas les fichiers HTML bruts par défaut.
- Les données de démonstration sont fictives.
- Ce projet est un site de fan non officiel, non affilié à la SCP Foundation. Vérifie les licences et règles de crédit applicables aux contenus et images que tu publies.

## Déploiement sur Render (mis à jour)

1. Pousse le projet sur GitHub, puis crée un **Blueprint** Render (le fichier `render.yaml` configure tout) ou un Web Service manuel.
2. Commande de démarrage : `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT` ; health check : `/healthz`.
3. Variables : `FLASK_SECRET_KEY`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI` (identique au Redirect URI du portail Discord, en HTTPS), `ADMIN_DISCORD_IDS`, `DATA_DIR=/var/data`.
4. **Disque persistant** monté sur `/var/data` : sans lui (plan gratuit), la base SQLite et les images sont effacées à chaque redéploiement.
5. Les images sont servies via `/media/…` ; les anciens liens `/static/uploads/…` restent compatibles.
