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
│   ├── realtime.py      # /api/realtime - données temps réel
│   ├── timeline.py      # /api/timeline - données historiques
│   ├── episodes.py      # /api/episodes - gestion épisodes d'inondation
│   └── risk.py          # /api/risk - score de risque
├── models/
│   └── schemas.py       # Tous les modèles Pydantic (TideData, RiskScore, etc.)
└── services/
    ├── risk.py          # Calcul du score de risque pondéré
    ├── data_store.py    # Accès InfluxDB/PostgreSQL
    ├── scheduler.py     # APScheduler pour collecte périodique
    ├── episode_analyzer.py  # Analyse contexte hydro-météo des épisodes
    ├── video_analyzer.py    # Détection niveau d'eau via caméra
    └── collectors/          # Collecteurs de données externes
        ├── tide.py          # SHOM / maree.info (marées)
        ├── vigicrues.py     # Vigicrues (niveaux Dordogne)
        └── weather.py       # Open-Meteo (pluie prévue/observée)
```

### Frontend (`frontend/src/`)

```
src/
├── App.tsx              # Router principal, navigation
├── api.ts               # Client API backend
├── types.ts             # Types TypeScript (miroir des schemas backend)
├── pages/
│   ├── Dashboard.tsx    # Vue temps réel avec score de risque
│   ├── Timeline.tsx     # Graphiques historiques (Recharts)
│   ├── Episodes.tsx     # Liste/création épisodes
│   └── Configuration.tsx
├── tooltipContent.ts    # Dictionnaire centralisé des textes de tooltips
└── components/
    ├── RiskGauge.tsx        # Jauge de risque visuelle
    ├── RiskBreakdownPanel.tsx  # Détail du calcul de risque (facteurs, poids, stations)
    ├── StationsPanel.tsx    # Panneau stations hydrométriques (par rivière/distance)
    ├── RainfallPanel.tsx    # Pluviométrie multi-sources (Netatmo, Infoclimat, prévisions)
    ├── ForecastPanel.tsx    # Prévisions de risque (timeline + tableau)
    ├── BasinRainfallGauge.tsx  # Jauge pluie bassin Lidoire (Amont/Milieu/Aval)
    ├── HydroMap.tsx         # Carte interactive des stations
    ├── MetricSparkline.tsx  # Mini-graphiques temps réel
    ├── TimeMachineBanner.tsx # Navigation temporelle (replay épisodes)
    ├── InfoTooltip.tsx      # Tooltip pédagogique réutilisable (hover/tap)
    ├── DataCard.tsx         # Carte de données générique
    └── DataSourcesStatus.tsx # Statut des sources de données
```

## Modèle de risque v2.3 (multi-stations, calibré fév. 2024)

Le score de risque (0-100) est calculé dans `backend/app/services/risk_v2.py` avec :

| Facteur | Poids |
|---------|-------|
| Niveaux rivières (multi-stations, Libourne exclue) | 42% |
| Pluie 24h | 8% |
| Pluie 72h | 10% |
| Pluie 7j | 5% |
| Coefficient marée | 8% |
| Phase marée | 8% |
| Tendance (vitesse montée) | 5% |
| Saturation sol | 5% |
| Vent d'ouest (surcote) | 3% |
| Niveau vidéo | 5% |

**Multiplicateur de conjonction** (1.0 à 1.6x) : appliqué quand marée haute + niveau élevé + tendance hausse + élévation multi-stations simultanée.

v2.3 (jan 2026) : calibré sur l'épisode de février 2024 (sévérité 5, moulin inondé).
- Poids rain_72h/7d augmentés : la pluie soutenue multi-jours (62mm/5j) a causé l'inondation malgré une pluie 24h modérée (34mm).
- Facteur multi-station ajouté à la conjonction : toutes les stations simultanément au-dessus du seuil jaune est un signal critique.
- Saturation sol renforcée : les sols d'hiver gorgés amplifient massivement le ruissellement.

### Stations surveillées

| Station | Code Hub'Eau | Rivière | Distance | Délai | Poids |
|---------|--------------|---------|----------|-------|-------|
| Libourne | P577001001 | Dordogne | 5km | 0h | 0% (exclu: marée) |
| Pessac-sur-Dordogne | P555001001 | Dordogne | 30km | 4h | 30% |
| Bergerac | P514001001 | Dordogne | 60km | 8h | 15% |
| Abzac | P726151001 | Isle | 15km | 2h | 25% |
| Périgueux | P704151001 | Isle | 80km | 12h | 10% |
| Coutras | P846251001 | Dronne | 25km | 3h | 15% |
| Ribérac | P816251001 | Dronne | 70km | 10h | 5% |

## Sources de données

- **Marées** : SHOM / maree.info (Bordeaux, Libourne)
- **Niveaux rivières** : Hub'Eau API (7 stations Dordogne/Isle/Dronne)
- **Météo prévue** : Open-Meteo (modèles AROME/ARPEGE)
- **Météo locale** : Netatmo (stations citoyennes dans un rayon de 25km)
- **Météo locale** : Infoclimat StatIC (stations amateur complémentaires)
- **Caméra** : Flux RTSP configurable via `CAMERA_RTSP_URL`

### API Hub'Eau

```python
# Temps réel (30 derniers jours)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/observations_tr?code_entite=P577001001&grandeur_hydro=H

# Historique (depuis 1900)
https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab?code_entite=P577001001&grandeur_hydro_elab=HXI
```

## Analyse d'épisodes historiques

Endpoints pour analyser les inondations passées :

```bash
# Analyser une date d'inondation
POST /api/episodes/analyze-v2?date=2024-01-15T14:00:00&severity=4

# Comparer plusieurs épisodes
POST /api/episodes/compare?dates=2024-01-15T14:00&dates=2023-12-10T08:00

# Niveaux max par station sur une période
GET /api/episodes/station-extremes?start_date=2020-01-01&end_date=2024-12-31
```

## Tooltips pédagogiques

Système de tooltips interactifs sur toutes les métriques du dashboard :
- **Composant** : `InfoTooltip.tsx` (hover desktop, tap mobile, portal pour z-index)
- **Dictionnaire** : `tooltipContent.ts` (~25 entrées centralisées avec liens externes)
- **Couverture** : cm/h, coefficient marée, BM/PM, seuils J/O/R, poids stations, délai propagation, pluie 24h/72h, score de risque, multiplicateur conjonction, sources Netatmo/Infoclimat

Pour ajouter un tooltip : ajouter une entrée dans `TOOLTIPS` (tooltipContent.ts) puis `<InfoTooltip {...TOOLTIPS.maClé} />` dans le composant.

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
