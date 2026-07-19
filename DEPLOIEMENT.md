# Mettre le site en ligne — mode d'emploi

Le site est prêt pour GitHub Pages (hébergement gratuit) avec actualisation
automatique quotidienne des pronostics via GitHub Actions.

## 1. Créer le dépôt

1. Crée un compte sur https://github.com (gratuit) si besoin.
2. Clique sur **New repository** : nom `sail-by-self` (par exemple),
   visibilité **Public** (obligatoire pour Pages en gratuit), sans README.

## 2. Envoyer les fichiers

Le plus simple sans ligne de commande : installe **GitHub Desktop**
(https://desktop.github.com), connecte ton compte, puis
**Add local repository** → choisis ce dossier → **Publish repository**.

⚠ Le fichier `pronostics/generator/api_key.txt` est protégé par le
`.gitignore` : il ne sera pas envoyé. C'est voulu — ne le publie jamais.

## 3. Donner la clé API à GitHub (pour l'automatisation)

Sur la page du dépôt : **Settings → Secrets and variables → Actions →
New repository secret**.
- Name : `FOOTBALL_DATA_KEY`
- Secret : ta clé football-data.org

## 4. Activer l'hébergement

**Settings → Pages** → Source : **Deploy from a branch** →
Branch : `main`, dossier `/ (root)` → **Save**.
Après 1-2 minutes, le site est en ligne sur
`https://TON-PSEUDO.github.io/sail-by-self/`.

## 5. L'actualisation automatique

Rien à faire : le workflow `.github/workflows/update-pronostics.yml`
régénère `pronostics/data.js` chaque jour à 08h00 (heure de Paris, été)
et republie le site si les pronostics ont changé.
Pour forcer une actualisation : onglet **Actions** → « Actualiser les
pronostics » → **Run workflow**.

## Mettre à jour le site ensuite

Modifie les fichiers dans ce dossier, puis dans GitHub Desktop :
écris un petit résumé → **Commit to main** → **Push origin**.
Le site en ligne est mis à jour en ~1 minute.
