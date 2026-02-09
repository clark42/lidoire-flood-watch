# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

Lidoire Flood Watch - Surveillance des niveaux d'eau de la Lidoire au moulin (44.8759811, 0.0015091) en croisant marées, niveaux de rivières, météo et analyse vidéo.

## Commandes de développement

### Lancement complet (Docker)
```bash
docker-compose up -d
```

### Backend (FastAPI + Python 3.11+)
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload          # Port 8000
pytest                                  # Tests
pytest -k "test_name"                   # Test unitaire spécifique
```

### Frontend (React + Vite + TypeScript)
```bash
cd frontend
npm install
npm run dev                             # Port 5173
npm run build                           # Build production
npm run lint                            # ESLint
```

## Architecture

### Backend (`backend/app/`)

```
app/
├── main.py              # FastAPI app, lifespan, CORS
├── config.py            # Settings Pydantic (env vars, seuils de risque)
├── api/                 # Routes FastAPI
│   ├── status.py        # /api/status - healthcheck
│   ├── realtime.py      # /api/realtime - donnees temps reel + TimeMachine /at
│   ├── timeline.py      # /api/timeline - donnees historiques + forecast overlay
│   ├── episodes.py      # /api/episodes - gestion episodes + backfill + detection
│   └── risk.py          # /api/risk - score de risque + previsions
├── models/
│   └── schemas.py       # Tous les modeles Pydantic (TideData, RiskScore, etc.)
└── services/
    ├── risk.py          # Calcul du score de risque v1 (legacy)
    ├── risk_v2.py       # Modele de risque v3.0 (multi-stations, momentum, dampener)
    ├── forecast.py      # Previsions 7 jours (pluie + maree + niveaux projetes)
    ├── backfill.py      # Backfill historique (rivers, rain, tides, soil, risk)
    ├── episode_detector.py  # Detection automatique d'episodes par seuil
    ├── data_store.py    # Acces InfluxDB/PostgreSQL
    ├── scheduler.py     # APScheduler pour collecte periodique
    ├── episode_analyzer.py  # Analyse contexte hydro-meteo des episodes
    ├── video_analyzer.py    # Detection niveau d'eau via camera
    └── collectors/          # Collecteurs de donnees externes
        ├── tide.py          # SHOM / maree.info (marees)
        ├── hubeau.py        # Hub'Eau API (niveaux rivieres)
        ├── weather.py       # Open-Meteo (pluie prevue/observee)
        ├── netatmo.py       # Netatmo (stations citoyennes)
        ├── infoclimat.py    # Infoclimat StatIC
        └── meteo_agricole.py # Meteo Agricole (bilan hydrique)
```

### Frontend (`frontend/src/`)

```
src/
├── App.tsx              # Router principal, navigation
├── api.ts               # Client API backend (toutes les fonctions fetch)
├── types.ts             # Types TypeScript (miroir des schemas backend)
├── pages/
│   ├── Dashboard.tsx    # Vue temps reel avec score de risque + TimeMachine
│   ├── Timeline.tsx     # Graphiques historiques multi-sources + forecast overlay
│   ├── Episodes.tsx     # Liste/creation/analyse episodes
│   └── Configuration.tsx
├── tooltipContent.ts    # Dictionnaire centralise des textes de tooltips (~26 entrees)
└── components/
    ├── RiskGauge.tsx            # Jauge de risque visuelle
    ├── RiskBreakdownPanel.tsx   # Detail du calcul (facteurs, poids, momentum, stations)
    ├── StationsPanel.tsx        # Panneau stations hydrometriques
    ├── RainfallPanel.tsx        # Pluviometrie multi-sources
    ├── ForecastPanel.tsx        # Previsions 7 jours (graphique + tableau)
    ├── BasinRainfallGauge.tsx   # Jauge pluie bassin Lidoire (Amont/Milieu/Aval)
    ├── SoilConditionsPanel.tsx  # Conditions du sol (humidite, bilan hydrique)
    ├── NowcastRainChart.tsx     # Pluie nowcast (60 min)
    ├── HydroMap.tsx             # Carte interactive des stations
    ├── MetricSparkline.tsx      # Mini-graphiques temps reel
    ├── TimeMachineBanner.tsx    # Navigation temporelle (replay episodes)
    ├── TimelineSourceSelector.tsx # Selecteur de sources timeline (+ forecast toggle)
    ├── InfoTooltip.tsx          # Tooltip pedagogique reutilisable (hover/tap)
    ├── DataCard.tsx             # Carte de donnees generique
    └── DataSourcesStatus.tsx    # Statut des sources de donnees
```

## Modele de risque v3.2 (calibre sur 14 episodes annotes)

Le score de risque (0-100) est calcule dans `backend/app/services/risk_v2.py`.

Constat cle : le moulin est sur la Lidoire (affluent), PAS sur la Dordogne.
Un niveau Dordogne eleve seul ne signifie pas inondation au moulin.
Le signal le plus fort est la montee simultanee de plusieurs stations + pluie.

| Facteur | Poids | Description |
|---------|-------|-------------|
| Niveaux rivieres (multi-stations) | 28% | Moyenne ponderee, Libourne exclue |
| Pluie 24h | 10% | Cumul precipitations |
| Pluie 72h | 13% | Saturation 3 jours |
| Pluie 7j | 5% | Saturation longue duree |
| Tendance | 13% | Vitesse de montee cm/h |
| Momentum | 8% | Stations en hausse simultanee |
| Coefficient maree | 5% | Coefficient maree |
| Phase maree | 5% | Proximite pleine mer |
| Saturation sol | 5% | Facteur ruissellement |
| Vent d'ouest | 3% | Surcote Gironde |
| Analyse video | 5% | Index niveau camera |

**Multiplicateur de conjonction** (1.0 a 2.5x) : "crue active" = plusieurs stations montent + pluie.
**Amortisseur de stabilite** (0.65 a 1.0x) : riviere haute mais stable/baisse → score reduit.
**Gate momentum** : score riviere < 0.30 → momentum × 0.2 (score normalise, pas metres).

### Previsions 7 jours

Service dans `backend/app/services/forecast.py` :
- Previsions horaires sur 168h (7 jours)
- Sources : pluie Open-Meteo (AROME/ARPEGE), marees predites, projection niveaux amont
- Momentum decay lineaire sur 48h (pas de prediction du nombre de stations en hausse)
- Cache Redis 30 min TTL
- Integrable dans la Timeline via `include_forecast=true`

### Stations surveillees

| Station | Code Hub'Eau | Riviere | Distance | Delai | Poids |
|---------|-------------|---------|----------|-------|-------|
| Libourne | P577001001 | Dordogne | 5km | 0h | 0% (exclue: maree) |
| Pessac-sur-Dordogne | P555001001 | Dordogne | 30km | 4h | 30% (primaire) |
| Bergerac | P514001001 | Dordogne | 60km | 8h | 15% |
| Abzac | P726151001 | Isle | 15km | 2h | 25% |
| Perigueux | P704151001 | Isle | 80km | 12h | 10% |
| Coutras | P846251001 | Dronne | 25km | 3h | 15% |
| Riberac | P816251001 | Dronne | 70km | 10h | 5% |

## Sources de donnees

- **Marees** : SHOM / maree.info (Bordeaux, Libourne) + Open-Meteo Marine
- **Niveaux rivieres** : Hub'Eau API (7 stations Dordogne/Isle/Dronne)
- **Meteo prevue** : Open-Meteo (modeles AROME/ARPEGE)
- **Meteo locale** : Netatmo (stations citoyennes, rayon 25km, bassin Lidoire)
- **Meteo locale** : Infoclimat StatIC (stations amateur complementaires)
- **Sol** : Open-Meteo (humidite, temperature, ET0) + Meteo Agricole (bilan hydrique)
- **Camera** : Flux RTSP configurable via `CAMERA_RTSP_URL`

### API Hub'Eau

```python
# Temps reel (30 derniers jours)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr?code_entite=P577001001&grandeur_hydro=H

# Historique (depuis 1900)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab?code_entite=P577001001&grandeur_hydro_elab=HXI
```

## Episodes d'inondation

14 episodes annotes (nov 2022 - jan 2026), dont 5 inondations confirmees.

```bash
# Analyser une date d'inondation
POST /api/episodes/analyze-v2?date=2024-02-15T14:00:00&severity=5

# Comparer plusieurs episodes
POST /api/episodes/compare?dates=2024-02-15T14:00&dates=2023-12-10T08:00

# Niveaux max par station sur une periode
GET /api/episodes/station-extremes?start_date=2020-01-01&end_date=2024-12-31

# Backfill historique (tache asynchrone)
POST /api/episodes/backfill?start_date=2022-11-01&end_date=2026-01-28

# Detection automatique d'episodes
POST /api/episodes/detect?start_date=2022-11-01&end_date=2025-12-31&threshold=45
```

## Tooltips pedagogiques

Systeme de tooltips interactifs sur toutes les metriques du dashboard :
- **Composant** : `InfoTooltip.tsx` (hover desktop, tap mobile, portal pour z-index)
- **Dictionnaire** : `tooltipContent.ts` (~26 entrees centralisees avec liens externes)
- **Couverture** : cm/h, coefficient maree, BM/PM, seuils J/O/R, poids stations, delai propagation, pluie 24h/72h, score de risque, momentum, multiplicateur conjonction, sources Netatmo/Infoclimat

Pour ajouter un tooltip : ajouter une entree dans `TOOLTIPS` (tooltipContent.ts) puis `<InfoTooltip {...TOOLTIPS.maCle} />` dans le composant.

## Services Docker (ports offset +100)

| Service | Port | Description |
|---------|------|-------------|
| backend | 8100 | API FastAPI |
| frontend | 3100 | React (nginx) |
| postgres | 5532 | PostgreSQL 15 |
| influxdb | 8186 | InfluxDB 2.7 (time-series) |
| redis | 6479 | Cache |

## Variables d'environnement

Fichier `.env` à la racine (voir `.env.example`) :
- `DATABASE_URL` : PostgreSQL
- `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET`
- `REDIS_URL`
- `SHOM_API_KEY`, `METEOFRANCE_API_KEY` (optionnel)
- `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET` (optionnel, pour données météo locales)
- `CAMERA_RTSP_URL` (optionnel)
