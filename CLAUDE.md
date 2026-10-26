# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

Lidoire Flood Watch - Surveillance des niveaux d'eau de la Lidoire au moulin (44.8759811, 0.0015091) en croisant marées, niveaux de rivières, météo et analyse vidéo.

Faits hydrologiques a garder en tete :
- Lidoire : 49 km, bassin de 231 km² (SAGE Dordogne Atlantique), confluence rive droite avec la Dordogne entre Castillon-la-Bataille et Lamothe-Montravel, dans le bief soumis a la maree.
- Aucune station de hauteur/debit sur la Lidoire : le niveau au moulin n'est jamais mesure.
- Temps de concentration a la confluence : ordre de grandeur 12-20 h (formules empiriques).
- Isle et Dronne rejoignent la Dordogne a Libourne, en aval de la confluence de la Lidoire : simples indicateurs de pluie regionale.

## Commandes de développement

### Lancement complet (Docker)
```bash
cp .env.example .env                    # remplir chaque secret vide (openssl rand -hex 32)
docker compose up -d --build
```

### Backend (FastAPI, Python 3.12 dans l'image)
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload          # Port 8000
pytest -q                               # Suite complete (pytest.ini : testpaths=tests, asyncio_mode=auto)
pytest -k "test_name"                   # Test unitaire specifique
```

Tests : `backend/tests/<domaine>/test_<domaine>_*.py`, sans `__init__.py`.

### Frontend (React + Vite 8 + TypeScript, Node 22)
```bash
cd frontend
npm ci
npm run dev                             # Port 3000, proxy /api -> http://localhost:8000
npm run lint                            # ESLint 9
npx tsc --noEmit -p .                   # Verification des types
npm run build                           # tsc + vite build
```

### Evaluation hors ligne
```bash
cd backend
python -m scripts.eval build            # une fois, ~4 min de telechargements (cache data/eval_cache/)
python -m scripts.eval replay --offline # scores horaires v3.3 + baselines
python -m scripts.eval report --offline # metriques -> data/eval_out/
python -m scripts.eval.candidates --offline   # modeles candidats (data/eval_out/candidates/)
```

## Architecture

### Backend (`backend/app/`)

```
app/
├── main.py              # FastAPI app, lifespan (init DB, scheduler, close Redis), CORS
├── config.py            # Settings Pydantic (env vars, stations, poids et seuils du modele)
├── db.py                # Moteur SQLAlchemy async (PostgreSQL)
├── api/                 # Routes FastAPI (toutes sous /api)
│   ├── deps.py          # require_api_key_for_writes : X-API-Key sur POST/PUT/PATCH/DELETE
│   ├── status.py        # /api/status (checks reels Influx/Postgres/Redis + fraicheur des sources), /api/health
│   ├── realtime.py      # /api/realtime + TimeMachine /at
│   ├── timeline.py      # /api/timeline/sources, /api/timeline/unified
│   ├── episodes.py      # /api/episodes : CRUD, labels, confirm, analyse, backfill, detection
│   ├── risk.py          # /api/risk : score, previsions, simulation
│   └── auth.py          # /api/auth/netatmo (OAuth2)
├── data/
│   └── episodes_labels.json  # Verite terrain des 14 episodes (source unique)
├── models/
│   ├── schemas.py       # Modeles Pydantic (RiskScore, EpisodeOutcome, EpisodeLabelSet...)
│   └── orm.py           # Tables PostgreSQL
└── services/
    ├── risk_core.py     # Coeur de scoring PUR, modele v3.3 (compute_risk, aucune I/O)
    ├── risk_v2.py       # Construction des features (temps reel, simulation, backfill), stockage
    ├── forecast.py      # Previsions horaires (features par heure -> compute_risk)
    ├── backfill.py      # Backfill historique (rivers, rain, tides, soil, risk)
    ├── episode_detector.py  # Detection automatique d'episodes par seuil
    ├── episode_analyzer.py / episode_analyzer_v2.py  # Contexte hydro-meteo des episodes
    ├── labels.py        # Chargement des labels, parsing des anciens prefixes [CONFIRMED: ...]
    ├── cache.py         # get_redis() partage, TTLCache en memoire, CircuitBreaker, EXTERNAL_TIMEOUT_S
    ├── influx_client.py # InfluxDB non bloquant, ecritures par lot, validation Flux
    ├── data_store.py    # Acces InfluxDB/PostgreSQL de plus haut niveau
    ├── scheduler.py     # APScheduler (collectes + score horaire)
    ├── video_analyzer.py    # Detection niveau d'eau via camera
    ├── models_experimental.py  # Modeles candidats, importes par AUCUN code de production
    └── collectors/
        ├── hubeau.py        # Hub'Eau (temps reel) + Hydroportail (historique)
        ├── vigicrues.py     # Vigicrues (Libourne, metres)
        ├── tide.py          # SHOM / Open-Meteo Marine (marees estimees jamais stockees)
        ├── weather.py       # Open-Meteo (pluie, vent, sol), UTC
        ├── netatmo.py       # Netatmo (stations citoyennes du bassin)
        ├── infoclimat.py    # Infoclimat StatIC
        └── meteo_agricole.py # Meteo Agricole (bilan hydrique, nowcast)
```

### Scripts et tests (`backend/`)

```
scripts/eval/            # Harnais d'evaluation (python -m scripts.eval build|replay|report)
├── sources.py           # Telechargements + cache (Hydroportail, Open-Meteo archive/marine)
├── dataset.py           # Lignes horaires FeatureRow (features risk_core + valeurs brutes)
├── truth.py             # Evenements depuis episodes_labels.json
├── contingency.py       # Alertes, POD/FAR/CSI, avance
├── hourly.py            # Cible "crue dans 24 h", ROC-AUC, Brier
├── models.py            # Protocole Model, V33Model, PessacLevelModel, Rain72hModel
├── loeo.py              # Leave-one-event-out
├── report.py            # summary.md, metrics.json, CSV
└── candidates.py        # Comparaison des modeles candidats (LOEO bloque)
tests/
├── risk/  storage/  security/  collectors/
├── groundtruth/  evaluation/  models/  followups/
```

Supprimes : `services/risk.py` (modele v1), `validate_model.py`, `analyze_episodes.py` (remplaces par `scripts/eval/`).

### Frontend (`frontend/src/`)

```
src/
├── App.tsx              # Router principal, navigation
├── api.ts               # Client API (fetchJson, X-API-Key depuis localStorage 'lfw_api_key')
├── types.ts             # Types TypeScript (miroir des schemas backend)
├── utils/time.ts        # Helpers de dates (UTC)
├── tooltipContent.ts    # Dictionnaire centralise des textes de tooltips
├── pages/
│   ├── Dashboard.tsx    # Vue temps reel avec score de risque + TimeMachine
│   ├── Timeline.tsx     # Graphiques historiques multi-sources + forecast overlay
│   ├── Episodes.tsx     # Liste/creation/analyse/issue des episodes
│   └── Configuration.tsx # Statut, simulation, cle API
└── components/
    ├── RiskGauge.tsx, RiskBreakdownPanel.tsx, StationsPanel.tsx, RainfallPanel.tsx
    ├── ForecastPanel.tsx, BasinRainfallGauge.tsx, SoilConditionsPanel.tsx, NowcastRainChart.tsx
    ├── HydroMap.tsx, MetricSparkline.tsx, TimeMachineBanner.tsx, TimelineSourceSelector.tsx
    ├── InfoTooltip.tsx, DataCard.tsx, DataSourcesStatus.tsx
    └── ApiKeyPanel.tsx  # Saisie de la cle API (localStorage)
```

## Modele de risque v3.3

Une seule implementation : `risk_core.compute_risk(features: RiskFeatures) -> RiskBreakdown` (pure, sans I/O ni `utcnow()`). Tous les chemins construisent un `RiskFeatures` et l'appellent : `/api/realtime` et `/api/risk` (via `risk_v2.gather_realtime_inputs`), `/api/risk/simulate`, TimeMachine `/api/realtime/at`, `risk_v2.backfill_risk_scores`, et chaque heure de `forecast.py`. Les parametres viennent de `config.py` (`get_model_params()`). v3.3 = corrections de bugs de la calibration v3.2 (14 episodes), poids et mecanismes inchanges.

Constat cle : le moulin est sur la Lidoire (affluent), PAS sur la Dordogne. Un niveau Dordogne eleve seul ne signifie pas inondation au moulin. Le signal le plus fort est la montee simultanee de plusieurs stations + pluie.

| Facteur | Cle `risk_weights` | Poids | Calcul |
|---------|--------------------|-------|--------|
| Niveaux rivieres | `river` | 28% | Moyenne ponderee des stations de poids > 0 (Libourne exclue) |
| Pluie 24h | `rain_24h` | 10% | Observee + 0.5 x prevision 24h ; seuils 25/40/60 mm |
| Pluie 72h | `rain_72h` | 13% | Observee + 0.3 x prevision 72h ; seuils 50/80/120 mm |
| Pluie 7j | `rain_7d` | 5% | Seuils 60/100/150 mm |
| Tendance | `tendency` | 13% | Vitesse de la station primaire (Pessac), cm/h |
| Momentum | `momentum` | 8% | Stations en hausse (> 1 cm/h), avec gate |
| Coefficient maree | `tide` | 5% | Seuils 70/85/110 |
| Phase maree | `tide_phase` | 5% | 1.0 a <= 1 h de la PM, 0.1 au-dela de 4 h |
| Saturation sol | `soil_sat` | 5% | (facteur - 1.0) / 0.5, facteur 1.0-1.5 |
| Vent d'ouest | `wind_west` | 3% | Lineaire 30-100 km/h |
| Analyse video | `video` | 5% | Index camera 0-100 |

Somme des poids = 1.00. Score station : lineaire par morceaux (0.33 au seuil jaune, 0.66 a l'orange, 1.0 au rouge) puis courbe x^0.6. Une station ne compte que si son dernier point a moins de 3 h (`risk_level_max_age_hours`) ; vitesse par regression sur 3 h de points <= t.

- **Denominateur** : somme des poids des facteurs disponibles (`rain_7d`, `wind_west`, `soil_sat`, `video` peuvent etre indisponibles, listes dans `unavailable`). La maree reste au denominateur : absente ou estimee => `tide` et `tide_phase` valent 0 (note "tide data unavailable or estimated").
- **Sol** : n'entre que par `soil_saturation_factor` (multiplie les scores pluie, facteur `soil_sat`, branche de conjonction). Ne jamais pre-multiplier les millimetres.
- **Multiplicateur de conjonction** (1.0 a 2.5x, `conjunction_multiplier`) : branches cumulees "crue active" (>= 4 stations en hausse + riviere > 0.35 + pluie), "multi-stations au-dessus de l'orange" (>= 3 stations a hauteur >= leur propre seuil orange : +0.45 si >= 1 en hausse, +0.30 sinon), "drainage empeche" (riviere haute + pluie 72h, +0.15 a +0.45), sol sature +0.15, montee + pluie +0.20, pluie soutenue +0.15, pleine mer + riviere > 0.5 +0.15.
- **Amortisseur de stabilite** (0.65 a 1.0x, `stability_dampener`) : si score riviere >= 0.4, pluie 72h faible et moins de 3 stations au-dessus de l'orange : Pessac stable/en baisse avec 0 station en hausse => 0.65, 1 => 0.80 ; Pessac en hausse avec <= 1 station en hausse => 0.85.
- **Gate momentum** : base selon le nombre de stations en hausse (0 / 0.2 / 0.5 / 0.8 / 1.0) multipliee par 0.2 / 0.4 / 0.7 / 1.0 selon le score riviere normalise (< 0.30 / < 0.40 / < 0.55 / au-dela ; score, pas metres).
- **Score final** = min(100, somme ponderee x conjonction x amortisseur / denominateur x 100). Niveaux : vert < 15, jaune < 35, orange < 55, rouge.

Stockage : seul le job `risk_scorer` du scheduler (toutes les heures, premier passage 2 min apres le demarrage) ecrit le score temps reel ; les routes de lecture ne stockent jamais. Points `risk_score` : `level` en champ, tag `model_version=3.3` ; les lectures preferent la version courante, les anciens points sans tag restent lisibles. Le backfill ecrit la meme serie (pas 1 h jusqu'a 7 jours, 3 h jusqu'a 30 jours, 6 h au-dela).

Les entrees different selon le chemin : le temps reel fusionne Open-Meteo/Netatmo/Infoclimat (max) et a vent et sol ; backfill, TimeMachine et replay n'ont que les archives (vent, sol, video indisponibles).

### Previsions

Service dans `backend/app/services/forecast.py` :
- Previsions horaires, 168 h par defaut (`/api/risk/forecast?hours=` de 6 a 336)
- Chaque heure : features projetees -> `compute_risk` ; H+0 reprend les entrees temps reel (meme score que `/api/realtime`)
- Pluie Open-Meteo (AROME/ARPEGE + ECMWF) en UTC, pluie passee calibree sur la pluie temps reel multi-sources (facteur borne a [1/3, 3])
- Niveaux projetes : tendance actuelle (12 h max) + reponse a la pluie tombee apres t0, decalee par `delay_hours` de chaque station
- Nombre de stations en hausse : decroissance lineaire sur 48 h, plus apport de la pluie projetee
- Cache Redis 30 min ; overlay Timeline via `include_forecast=true`

### Stations surveillees

Distances a vol d'oiseau depuis le moulin (haversine sur `lat`/`lon` du config) ; delais et poids de `config.hubeau_stations`. Le champ `distance_km` du config est uniquement affiche et ne correspond pas a ces valeurs.

| Station | Code Hub'Eau | Riviere | Distance | Delai | Poids |
|---------|-------------|---------|----------|-------|-------|
| Libourne | P577001001 | Dordogne | 19 km | 0h | 0% (exclue: maree) |
| Pessac-sur-Dordogne | P555001001 | Dordogne | 7.6 km | 4h | 30% (primaire) |
| Bergerac | P514001001 | Dordogne | 38 km | 8h | 15% |
| Abzac | P726151001 | Isle | 10 km | 2h | 25% |
| Perigueux | P704151001 | Isle | 66 km | 12h | 10% |
| Coutras | P846251001 | Dronne | 21 km | 3h | 15% |
| Riberac | P816251001 | Dronne | 49 km | 10h | 5% |

Pessac (~7 km en amont de la confluence) est la station primaire (`hubeau_primary_station`) et le seul proxy du remous impose a la Lidoire. Isle et Dronne confluent a Libourne, en aval de la Lidoire : indicateurs de pluie regionale uniquement. La station primaire de secours n'est jamais une station de poids 0.

## Sources de donnees

- **Marees** : SHOM (cle API) ou Open-Meteo Marine (PM/BM extraites du niveau marin modele, coefficient estime). Marees estimees jamais stockees, `TideForecast.available=False`.
- **Niveaux rivieres** : Hub'Eau temps reel (30 derniers jours), Hydroportail pour l'historique, Vigicrues pour Libourne (metres)
- **Meteo prevue** : Open-Meteo (modeles AROME/ARPEGE), timestamps UTC
- **Meteo locale** : Netatmo (stations citoyennes du bassin Lidoire), Infoclimat StatIC
- **Sol** : Open-Meteo (humidite, temperature, ET0) + Meteo Agricole (bilan hydrique)
- **Camera** : Flux RTSP configurable via `CAMERA_RTSP_URL`

Jobs du scheduler : rivieres, Netatmo et Infoclimat 15 min, meteo 1 h, marees et Meteo Agricole 6 h, nowcast 5 min, `risk_scorer` 1 h, camera si configuree.

APIs externes hors du chemin des requetes :
- Netatmo, Infoclimat, Meteo Agricole et nowcast ne sont jamais appeles par une route ni par `gather_realtime_inputs` : lecture seule des caches remplis par les jobs (`peek_*`, perimes au-dela de 1 h pour Netatmo/Infoclimat, 15 min pour le nowcast, 12 h pour Meteo Agricole ; au-dela => indisponible, comme un echec).
- Hub'Eau, Open-Meteo, marees et Vigicrues restent en cache-through, timeout 10 s (`EXTERNAL_TIMEOUT_S`) ; les backfills historiques gardent 30-60 s.
- `CircuitBreaker` (`cache.py`) : apres un timeout / erreur de connexion / 5xx, l'API est sautee 15 min (Hub'Eau 5 min, sur un tour sans aucune station), un seul WARNING avec le `repr` de l'exception. Les 4xx n'ouvrent pas le disjoncteur.

### API Hub'Eau

```python
# Temps reel (30 derniers jours)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr?code_entite=P577001001&grandeur_hydro=H

# Historique (depuis 1900)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab?code_entite=P577001001&grandeur_hydro_elab=HXI
```

## Episodes d'inondation

Verite terrain : `backend/app/data/episodes_labels.json` (servie par `GET /api/episodes/labels`, chargee par `services/labels.py`), indexee par date de pic. 14 episodes annotes (periode 2022-11-01 - 2026-01-31) : 5 `flooded`, 6 `false_positive`, 2 `near_miss`, 1 `unknown`. Labels non exhaustifs (les negatifs sont tous des detections du modele).

En base, l'issue est un champ structure (`outcome`, `observed_at`, `water_height_cm`, `outcome_source`) ; les anciens prefixes `[CONFIRMED: ...]` dans `notes` sont migres au demarrage.

```bash
# Labels de reference
GET /api/episodes/labels

# Fixer l'issue d'un episode (remplace la precedente)
POST /api/episodes/confirm/12?outcome=near_miss&observed_at=2024-03-30T12:00:00&source=user

# Analyser une date d'inondation
POST /api/episodes/analyze-v2?date=2024-02-15T14:00:00&severity=5

# Comparer plusieurs episodes
POST /api/episodes/compare?dates=2024-02-15T14:00&dates=2023-12-10T08:00

# Niveaux max par station sur une periode
GET /api/episodes/station-extremes?start_date=2020-01-01&end_date=2024-12-31

# Backfill historique (tache asynchrone, suivi : GET /api/episodes/backfill/status)
POST /api/episodes/backfill?start_date=2022-11-01&end_date=2026-01-28

# Detection automatique (threshold 0-100, defaut 35 ; retenu si >= 6 h au-dessus de 35 ou >= 2 h au-dessus de 55)
# Toute la periode en un appel : lecture Influx par fenetres de 30 jours (DETECTION_CHUNK_DAYS), etat des episodes
# reporte d'une fenetre a l'autre => memoire bornee, resultat identique a une passe unique. Seuls les points
# risk_score model_version=3.3 (et anciens sans tag) sont lus, jamais les series shadow (3.3-gate).
POST /api/episodes/detect?start_date=2022-11-01&end_date=2026-10-26&threshold=45
```

Tous les POST/PUT/DELETE ci-dessus exigent `X-API-Key` quand `API_KEY` est defini.

## Evaluation (`backend/scripts/eval/`)

Replay horaire causal (points <= t uniquement) de v3.3 et de baselines sur les archives publiques, sans InfluxDB. `build` telecharge une fois (~4 min) dans `backend/data/eval_cache/` ; ensuite `replay`/`report` avec `--offline` (lecture du cache en ~15 s). Sorties dans `backend/data/eval_out/` (gitignore). Options : `--merge-gap 12`, `--strict-start`, `--no-tide`, `--sensitivity`, `--threshold orange=35`.

Definitions (`contingency.py`, `hourly.py`) :
- Alerte = suite d'heures consecutives avec score >= seuil. Elle detecte une crue si elle demarre entre pic - 72 h et pic + 6 h (ou est deja en cours a l'ouverture de la fenetre).
- POD = detections / crues ; FAR brut = (negatives labellisees + non labellisees) / alertes jugees ; FAR labellise = negatives labellisees seulement ; CSI = detections / (detections + manques + fausses alertes) ; avance = pic - debut d'alerte.
- AUC horaire : cible "crue dans les 24 h" (125 heures positives, 28 170 negatives).

Interface d'un modele candidat : `name`, `score(row: FeatureRow) -> float | None`, optionnellement `kind` (`score` | `probability`), `thresholds`, `reset()`. Voir la docstring de `scripts/eval/__init__.py`.

Resultats (2022-11-01 a 2026-10-25, 5 crues, alertes non fusionnees) :

| Modele | Protocole | Niveau | Crues | Alertes | FA brutes (lab.+non lab.) | FAR brut / lab. | CSI | Avance med./min (h) | Alertes/an | AUC |
|--------|-----------|--------|-------|---------|------|------|-----|------|------|-----|
| v3.3 | in-sample (~45 constantes) | orange | 5/5 | 241 | 221 (20+201) | 98% / 80% | 0.02 | 63 / 11 | 68.0 | 0.952 [0.89-0.99] |
| v3.3 | in-sample | rouge | 5/5 | 79 | 65 (13+52) | 93% / 72% | 0.07 | 21 / 5 | 23.1 | |
| pessac_level | sans label | orange | 4/5 | 20 | 16 (3+13) | 80% / 43% | 0.19 | 37 / -2 | 5.8 | 0.894 [0.78-0.97] |
| pessac_level | sans label | rouge | 3/5 | 9 | | 67% / 25% | 0.27 | 3 / -4 | 3.0 | |
| rain_72h | sans label | orange | 1/5 | 1 | | | 0.20 | | | 0.912 |
| v3.3 + gate remous | sans label, 0 param. | orange | 5/5 | 74 | 62 (11+51) | 93% / 69% | 0.07 | 20 / 0 | 23.6 | 0.948 [0.88-0.99] |
| v3.3 + gate remous | sans label | rouge | 4/5 | 56 | | | 0.08 | 44 / 11 | | |
| bw_rule_api_cv | LOEO bloque, 2 param. | orange | 3/5 | 8 | 5 (1+4) | 62% / 25% | 0.30 | 12 / 4 | 2.5 | 0.928 [0.86-0.98] |
| lr_gr4h_haval_cv | LOEO bloque, 3 param. | orange | 4/5 | 15 | 10 (2+8) | 71% / 33% | 0.27 | 3 / -2 | 4.0 | 0.909 [0.81-0.98] |

Alertes fusionnees a 12 h (orange) : v3.3 57 alertes / CSI 0.09 / 15.3 par an ; v3.3 + gate remous 21 / CSI 0.24 / 5.8 par an, POD 5/5 ; pessac_level 13 / CSI 0.29 ; bw_rule_api_cv 8 / CSI 0.30 ; lr_gr4h_haval_cv 13 / CSI 0.29.

Precautions : 5 crues (une crue = 20 points de POD, IC 95% de 3/5 = 15-95%, aucun ecart hors echantillon significatif) ; labels non exhaustifs (FAR brut pessimiste) ; v3.3 est in-sample donc optimiste ; le score v3.3 oscille autour de 35 au rythme de la maree (`--no-tide` : 241 -> 122 alertes orange). 2023-03-11 est une crue par infiltration (colmatee depuis).

Periodes d'alerte v3.3 non labellisees a faire annoter : 2023-11-02 -> 11-06, 2023-11-09 -> 11-15, 2024-01-18 -> 01-19, 2024-02-10 -> 02-13, 2024-03-02 -> 03-04, 2024-05-14 -> 05-16, 2024-05-20 -> 05-23, 2024-11-21 -> 11-22, 2025-01-27 -> 01-29 (liste complete : `unlabelled_alerts.csv`).

`app/services/models_experimental.py` (gate remous, regles backwater, regression logistique, GR4H) n'est importe par aucun code de production : un test (`test_models_experimental_not_imported_by_production`) l'impose.

## Tooltips pedagogiques

Systeme de tooltips interactifs sur toutes les metriques du dashboard :
- **Composant** : `InfoTooltip.tsx` (hover desktop, tap mobile, portal pour z-index)
- **Dictionnaire** : `tooltipContent.ts` (entrees centralisees avec liens externes)

Pour ajouter un tooltip : ajouter une entree dans `TOOLTIPS` (tooltipContent.ts) puis `<InfoTooltip {...TOOLTIPS.maCle} />` dans le composant.

## Services Docker

| Service | Port hote | Description |
|---------|-----------|-------------|
| backend | 8100 | API FastAPI (uid 10001) |
| frontend | 3100 | React (nginx-unprivileged, 8080 dans le conteneur) |
| postgres | - | PostgreSQL 15, reseau interne uniquement |
| influxdb | - | InfluxDB 2.7, reseau interne uniquement |
| redis | - | Redis 7 avec mot de passe, reseau interne uniquement |

Ports lies a `${BIND_ADDRESS:-127.0.0.1}`. Pour exposer les bases en local : `docker-compose.override.yml` (gitignore, exemple en tete de `docker-compose.yml`) sur 127.0.0.1:5532 / 8186 / 6479.

## Deploiement Kubernetes

Chart `deploy/helm/lidoire-flood-watch/` (cluster Scaleway Kapsule `sharedhosting`, contexte `admin@sharedhosting-production`, toujours en fin de commande). Images GHCR `ghcr.io/clark42/lidoire-flood-watch-{backend,frontend}` construites par `.github/workflows/images.yml` (push `main` / tags `v*` : amd64 + arm64, tags `sha-<7>`, `main`, semver ; PR : build amd64 sans push).

- Prerequis : stack Terraform `11-lidoire` appliquee (namespace `lidoire` PSS restricted, NetworkPolicies, secrets Scaleway `lidoire-generated`, `lidoire-external-apis`, `lidoire-backup-s3`) et images publiees.
- Deploiement : `helm upgrade --install lidoire deploy/helm/lidoire-flood-watch -n lidoire --set image.tag=sha-<7> --kube-context admin@sharedhosting-production`. Verification : `helm lint` + `helm template ... | kubeconform -strict -ignore-missing-schemas -kubernetes-version 1.35.0 -summary`.
- Le chart ne cree ni namespace ni NetworkPolicy. Noms de Services fixes (`backend` impose par `frontend/nginx.conf`) : une release par namespace.
- Secrets : ExternalSecrets (`ClusterSecretStore` `scaleway-secret-manager`, `external-secrets.io/v1beta1`, cle `name:<secret>`). `lidoire-generated` (mots de passe bruts, sans `API_KEY`) et `lidoire-backend` (`DATABASE_URL`, `REDIS_URL` assembles par le template ESO avec `urlquery`, `INFLUXDB_TOKEN` = jeton admin, `API_KEY`). `INFLUXDB_ADMIN_*` jamais passes au backend. `lidoire-external-apis` derriere `externalApis.enabled` (envFrom optionnel).
- Backend : 1 replique, `Recreate` obligatoire (APScheduler), sondes `/api/health`, initContainer d'attente TCP des dependances, PVC `backend-data` 1 Gi (`helm.sh/resource-policy: keep`). StatefulSets `postgres` (5 Gi) et `influxdb` (10 Gi) sur `sbs-default-retain`, Redis sans persistance.
- PSS restricted partout, rootfs en lecture seule + emptyDir : uid 10001 (backend), 101 (nginx), 70 (postgres, `PGDATA` en sous-dossier), 1000 (influxdb, entrypoint non root), 999/1000 (redis).
- Acces `http://lidoire.ts.kamorion.cloud` via tailnet (Service `frontend` ClusterIP fixe `10.32.0.100`) ; Ingress optionnel desactive.
- Installation neuve : reconstituer via `POST /api/episodes/backfill?start_date=2022-11-01&end_date=<aujourd'hui>` avec `X-API-Key` (Secret `lidoire-backend`, cle `API_KEY`).
- Sauvegarde : CronJob `backup` (pg_dump + `influx backup`, upload rclone vers S3 Scaleway, expiration a 30 jours par la regle de cycle de vie du bucket, le Job ne supprime rien : `backup.retentionDays: 0`). Restauration (`pg_restore`, `influx restore --full`) : voir README.

## Securite

- `POST`/`PUT`/`PATCH`/`DELETE` sur `/api/*` : en-tete `X-API-Key` compare en temps constant a `API_KEY` (401 sinon). `GET` public. `API_KEY` vide = mode dev (avertissement au demarrage). Le backend refuse de demarrer si un secret vaut un placeholder connu (`change-me`, ...).
- Frontend : cle stockee dans localStorage `lfw_api_key`, envoyee par `fetchJson` sur les methodes d'ecriture.
- `GET /api/auth/netatmo` exige `?api_key=` quand `API_KEY` est defini (navigation navigateur, pas d'en-tete possible).
- CORS : liste explicite (`CORS_ORIGINS` + `FRONTEND_URL`), pas de joker, `X-Backfill-Pending` expose.
- InfluxDB : `INFLUXDB_TOKEN` limite au bucket recommande, `INFLUXDB_READ_TOKEN` en lecture seule pour les requetes.

### Contrats d'API a respecter

- `/api/timeline/unified` : 422 si `start >= end`, periode > 400 jours ou id inconnu (detail `{error, unknown, valid}`) ; attente max 5 s des backfills, ceux en cours listes dans `X-Backfill-Pending`.
- `/api/status` : `services.database|influxdb|redis` = `connected` | `error` | `timeout` (2 s), detail dans `services.checks`, `status` = `healthy` | `degraded`. `sources.{river_primary, rain_openmeteo, infoclimat, netatmo, tide, soil, risk_score}` = fraicheur du dernier point stocke (`last()` Flux sur 30 jours, cache 60 s) : `last_timestamp`, `age_hours`, `max_age_hours`, `critical`, `status` = `ok` | `stale` | `unavailable` (aucun point en 30 j) | `unknown` (requete en echec ou Influx down, non cache), `note` (non configure, jeton Netatmo absent). Seuils : Pessac 3 h (`risk_level_max_age_hours`), risk_score v3.3 temps reel 2 h, Netatmo 1 h, Open-Meteo/Infoclimat/sol 3 h, maree : la derniere PM/BM stockee doit etre dans le futur. `degraded` si une dependance n'est pas `connected` OU si une source critique (`river_primary`, `risk_score`) est `stale`/`unavailable` ; les autres sources sont informatives. `services.collectors` reste une declaration statique (compatibilite).
- `/api/episodes/confirm/{id}` : `outcome` (`flooded`, `false_positive`, `near_miss`, `unknown`) ou ancien `was_flooded` ; remplace l'issue precedente.

## Variables d'environnement

Fichier `.env` a la racine (voir `.env.example`) :
- Obligatoires pour compose : `POSTGRES_PASSWORD`, `INFLUXDB_ADMIN_PASSWORD`, `INFLUXDB_ADMIN_TOKEN`, `REDIS_PASSWORD` (vides dans `.env.example`, donc compose echoue tant qu'ils ne sont pas remplis)
- `API_KEY` (`openssl rand -hex 32`), `BIND_ADDRESS`, `CORS_ORIGINS`, `FRONTEND_URL`
- `INFLUXDB_TOKEN` (optionnel, sinon jeton admin), `INFLUXDB_READ_TOKEN` (optionnel), `INFLUXDB_ORG`, `INFLUXDB_BUCKET`
- `DATABASE_URL`, `INFLUXDB_URL`, `REDIS_URL` : construits par compose, a definir seulement hors Docker
- `SHOM_API_KEY`, `METEOFRANCE_API_KEY`, `INFOCLIMAT_API_KEY` (optionnels)
- `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` (optionnels)
- `CAMERA_RTSP_URL` (optionnel), `LOG_LEVEL`

Installation existante : conserver l'ancien mot de passe Postgres et l'ancien jeton Influx (`INFLUXDB_ADMIN_TOKEN` = ancien `INFLUXDB_TOKEN`) pour les volumes existants, puis `docker compose run --rm --user root backend chown -R 10001:10001 /app/data`. Detail dans le README.

## Pistes

- Capteur de niveau au moulin (seule mesure directe possible).
- Pluie radar (COMEPHORE, ANTILOPE) comme entree d'un modele pluie-debit.
- Gate de remous sur v3.3 en mode shadow pendant une saison, apres labellisation des periodes ci-dessus.
- Regression logistique / GR4H seulement quand davantage d'episodes seront labellises.
