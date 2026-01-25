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

## Fonctionnalités

- **Dashboard temps réel** : visualisation des niveaux, marées, pluie et score de risque
- **Timeline historique** : navigation dans les données passées
- **Analyse d'épisodes** : reconstruction du contexte hydro-météo des inondations passées
- **Modèle de risque** : scoring basé sur marée, niveaux Dordogne, pluie
- **Analyse vidéo** : détection du niveau d'eau via caméra sur le seuil

## Sources de données

- **Marées** : API SHOM / maree.info (Bordeaux, Libourne)
- **Niveaux rivières** : Vigicrues (Dordogne à Libourne)
- **Météo prévue** : Open-Meteo (modèles AROME/ARPEGE)
- **Pluie observée** : Radar Météo-France, Infoclimat
- **Caméra locale** : Flux RTSP/HTTP du seuil

## Installation

### Prérequis

- Docker et Docker Compose
- Node.js 18+ (pour le développement frontend)
- Python 3.11+ (pour le développement backend)

### Lancement avec Docker

```bash
docker-compose up -d
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

## Configuration

Copier `.env.example` vers `.env` et configurer :

- `DATABASE_URL` : URL de la base PostgreSQL
- `INFLUXDB_URL` : URL de InfluxDB
- `SHOM_API_KEY` : Clé API SHOM (optionnel)
- `METEOFRANCE_API_KEY` : Clé API Météo-France (optionnel)

## API Endpoints

- `GET /api/status` : Healthcheck
- `GET /api/realtime` : Données temps réel
- `GET /api/timeline` : Données historiques
- `GET /api/episodes` : Liste des épisodes d'inondation
- `POST /api/episodes` : Créer un épisode
- `POST /api/analyze-episode` : Analyser un épisode passé
- `GET /api/risk` : Score de risque actuel

## Licence

Projet privé - Usage personnel
