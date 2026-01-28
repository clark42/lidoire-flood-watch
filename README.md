# Lidoire Flood Watch

Surveillance des niveaux d'eau de la Lidoire au moulin (coordonnees 44.8759811, 0.0015091) en croisant marees, niveaux de rivieres, meteo et analyse video.

## Architecture

```
lidoire-flood-watch/
├── backend/                    # API FastAPI + services de collecte
│   ├── app/
│   │   ├── api/                # Routes (realtime, timeline, risk, episodes)
│   │   ├── models/             # Schemas Pydantic
│   │   └── services/
│   │       ├── risk_v2.py      # Modele de risque v3.0
│   │       ├── forecast.py     # Previsions 7 jours
│   │       ├── backfill.py     # Backfill historique (rivers, rain, tides, risk)
│   │       ├── episode_detector.py  # Detection automatique d'episodes
│   │       ├── data_store.py   # Acces InfluxDB/PostgreSQL
│   │       └── collectors/     # Collecteurs (hubeau, tide, weather, netatmo, infoclimat)
│   ├── analyze_episodes.py     # Script d'analyse des episodes annotes
│   └── validate_model.py       # Script de validation du modele
├── frontend/                   # Application React + Vite + TailwindCSS
│   └── src/
│       ├── pages/              # Dashboard, Timeline, Episodes
│       └── components/         # RiskGauge, ForecastPanel, HydroMap, TimeMachine...
├── docker-compose.yml
└── README.md
```

## Stack technique

- **Backend** : FastAPI (Python 3.11+)
- **Frontend** : React + Vite + TailwindCSS + Recharts
- **Base de donnees** : InfluxDB 2.7 (time-series), PostgreSQL 15 (episodes)
- **Cache** : Redis

## Fonctionnalites

### Dashboard temps reel
- Score de risque 0-100 avec jauge visuelle et detail des facteurs
- Niveaux de 7 stations hydrometriques avec tendance et vitesse (cm/h)
- Pluviometrie multi-sources (Netatmo, Infoclimat, Open-Meteo)
- Marees (coefficient, phase, prochaine PM/BM)
- Conditions du sol (humidite, saturation, bilan hydrique)
- Carte interactive des stations (HydroMap)
- Tooltips pedagogiques sur toutes les metriques

### Previsions 7 jours
- Previsions horaires du score de risque sur 168h
- Basees sur : pluie Open-Meteo (AROME/ARPEGE), marees predites, projection des niveaux amont
- Integrees dans le graphique de prevision ET dans la timeline
- Cache Redis (30 min TTL)

### Timeline historique multi-sources
- Superposition de series : niveaux riviere, pluie, marees, score de risque, sol, vent
- Selecteur de sources (stations, types de donnees)
- Overlay des previsions 7j en pointilles sur la timeline
- Resolution configurable (15min, 1h, 6h, 1j)

### TimeMachine
- Navigation temporelle vers n'importe quelle date historique
- Reconstruction complete du dashboard a la date choisie
- Acces rapide aux episodes d'inondation connus

### Episodes d'inondation
- 14 episodes annotes (nov 2022 - jan 2026)
- Analyse contextuelle : pluie avant, niveaux max, coefficient maree, phase
- Confirmation/rejet pour la calibration du modele
- Detection automatique par seuil de score

### Backfill historique
- Recompute complet des scores de risque sur tout l'historique
- Backfill selectif (rivers, rainfall, tides, soil, risk)
- Tache asynchrone en arriere-plan avec suivi de progression

## Modele de risque v3.2

Calibre a partir de 14 episodes annotes (5 inondations confirmees, 9 faux positifs).

**Constat cle** : le moulin est sur la Lidoire (affluent), PAS sur la Dordogne. Un niveau Dordogne eleve seul ne signifie pas inondation au moulin. Le signal le plus fort est la montee simultanee de plusieurs stations + pluie, OU plusieurs stations au-dessus du seuil orange (indicateur de pluie en amont).

### Facteurs et poids

| Facteur | Poids | Description |
|---------|-------|-------------|
| Niveaux rivieres (multi-stations) | 28% | Moyenne ponderee par distance/delai |
| Pluie 24h | 10% | Cumul precipitations dernieres 24h |
| Pluie 72h | 12% | Cumul precipitations 3 jours |
| Pluie 7j | 5% | Saturation longue duree |
| Tendance | 12% | Vitesse de montee (cm/h) |
| Momentum | 8% | Nombre de stations en hausse simultanee |
| Coefficient maree | 5% | Coefficient de la maree |
| Phase maree | 5% | Proximite de la pleine mer |
| Saturation du sol | 5% | Facteur de ruissellement |
| Vent d'ouest | 3% | Amplification surcote Gironde |
| Analyse video | 5% | Index niveau d'eau camera |

### Mecanismes avances

- **Multiplicateur de conjonction** (1.0 a 2.5x) :
  - Se declenche quand plusieurs stations montent ET qu'il pleut (pattern "crue active")
  - **Multi-stations au-dessus du seuil** (v3.2) : si ≥3 stations actives ont un score > 0.6 (au-dessus du seuil orange), le multiplicateur augmente (+0.45 si ≥1 en hausse, +0.30 sinon). Les niveaux eleves SONT le signal de pluie en amont.
  - **Drainage impairment** (v3.1) : quand la Dordogne est haute + pluie cumulative, la Lidoire ne peut pas s'ecouler → inondation par refoulement
- **Amortisseur de stabilite** (0.65 a 1.0x) : reduit le score quand la riviere est haute mais stable/en baisse (piege les faux positifs). Desactive si ≥3 stations au-dessus du seuil orange ou si pluie 72h significative.
- **Seuillage du momentum** : gate par le niveau riviere (niveau < 0.30m → momentum × 0.2)

### Seuils d'alerte

| Niveau | Score | Signification |
|--------|-------|---------------|
| Vert | 0-15 | Pas de risque |
| Jaune | 15-35 | Vigilance |
| Orange | 35-55 | Risque eleve |
| Rouge | 55+ | Danger imminent |

### Stations surveillees

| Station | Code Hub'Eau | Riviere | Distance | Delai | Poids |
|---------|-------------|---------|----------|-------|-------|
| Libourne | P577001001 | Dordogne | 19 km | 0h | 0% (exclue: influence maree) |
| Pessac-sur-Dordogne | P555001001 | Dordogne | 25 km | 4h | 35% (station primaire) |
| Bergerac | P514001001 | Dordogne | 60 km | 8h | 10% |
| Abzac | P726151001 | Isle | 10 km | 2h | 15% |
| Perigueux | P704151001 | Isle | 65 km | 12h | 5% |
| Coutras | P846251001 | Dronne | 21 km | 3h | 10% |
| Riberac | P816251001 | Dronne | 50 km | 10h | 5% |

> Libourne est exclue du calcul de risque (poids 0%) car ses oscillations de maree ~4m masquent le signal de crue. Elle reste affichee pour reference.

## Sources de donnees

### Marees
- **Open-Meteo Marine** : API gratuite pour les donnees de niveau marin
- **SHOM** : API officielle (necessite cle API)
- Fallback : calcul predictif

### Niveaux rivieres
- **Hub'Eau** : API nationale (7 stations, temps reel + historique depuis 1900)
- Pattern cache-through avec InfluxDB

### Precipitations
- **Netatmo** : Reseau de stations meteo citoyennes (OAuth2)
  - Priorite aux stations du bassin Lidoire (amont/milieu/aval)
  - Moyenne ponderee par distance
- **Infoclimat** : Reseau StatIC (cle API requise)
- **Open-Meteo** : Previsions AROME/ARPEGE (gratuit)

### Conditions du sol
- **Open-Meteo** : Humidite du sol, temperature, evapotranspiration
- **Meteo Agricole** : Bilan hydrique 30j, deficit 12 mois

## Stockage des donnees

Toutes les donnees sont stockees de maniere permanente dans InfluxDB :

| Measurement | Contenu | Volume |
|-------------|---------|--------|
| `river_level` | Niveaux des 7 stations | ~2000 pts/station/semaine |
| `tide` | Hauteur, coefficient, type PM/BM | ~100 pts/jour |
| `rainfall` | Precipitations mm/h par source | ~200 pts/jour |
| `risk_score` | Score de risque + facteurs detailles | ~24 pts/jour |
| `soil_conditions` | Humidite, saturation, bilan | ~24 pts/jour |

### Pattern cache-through

1. Lecture d'abord dans InfluxDB
2. Si absent → fetch depuis l'API externe
3. Stockage permanent dans InfluxDB
4. Les donnees historiques ne sont jamais re-fetchees

## Installation

### Prerequis

- Docker et Docker Compose
- Node.js 18+ (pour le developpement frontend)
- Python 3.11+ (pour le developpement backend)

### Lancement avec Docker

```bash
docker compose up -d
```

### Developpement local

Backend :
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Frontend :
```bash
cd frontend
npm install
npm run dev
```

## Ports (offset +100 pour eviter conflits)

| Service | Port |
|---------|------|
| Frontend | 3100 |
| Backend API | 8100 |
| PostgreSQL | 5532 |
| InfluxDB | 8186 |
| Redis | 6479 |

## Configuration

Copier `.env.example` vers `.env` et configurer :

```bash
# Base de donnees (ports avec offset +100)
DATABASE_URL=postgresql://user:pass@localhost:5532/lidoire
INFLUXDB_URL=http://localhost:8186
INFLUXDB_TOKEN=your-token
INFLUXDB_ORG=lidoire
INFLUXDB_BUCKET=flood_data

# APIs externes
SHOM_API_KEY=           # Optionnel - marees officielles
NETATMO_CLIENT_ID=      # Requis pour precipitations Netatmo
NETATMO_CLIENT_SECRET=
NETATMO_REFRESH_TOKEN=
INFOCLIMAT_API_KEY=     # Optionnel - stations Infoclimat

# Coordonnees du moulin
MOULIN_LAT=44.8759811
MOULIN_LON=0.0015091
```

## API Endpoints

### Status
- `GET /api/health` : Healthcheck
- `GET /api/status` : Status detaille des sources de donnees

### Temps reel
- `GET /api/realtime` : Toutes les donnees temps reel (risk, tide, river, rainfall, soil, wind)
- `GET /api/realtime/at?timestamp=<iso>` : Reconstruction du dashboard a une date historique
- `GET /api/realtime/stations` : Donnees multi-stations detaillees
- `GET /api/realtime/netatmo` : Donnees Netatmo brutes
- `GET /api/realtime/infoclimat` : Stations Infoclimat
- `GET /api/realtime/sources` : Status de chaque source de donnees
- `GET /api/realtime/basin-rainfall` : Pluviometrie aggregee bassin Lidoire

### Timeline
- `GET /api/timeline/unified` : Timeline multi-sources (river, rain, tide, risk, soil, wind, forecast)
- `GET /api/timeline/sources` : Sources disponibles (stations, types)
- `GET /api/timeline/rainfall` : Timeline pluviometrique detaillee

### Risque
- `GET /api/risk` : Score de risque actuel
- `GET /api/risk/forecast?hours=168` : Previsions de risque (defaut 7 jours)
- `POST /api/risk/simulate` : Simulation avec valeurs custom

### Episodes
- `GET /api/episodes` : Liste des episodes d'inondation
- `POST /api/episodes` : Creer un episode
- `GET /api/episodes/{id}` : Detail d'un episode
- `PUT /api/episodes/{id}` : Modifier un episode
- `DELETE /api/episodes/{id}` : Supprimer un episode
- `POST /api/episodes/analyze-v2` : Analyser une date d'inondation
- `GET /api/episodes/analyze/{id}` : Analyser un episode existant
- `POST /api/episodes/confirm/{id}` : Confirmer/rejeter (calibration modele)
- `POST /api/episodes/compare` : Comparer plusieurs episodes
- `GET /api/episodes/station-extremes` : Niveaux max par station sur une periode

### Backfill
- `POST /api/episodes/backfill` : Lancer un backfill historique (tache asynchrone)
- `GET /api/episodes/backfill/status` : Progression du backfill en cours
- `POST /api/episodes/detect` : Detection automatique d'episodes par seuil

## Licence

Projet prive - Usage personnel
