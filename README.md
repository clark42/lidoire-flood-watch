# Lidoire Flood Watch

Surveillance des niveaux d'eau de la Lidoire au moulin (coordonnées 44.8759811, 0.0015091) en croisant marées, niveaux de rivières, météo et analyse vidéo.

## Architecture

```
lidoire-flood-watch/
├── backend/           # API FastAPI + services de collecte
├── frontend/          # Application React
├── docker-compose.yml # Orchestration des services
└── README.md
```

## Stack technique

- **Backend** : FastAPI (Python 3.11+)
- **Frontend** : React + Vite + TailwindCSS
- **Base de données** : InfluxDB (time-series), PostgreSQL (episodes)
- **Cache** : Redis

## Fonctionnalités

- **Dashboard temps réel** : visualisation des niveaux, marées, pluie et score de risque
- **Timeline historique** : navigation dans les données passées (7+ jours)
- **Multi-stations** : suivi de 7 stations hydrométriques sur la Dordogne, Isle et Dronne
- **Analyse d'épisodes** : reconstruction du contexte hydro-météo des inondations passées
- **Modèle de risque** : scoring pondéré (marée 15%, niveaux rivière 30%, pluie 40%, etc.)
- **Analyse vidéo** : détection du niveau d'eau via caméra sur le seuil (en cours)

## Sources de données

### Marées
- **Open-Meteo Marine** : API gratuite pour les données de niveau marin
- **SHOM** : API officielle (nécessite clé API)
- Fallback : calcul prédictif

### Niveaux rivières (Hub'Eau)
7 stations hydrométriques via l'API Hub'Eau :

| Station | Rivière | Distance du moulin | Seuils (jaune/orange/rouge) |
|---------|---------|-------------------|-----------------------------|
| Libourne | Dordogne | 19 km | 3.0 / 4.0 / 5.0 m |
| Abzac | Isle | 10 km | 2.5 / 3.5 / 4.5 m |
| Coutras | Dronne | 21 km | 2.0 / 3.0 / 4.0 m |
| Pessac-sur-Dordogne | Dordogne | 25 km | 2.5 / 3.5 / 4.5 m |
| Bergerac | Dordogne | 60 km | 3.0 / 4.0 / 5.0 m |
| Périgueux | Isle | 65 km | 2.0 / 3.0 / 4.0 m |
| Ribérac | Dronne | 50 km | 1.5 / 2.5 / 3.5 m |

### Précipitations
- **Netatmo** : Réseau de stations météo (OAuth2, clé API requise)
  - Priorité aux stations du bassin Lidoire
  - Moyenne pondérée par distance
- **Infoclimat** : Réseau StatIC (clé API requise, IP autorisée)
- **Open-Meteo** : Prévisions météo (gratuit, pas de clé)

## Stockage des données

Toutes les données sont stockées de manière permanente dans InfluxDB :

- **river_level** : Niveaux des 7 stations (~2000 points/station/semaine)
- **tide** : Données de marée (hauteur, coefficient, type PM/BM)
- **rainfall** : Précipitations (mm/h, mm/24h) par source
- **risk_score** : Scores de risque calculés avec tous les facteurs

### Pattern cache-through

1. Lecture d'abord dans InfluxDB
2. Si absent → fetch depuis l'API externe
3. Stockage permanent dans InfluxDB
4. Les données historiques ne sont jamais re-fetchées

## Installation

### Prérequis

- Docker et Docker Compose
- Node.js 18+ (pour le développement frontend)
- Python 3.11+ (pour le développement backend)

### Lancement avec Docker

```bash
docker compose up -d
```

### Développement local

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

## Ports (offset +100 pour éviter conflits)

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
# Base de données (ports avec offset +100)
DATABASE_URL=postgresql://user:pass@localhost:5532/lidoire
INFLUXDB_URL=http://localhost:8186
INFLUXDB_TOKEN=your-token
INFLUXDB_ORG=lidoire
INFLUXDB_BUCKET=flood_data

# APIs externes
SHOM_API_KEY=           # Optionnel - marées officielles
NETATMO_CLIENT_ID=      # Requis pour précipitations Netatmo
NETATMO_CLIENT_SECRET=
NETATMO_REFRESH_TOKEN=
INFOCLIMAT_API_KEY=     # Optionnel - stations Infoclimat

# Coordonnées du moulin
MOULIN_LAT=44.8759811
MOULIN_LON=0.0015091
```

## API Endpoints

### Status
- `GET /api/health` : Healthcheck
- `GET /api/status` : Status détaillé des sources de données

### Temps réel
- `GET /api/realtime` : Toutes les données temps réel
- `GET /api/realtime/river` : Niveau Dordogne
- `GET /api/realtime/tide` : Marées
- `GET /api/realtime/rainfall` : Précipitations agrégées
- `GET /api/realtime/netatmo` : Données Netatmo brutes
- `GET /api/realtime/infoclimat` : Stations Infoclimat

### Timeline
- `GET /api/timeline?days=7` : Timeline historique
- `GET /api/timeline/stations?days=7` : Multi-stations (7 jours de données)

### Risque
- `GET /api/risk` : Score de risque actuel
- `GET /api/risk/forecast` : Prévisions de risque
- `GET /api/risk/thresholds` : Configuration des seuils
- `POST /api/risk/simulate` : Simulation avec valeurs custom

### Episodes
- `GET /api/episodes` : Liste des épisodes d'inondation
- `POST /api/episodes` : Créer un épisode
- `GET /api/episodes/analyze-v2` : Analyser un épisode passé

### Authentification
- `GET /api/auth/netatmo/setup` : Instructions OAuth Netatmo
- `GET /api/auth/netatmo/callback` : Callback OAuth
- `GET /api/auth/netatmo/status` : Status de l'authentification

## Licence

Projet privé - Usage personnel
