# Lidoire Flood Watch

Surveillance des niveaux d'eau de la Lidoire au moulin (coordonnees 44.8759811, 0.0015091) en croisant marees, niveaux de rivieres, meteo et analyse video.

## Contexte hydrologique

- **Lidoire** : 49 km, bassin versant de 231 km² (SAGE Dordogne Atlantique), confluence rive droite avec la Dordogne entre Castillon-la-Bataille et Lamothe-Montravel.
- **Aucune station de hauteur ou de debit sur la Lidoire** : le niveau au moulin n'est pas mesure, il est infere des stations Hub'Eau voisines et de la pluie.
- **Confluence dans le bief maritime** : la Dordogne est fluviale jusqu'a Pessac-sur-Dordogne et soumise a la maree en aval, donc au droit de la confluence.
- **Temps de concentration** du bassin a la confluence : de l'ordre de 12 a 20 h (estimation par formules empiriques, pas de mesure).
- L'Isle et la Dronne rejoignent la Dordogne a Libourne, **en aval** de la confluence de la Lidoire : leurs stations ne renseignent pas sur le remous au moulin, elles servent seulement d'indicateurs de pluie regionale.

## Architecture

```
lidoire-flood-watch/
├── backend/                       # API FastAPI + collecte + scoring
│   ├── app/
│   │   ├── api/                   # Routes (status, realtime, timeline, risk, episodes, auth)
│   │   │   └── deps.py            # Controle X-API-Key sur les methodes d'ecriture
│   │   ├── data/
│   │   │   └── episodes_labels.json  # Verite terrain des 14 episodes annotes
│   │   ├── models/                # Schemas Pydantic, ORM PostgreSQL
│   │   └── services/
│   │       ├── risk_core.py       # Coeur de scoring pur, modele v3.3 (aucune I/O)
│   │       ├── risk_v2.py         # Construction des features (temps reel, simulation, backfill)
│   │       ├── forecast.py        # Previsions horaires (defaut 7 jours)
│   │       ├── backfill.py        # Backfill historique (rivers, rain, tides, soil, risk)
│   │       ├── episode_detector.py  # Detection automatique d'episodes
│   │       ├── labels.py          # Chargement des labels, migration des anciens prefixes
│   │       ├── cache.py           # Client Redis partage + cache TTL en memoire
│   │       ├── influx_client.py   # Acces InfluxDB non bloquant, ecritures par lot
│   │       ├── models_experimental.py  # Modeles candidats (hors production)
│   │       └── collectors/        # hubeau, vigicrues, tide, weather, netatmo, infoclimat, meteo_agricole
│   ├── scripts/eval/              # Harnais d'evaluation hors ligne (replay horaire)
│   ├── tests/                     # Tests par domaine (risk, storage, security, collectors...)
│   ├── requirements.txt
│   └── requirements-dev.txt
├── frontend/                      # React + Vite + TailwindCSS + Recharts
│   └── src/
│       ├── pages/                 # Dashboard, Timeline, Episodes, Configuration
│       └── components/            # RiskGauge, ForecastPanel, HydroMap, TimeMachineBanner, ApiKeyPanel...
├── docs/                          # Analyses et audits
├── docker-compose.yml
└── .env.example
```

## Stack technique

- **Backend** : FastAPI (Python 3.12 dans l'image Docker), APScheduler
- **Frontend** : React + Vite 8 + TailwindCSS + Recharts (Node 22 dans l'image Docker)
- **Bases de donnees** : InfluxDB 2.7 (series temporelles), PostgreSQL 15 (episodes)
- **Cache** : Redis 7

## Fonctionnalites

### Dashboard temps reel
- Score de risque 0-100 avec jauge et detail du calcul (facteurs, poids, multiplicateur, amortisseur, facteurs indisponibles)
- Niveaux de 7 stations hydrometriques avec tendance et vitesse (cm/h), Libourne signalee comme station a maree
- Pluviometrie multi-sources (Netatmo, Infoclimat, Open-Meteo)
- Marees (coefficient, phase, prochaine PM/BM)
- Conditions du sol (humidite, saturation, bilan hydrique)
- Carte interactive des stations (HydroMap), tooltips pedagogiques

### Previsions
- Score de risque horaire sur 168 h par defaut (`hours` de 6 a 336)
- Chaque heure est scoree par la meme fonction que le temps reel ; H+0 reprend les entrees temps reel
- Entrees : pluie Open-Meteo (AROME/ARPEGE + ECMWF) convertie en UTC, marees predites, projection des niveaux avec les delais de propagation du config, decroissance lineaire sur 48 h du nombre de stations en hausse
- Cache Redis (30 min)

### Timeline historique multi-sources
- Series : niveaux rivieres, pluie, marees, score de risque, sol, vent, overlay de prevision
- Resolutions 15m, 1h, 6h, 12h, 1d ; periode bornee a 400 jours
- Donnees manquantes completees par un backfill en arriere-plan (en-tete `X-Backfill-Pending`)

### TimeMachine
- Reconstruction du dashboard a une date passee (`/api/realtime/at`)
- Score recalcule avec les memes features que le backfill (points observes avant la date cible uniquement), jamais stocke

### Episodes d'inondation
- 14 episodes annotes (`backend/app/data/episodes_labels.json`) : 5 inondations, 6 faux positifs, 2 quasi-inondations, 1 inconnu
- Issue structuree par episode (`flooded`, `false_positive`, `near_miss`, `unknown`), modifiable via `/confirm/{id}`
- Analyse contextuelle (pluie avant, niveaux max, maree) et detection automatique par seuil de score

## Modele de risque v3.3

Implemente dans `backend/app/services/risk_core.py` (fonction pure `compute_risk`). v3.3 corrige les bugs de v3.2 sans changer la calibration (memes poids, memes mecanismes). Chaque point de score stocke porte le tag `model_version=3.3`.

**Constat cle** : le moulin est sur la Lidoire (affluent), pas sur la Dordogne. Un niveau Dordogne eleve seul ne signifie pas inondation au moulin. Le signal le plus fort est la montee simultanee de plusieurs stations + pluie, ou plusieurs stations au-dessus de leur seuil orange.

### Facteurs et poids

Poids lus dans `config.risk_weights` (somme = 1.00) :

| Facteur | Poids | Description |
|---------|-------|-------------|
| Niveaux rivieres (multi-stations) | 28% | Moyenne ponderee des stations de poids > 0 |
| Pluie 24h | 10% | Observee + 50% de la prevision 24h |
| Pluie 72h | 13% | Observee + 30% de la prevision 72h |
| Pluie 7j | 5% | Saturation longue duree |
| Tendance | 13% | Vitesse de montee de Pessac (cm/h) |
| Momentum | 8% | Stations en hausse simultanee (> 1 cm/h) |
| Coefficient maree | 5% | Coefficient de la maree |
| Phase maree | 5% | Proximite de la pleine mer |
| Saturation du sol | 5% | Facteur de ruissellement (1.0 sec a 1.5 sature) |
| Vent d'ouest | 3% | Surcote Gironde (30-100 km/h) |
| Analyse video | 5% | Index niveau d'eau camera |

Le denominateur est la somme des poids des facteurs disponibles (pluie 7j, vent, sol et video peuvent manquer). Si la maree est absente ou seulement estimee, ses deux facteurs valent 0 mais restent au denominateur.

### Mecanismes

- **Multiplicateur de conjonction** (1.0 a 2.5x) : cumule des bonus quand plusieurs stations montent avec de la pluie, quand au moins 3 stations sont au-dessus de leur propre seuil orange (+0.45 si au moins une monte, +0.30 sinon), quand la Dordogne est haute avec de la pluie cumulee (drainage empeche), et pour sol sature, pleine mer ou pluie soutenue sur riviere elevee.
- **Amortisseur de stabilite** (0.65 a 1.0x) : riviere haute (score >= 0.4) mais Pessac stable ou en baisse avec 0 station en hausse (0.65) ou 1 (0.80) ; Pessac en hausse mais au plus 1 station en hausse (0.85). Desactive si pluie 72h significative ou si au moins 3 stations sont au-dessus de leur seuil orange.
- **Gate du momentum** : le momentum est multiplie par 0.2 / 0.4 / 0.7 / 1.0 selon le score riviere normalise (< 0.30 / < 0.40 / < 0.55 / au-dela).

### Seuils d'alerte

| Niveau | Score | Signification |
|--------|-------|---------------|
| Vert | 0-15 | Pas de risque |
| Jaune | 15-35 | Vigilance |
| Orange | 35-55 | Risque eleve |
| Rouge | 55+ | Danger imminent |

### Un seul calcul pour tous les chemins

`compute_risk` est appele par `/api/realtime`, `/api/risk`, la simulation, la TimeMachine, le backfill et chaque heure des previsions. Seul le job planifie `risk_scorer` (toutes les heures) ecrit le score temps reel ; les routes de lecture ne stockent rien. Les entrees different selon le chemin : le replay historique n'a ni vent, ni sol, ni Netatmo/Infoclimat (facteurs indisponibles).

### Stations surveillees

Distances a vol d'oiseau depuis le moulin (haversine sur les coordonnees du config). Poids et delais : `config.hubeau_stations`.

| Station | Code Hub'Eau | Riviere | Distance | Delai | Poids |
|---------|-------------|---------|----------|-------|-------|
| Libourne | P577001001 | Dordogne | 19 km | 0 h | 0% (exclue : maree) |
| Pessac-sur-Dordogne | P555001001 | Dordogne | 7.6 km | 4 h | 30% (station primaire) |
| Bergerac | P514001001 | Dordogne | 38 km | 8 h | 15% |
| Abzac | P726151001 | Isle | 10 km | 2 h | 25% |
| Perigueux | P704151001 | Isle | 66 km | 12 h | 10% |
| Coutras | P846251001 | Dronne | 21 km | 3 h | 15% |
| Riberac | P816251001 | Dronne | 49 km | 10 h | 5% |

- Libourne (oscillation de maree d'environ 4 m) est affichee mais exclue du score ; l'alerte « Niveau Dordogne eleve » compare Pessac a son seuil orange.
- Pessac est a environ 7 km en amont de la confluence : c'est la seule station proche du remous impose a la Lidoire.
- Isle et Dronne confluent a Libourne, en aval de la Lidoire : ce sont des indicateurs de pluie regionale, pas des causes de crue au moulin.
- Le champ `distance_km` du config (affichage uniquement) ne correspond pas a ces distances.

## Evaluation hors ligne

Harnais dans `backend/scripts/eval/` : replay horaire causal (seuls les points observes avant chaque heure sont utilises) de v3.3 et de modeles de reference sur les archives publiques (Hydroportail, Open-Meteo archive et marine). Pas besoin d'InfluxDB.

```bash
cd backend
python -m scripts.eval build              # une fois : ~4 min de telechargements, cache dans data/eval_cache/
python -m scripts.eval report --offline   # replay + metriques depuis le cache
python -m scripts.eval.candidates --offline   # comparaison des modeles candidats
```

Sorties dans `backend/data/eval_out/` (`summary.md`, `metrics.json`, `alerts.csv`, `events.csv`, `unlabelled_alerts.csv`). Options utiles : `--merge-gap 12`, `--strict-start`, `--no-tide`, `--sensitivity`, `--threshold orange=35`.

### Definitions

- **Alerte** : suite d'heures consecutives avec score >= seuil (orange 35, rouge 55).
- **Detection** : une alerte qui demarre entre 72 h avant et 6 h apres le pic d'une crue labellisee (ou deja en cours a l'ouverture de cette fenetre).
- **POD** = crues detectees / crues. **FAR brut** = alertes non associees a une crue (labellisees negatives + non labellisees) / alertes jugees. **FAR labellise** : negatives labellisees seulement. **CSI** = detections / (detections + manques + fausses alertes).
- **Avance** : pic - debut de l'alerte (labels au jour pres : -6 a 0 h signifie "alerte autour du pic").
- **AUC horaire** : cible "crue dans les 24 h suivantes" a chaque heure (125 heures positives, 28 170 negatives).

### Resultats actuels

Periode 2022-11-01 a 2026-10-25, alertes non fusionnees, 5 crues. Protocole : v3.3 est in-sample (environ 45 constantes reglees a la main sur ces episodes) ; "sans label" = aucun parametre appris sur les labels ; "LOEO bloque" = validation croisee par episode, chiffres hors echantillon.

| Modele | Protocole | Niveau | Crues | Alertes | FA brutes (lab.+non lab.) | FAR brut / lab. | CSI | Avance med./min (h) | Alertes/an | AUC |
|--------|-----------|--------|-------|---------|------|------|-----|------|------|-----|
| v3.3 | in-sample | orange | 5/5 | 241 | 221 (20+201) | 98% / 80% | 0.02 | 63 / 11 | 68.0 | 0.952 [0.89-0.99] |
| v3.3 | in-sample | rouge | 5/5 | 79 | 65 (13+52) | 93% / 72% | 0.07 | 21 / 5 | 23.1 | |
| pessac_level | sans label | orange | 4/5 | 20 | 16 (3+13) | 80% / 43% | 0.19 | 37 / -2 | 5.8 | 0.894 [0.78-0.97] |
| pessac_level | sans label | rouge | 3/5 | 9 | | 67% / 25% | 0.27 | 3 / -4 | 3.0 | |
| rain_72h | sans label | orange | 1/5 | 1 | | | 0.20 | | | 0.912 |
| v3.3 + gate remous | sans label | orange | 5/5 | 74 | 62 (11+51) | 93% / 69% | 0.07 | 20 / 0 | 23.6 | 0.948 [0.88-0.99] |
| v3.3 + gate remous | sans label | rouge | 4/5 | 56 | | | 0.08 | 44 / 11 | | |
| bw_rule_api_cv | LOEO bloque, 2 param. | orange | 3/5 | 8 | 5 (1+4) | 62% / 25% | 0.30 | 12 / 4 | 2.5 | 0.928 [0.86-0.98] |
| lr_gr4h_haval_cv | LOEO bloque, 3 param. | orange | 4/5 | 15 | 10 (2+8) | 71% / 33% | 0.27 | 3 / -2 | 4.0 | 0.909 [0.81-0.98] |

rain_72h ne detecte aucune crue au rouge, lr_gr4h_haval_cv une seule. Intervalles a 95% du POD : 5/5 = [48-100%], 4/5 = [28-99%], 3/5 = [15-95%].

Alertes separees de 12 h ou moins fusionnees (orange) : v3.3 57 alertes, FA 51, CSI 0.09, 15.3/an ; v3.3 + gate remous 21 alertes, FA 16 (5+11), CSI 0.24, 5.8/an, POD 5/5 ; pessac_level 13 alertes, CSI 0.29 ; bw_rule_api_cv 8 alertes, CSI 0.30 ; lr_gr4h_haval_cv 13 alertes, CSI 0.29.

### Precautions de lecture

- 5 crues seulement : une crue = 20 points de POD, aucun ecart entre candidats hors echantillon et pessac_level n'est significatif.
- Labels non exhaustifs : des alertes "non labellisees" peuvent etre de vraies crues non annotees, le FAR brut est pessimiste.
- Seul le 5/5 de v3.3 est in-sample, donc optimiste. 2023-03-11 est une crue par infiltration (depuis colmatee), 2023-12-11 est peut-etre le meme evenement que 2023-12-01.
- Le score v3.3 oscille autour de 35 sur un cycle d'environ 12 h qui suit la maree : avec `--no-tide`, les alertes orange passent de 241 a 122. La phase de maree et le terme de conjonction maree produisent l'essentiel des fausses alertes.

### Periodes d'alerte v3.3 a labelliser

2023-11-02 au 11-06, 2023-11-09 au 11-15, 2024-01-18 au 01-19, 2024-02-10 au 02-13, 2024-03-02 au 03-04, 2024-05-14 au 05-16, 2024-05-20 au 05-23, 2024-11-21 au 11-22, 2025-01-27 au 01-29. Liste complete : `unlabelled_alerts.csv`.

## Sources de donnees

### Marees
- **SHOM** : API officielle (cle API requise)
- **Open-Meteo Marine** : niveau marin modele a l'embouchure de la Gironde ; PM/BM extraites de la serie, coefficient estime depuis le marnage
- Les marees estimees (calcul de secours) ne sont jamais stockees et rendent la maree indisponible pour le score

### Niveaux rivieres
- **Hub'Eau** : temps reel (30 derniers jours), 7 stations
- **Hydroportail** : historique infra-journalier pour le backfill
- **Vigicrues** : niveau de Libourne (en metres)

### Precipitations
- **Netatmo** : stations citoyennes du bassin Lidoire (OAuth2)
- **Infoclimat** : reseau StatIC (cle API requise)
- **Open-Meteo** : observations et previsions AROME/ARPEGE (UTC)
- Fusion temps reel : maximum des sources sur 1 h et 24 h

### Conditions du sol
- **Open-Meteo** : humidite du sol, temperature, evapotranspiration
- **Meteo Agricole** : bilan hydrique, pluie nowcast

## Stockage des donnees

Measurements InfluxDB principaux : `river_level`, `tide`, `rainfall`, `risk_score` (tag `model_version`), `soil_timeline`, `wind`, `nowcast_rain`. PostgreSQL stocke les episodes.

Pattern cache-through : lecture InfluxDB d'abord, sinon recuperation depuis l'API externe et stockage permanent.

Jobs planifies (APScheduler) : rivieres et Netatmo toutes les 15 min, meteo toutes les heures, marees et Meteo Agricole toutes les 6 h, nowcast toutes les 5 min, score de risque toutes les heures (premier calcul 2 min apres le demarrage), camera si `CAMERA_RTSP_URL` est defini.

## Installation

### Prerequis

- Docker et Docker Compose
- Node.js 22 (Vite 8 exige Node >= 20.19) pour le developpement frontend
- Python 3.12 pour le developpement backend

### Lancement avec Docker

```bash
cp .env.example .env    # puis remplir chaque secret vide (openssl rand -hex 32)
docker compose up -d --build
```

Dashboard sur http://localhost:3100, API sur http://localhost:8100.

### Developpement local

Backend :
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload      # port 8000
pytest -q                          # tests (tests/<domaine>/test_<domaine>_*.py)
```

Frontend :
```bash
cd frontend
npm ci
npm run dev                        # port 3000, proxy /api -> http://localhost:8000
npm run lint
npx tsc --noEmit -p .
npm run build
```

Pour exposer Postgres, InfluxDB et Redis en local, creer un `docker-compose.override.yml` (ignore par git, exemple en tete de `docker-compose.yml`) : 127.0.0.1:5532, 8186, 6479.

## Securite et deploiement

### Exposition reseau

| Service | Port hote | Remarque |
|---------|-----------|----------|
| Frontend | 3100 | nginx non root, ecoute sur 8080 dans le conteneur |
| Backend API | 8100 | |
| PostgreSQL, InfluxDB, Redis | aucun | reseau interne Docker uniquement |

Les ports publies sont lies a `BIND_ADDRESS` (defaut `127.0.0.1`). Pour un acces depuis le LAN (`BIND_ADDRESS=0.0.0.0`), definir imperativement `API_KEY`.

### Cle API

- `POST`/`PUT`/`PATCH`/`DELETE` sur `/api/*` exigent l'en-tete `X-API-Key: <API_KEY>` (401 sinon). Les `GET` restent publics.
- `API_KEY` vide = ecritures non protegees (dev uniquement, avertissement au demarrage). Le backend refuse de demarrer si `API_KEY`, un token InfluxDB ou un mot de passe DB/Redis vaut un placeholder connu (`change-me`, ...).
- Dans le dashboard, la cle se saisit dans Configuration et reste dans le navigateur (localStorage `lfw_api_key`).
- La connexion Netatmo (`GET /api/auth/netatmo`) exige la cle en `?api_key=` quand `API_KEY` est defini.

### Variables d'environnement

Obligatoires (docker compose echoue si elles sont vides) : `POSTGRES_PASSWORD`, `INFLUXDB_ADMIN_PASSWORD`, `INFLUXDB_ADMIN_TOKEN`, `REDIS_PASSWORD`. Elles sont vides dans `.env.example` : les generer (`openssl rand -hex 32`), en gardant pour une installation existante le mot de passe Postgres et le token InfluxDB deja utilises par les volumes.

Recommandees : `API_KEY`, `INFLUXDB_TOKEN` (jeton limite au bucket, sinon le jeton admin est utilise), `INFLUXDB_READ_TOKEN` (jeton en lecture seule pour les requetes Flux, sinon `INFLUXDB_TOKEN`). Un jeton de lecture invalide casse toutes les lectures alors que les ecritures continuent.

Autres : `BIND_ADDRESS`, `CORS_ORIGINS` (liste explicite, pas de joker), `FRONTEND_URL` (sert aussi a l'URL de callback Netatmo), `NETATMO_*`, `INFOCLIMAT_API_KEY`, `SHOM_API_KEY`, `CAMERA_RTSP_URL`, `LOG_LEVEL`. Voir `.env.example`.

### Migration d'une installation existante

Avant le premier `docker compose up --build` avec cette version :

1. Ajouter a `.env` les nouvelles variables en conservant les secrets des volumes existants :
   - `POSTGRES_PASSWORD=<mot de passe actuel>` (ancien defaut : `postgres`), ou changer le mot de passe ensuite par `ALTER USER` ;
   - `INFLUXDB_ADMIN_TOKEN=<valeur actuelle de INFLUXDB_TOKEN>` et `INFLUXDB_ADMIN_PASSWORD=<mot de passe de setup>` (les variables `DOCKER_INFLUXDB_INIT_*` ne servent qu'au premier demarrage) ;
   - `REDIS_PASSWORD` et `API_KEY` nouveaux (`openssl rand -hex 32`).
2. Le backend tourne desormais en uid 10001 : corriger une fois le volume des jetons Netatmo :
   ```bash
   docker compose run --rm --user root backend chown -R 10001:10001 /app/data
   ```
3. Le frontend (nginx-unprivileged) ecoute sur 8080 dans le conteneur, publie sur 3100 : garder `Dockerfile`, `nginx.conf` et le mapping `3100:8080` coherents.

## Deploiement Kubernetes

Chart Helm `deploy/helm/lidoire-flood-watch/` pour le cluster Scaleway Kapsule `sharedhosting` (contexte `admin@sharedhosting-production`). Images publiees sur GHCR par `.github/workflows/images.yml` (`ghcr.io/clark42/lidoire-flood-watch-{backend,frontend}`, tags `sha-<7>`, `main`, semver).

Prerequis :
- stack Terraform `11-lidoire` appliquee : namespace `lidoire` (PSS `restricted`), NetworkPolicies, secrets Scaleway Secret Manager `lidoire-generated`, `lidoire-external-apis` (vide), `lidoire-backup-s3` et bucket de sauvegarde ;
- images publiees pour le tag deploye (workflow `images` vert sur `main`).

```bash
helm upgrade --install lidoire deploy/helm/lidoire-flood-watch -n lidoire \
  --set image.tag=sha-$(git rev-parse --short=7 HEAD) \
  --kube-context admin@sharedhosting-production
kubectl get externalsecret,pods,pvc -n lidoire --context=admin@sharedhosting-production
```

- Le chart ne cree ni le namespace ni les NetworkPolicies. Secrets via External Secrets (`ClusterSecretStore` `scaleway-secret-manager`) : aucun secret dans les values ni dans les manifests ; `DATABASE_URL`/`REDIS_URL` sont assembles par le template ESO (Secret `lidoire-backend`).
- Backend : 1 replique, strategie `Recreate` (APScheduler embarque, sinon collectes en double). Postgres, InfluxDB : StatefulSets sur `sbs-default-retain` (5 Gi, 10 Gi), `/app/data` du backend : PVC 1 Gi. Redis : cache sans persistance.
- Cles d'API externes (Netatmo, Infoclimat, SHOM...) : ajouter une version JSON au secret `lidoire-external-apis`, puis `--set externalApis.enabled=true`.
- Acces : `http://lidoire.ts.kamorion.cloud` via le tailnet headscale (Service `frontend`, ClusterIP fixe `10.32.0.100`). Pas d'Ingress par defaut (`ingress.enabled`).

Reconstitution des donnees apres une installation neuve (InfluxDB vide) :
```bash
API_KEY=$(kubectl get secret lidoire-backend -n lidoire -o jsonpath='{.data.API_KEY}' --context=admin@sharedhosting-production | base64 -d)
curl -X POST -H "X-API-Key: $API_KEY" "http://lidoire.ts.kamorion.cloud/api/episodes/backfill?start_date=2022-11-01&end_date=$(date +%F)"
curl http://lidoire.ts.kamorion.cloud/api/episodes/backfill/status
```

Sauvegarde : CronJob `backup` quotidien (03:30 Europe/Paris) : `pg_dump` + `influx backup` vers `s3://<BUCKET>/lidoire-flood-watch/<horodatage UTC>/`, expiration a 30 jours par la regle de cycle de vie du bucket (la cle de sauvegarde n'a pas le droit de supprimer, `backup.retentionDays` reste a 0). Lancement manuel : `kubectl create job --from=cronjob/backup backup-manual -n lidoire --context=admin@sharedhosting-production`.

Restauration (telecharger d'abord `postgres.dump` et le dossier `influxdb/` d'une sauvegarde dans `./restore`, avec les identifiants du secret `lidoire-backup-s3`) :
```bash
kubectl scale deploy/backend --replicas=0 -n lidoire --context=admin@sharedhosting-production
# PostgreSQL
kubectl exec -i postgres-0 -n lidoire --context=admin@sharedhosting-production -- \
  pg_restore -U postgres -d lidoire_flood --clean --if-exists --no-owner < restore/postgres.dump
# InfluxDB (--full : remplace toutes les donnees et metadonnees)
kubectl cp restore/influxdb lidoire/influxdb-0:/tmp/restore --context=admin@sharedhosting-production
kubectl exec influxdb-0 -n lidoire --context=admin@sharedhosting-production -- \
  sh -c 'influx restore /tmp/restore --full --host http://127.0.0.1:8086 --token "$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN" && rm -rf /tmp/restore'
kubectl scale deploy/backend --replicas=1 -n lidoire --context=admin@sharedhosting-production
```

## API Endpoints

Toutes les routes sont sous `/api`. Les methodes d'ecriture exigent `X-API-Key` quand `API_KEY` est defini (y compris les POST d'analyse en lecture seule).

### Status
- `GET /api/health` : healthcheck simple
- `GET /api/status` : verifications reelles InfluxDB, PostgreSQL, Redis (`connected` | `error` | `timeout`, 2 s max, details sous `services.checks`) ; `status` vaut `healthy` ou `degraded`
- `GET /api/sources-detail` : detail des sources configurees

### Temps reel
- `GET /api/realtime` : donnees temps reel (risque, maree, rivieres, pluie, sol, vent)
- `GET /api/realtime/at?timestamp=<iso>` : TimeMachine
- `GET /api/realtime/stations`, `/tide`, `/river`, `/rainfall`, `/netatmo`, `/infoclimat`, `/sources`, `/basin-rainfall`

### Timeline
- `GET /api/timeline/sources` : stations et sources de pluie disponibles (ids canoniques)
- `GET /api/timeline/unified` : series combinees. 422 si `start >= end`, periode > 400 jours ou id inconnu (detail `{error, unknown, valid}`). Attend au plus 5 s les backfills ; ceux encore en cours sont listes dans l'en-tete `X-Backfill-Pending` (recharger plus tard).

### Risque
- `GET /api/risk` : score actuel (memes entrees que `/api/realtime`, rien n'est stocke)
- `GET /api/risk/forecast?hours=168` : previsions horaires (6 a 336 h)
- `GET /api/risk/forecast/multi-model` : pluie prevue par plusieurs modeles meteo
- `GET /api/risk/thresholds` : seuils configures
- `POST /api/risk/simulate` : simulation avec valeurs saisies (rien n'est stocke)

### Episodes
- `GET /api/episodes` : liste (`limit`, `offset`, `min_severity`)
- `GET /api/episodes/labels` : verite terrain de reference (`episodes_labels.json`)
- `POST /api/episodes` : creer ; `GET` / `PUT` / `DELETE /api/episodes/{id}`
- `POST /api/episodes/confirm/{id}` : fixer l'issue (`outcome` = `flooded` | `false_positive` | `near_miss` | `unknown`, ou l'ancien `was_flooded`), plus `actual_severity`, `notes`, `observed_at`, `water_height_cm`, `source`. Une nouvelle confirmation remplace la precedente.
- `POST /api/episodes/analyze`, `POST /api/episodes/analyze-v2?date=...`, `GET /api/episodes/analyze/{id}` : analyse du contexte hydro-meteo
- `POST /api/episodes/compare?dates=...&dates=...` : comparaison d'episodes
- `GET /api/episodes/patterns`, `GET /api/episodes/station-extremes?start_date=...&end_date=...`

### Backfill et detection
- `POST /api/episodes/backfill` : backfill historique en tache de fond (`skip_rivers`, `skip_rainfall`, `skip_tides`, `skip_soil`, `skip_risk`)
- `GET /api/episodes/backfill/status` : progression
- `POST /api/episodes/detect?threshold=35` : detection d'episodes. Un point appartient a un episode si son score >= `threshold` (0-100, defaut 35) ; l'episode est retenu s'il dure au moins 6 h au-dessus de 35 ou 2 h au-dessus de 55. S'y ajoutent les depassements du seuil rouge d'une station non tidale.

### Authentification Netatmo
- `GET /api/auth/netatmo` (cle API en `?api_key=` si configuree), `/netatmo/callback`, `/netatmo/status`, `/netatmo/setup`

## Pistes

- **Capteur de niveau au moulin** (LoRaWAN par exemple) : seule mesure directe possible, il n'existe aucune station sur la Lidoire.
- **Pluie radar** (COMEPHORE pour l'historique, ANTILOPE en temps reel) a la place de la moyenne Open-Meteo sur 3 points.
- **Gate de remous en mode shadow** sur v3.3 pendant une saison : divise les alertes orange par environ 3 a POD constant dans le replay, au prix d'une avance plus courte. A activer seulement apres labellisation des periodes ci-dessus.
- **Regression logistique ou GR4H** une fois davantage d'episodes labellises : avec 5 crues, aucun candidat ne bat significativement le seul niveau de Pessac.

## Licence

Projet prive - Usage personnel
