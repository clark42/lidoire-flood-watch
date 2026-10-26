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
python -m scripts.eval replay --offline # scores horaires v3.4 + baselines
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
│   ├── risk.py          # /api/risk : score, previsions, outlook, forecast-skill, simulation
│   ├── notify.py        # /api/notify/test (message de test, X-API-Key)
│   ├── vigilance.py     # /api/vigilance (couleurs officielles, lecture du cache)
│   ├── observations.py  # /api/observations (+ photo), /api/episodes/{id}/observations
│   ├── indicators.py    # /api/indicators (indicateurs hydrologiques hors modele)
│   ├── forecast_ensemble.py  # /api/forecast/ensemble (pluie d'ensemble + GloFAS, hors score)
│   ├── geo.py           # /api/geo/basin (polygone du bassin)
│   └── auth.py          # /api/auth/netatmo (OAuth2)
├── data/
│   ├── episodes_labels.json  # Verite terrain des 14 episodes (source unique)
│   ├── action_plan.json # Actions par niveau (jaune/orange/rouge), modifiable par le proprietaire
│   └── lidoire_basin.geojson # Bassin versant topographique (BD TOPAGE, 230.8 km2)
├── models/
│   ├── schemas.py       # Modeles Pydantic (RiskScore, EpisodeOutcome, EpisodeLabelSet...)
│   ├── probabilistic.py # api_json (dates UTC Z) des routes radar/ensemble
│   └── orm.py           # Tables PostgreSQL (flood_episodes, notifier_state, forecast_runs)
└── services/
    ├── risk_core.py     # Coeur de scoring PUR, modele v3.4 (compute_risk, aucune I/O)
    ├── risk_shadow.py   # Modele shadow 3.4-gate (gate de remous, PUR), stocke, jamais affiche
    ├── risk_v2.py       # Construction des features (temps reel, simulation, backfill), stockage
    ├── forecast.py      # Previsions horaires (features par heure -> compute_risk)
    ├── outlook.py       # Horizons 6/24/72 h, pourquoi, checklist, notes de confiance (PUR + cache)
    ├── forecast_archive.py  # Archive forecast_runs (job horaire) + prevu vs observe
    ├── backfill.py      # Backfill historique (rivers, rain, tides, soil, risk)
    ├── episode_detector.py  # Detection automatique d'episodes par seuil
    ├── episode_analyzer.py / episode_analyzer_v2.py  # Contexte hydro-meteo des episodes
    ├── labels.py        # Chargement des labels, parsing des anciens prefixes [CONFIRMED: ...]
    ├── cache.py         # get_redis() partage, TTLCache en memoire, CircuitBreaker, EXTERNAL_TIMEOUT_S
    ├── influx_client.py # InfluxDB non bloquant, ecritures par lot, validation Flux
    ├── data_store.py    # Acces InfluxDB/PostgreSQL de plus haut niveau
    ├── scheduler.py     # APScheduler (collectes + score horaire)
    ├── notifier.py      # Notifications ntfy (niveau, prevision, donnees perimees)
    ├── observations.py  # Observations terrain : PostgreSQL, fichiers photo, lien episodes
    ├── photo_safety.py  # Octets magiques, retrait GPS (JPEG/PNG/WebP), sans lib image
    ├── video_analyzer.py    # Detection niveau d'eau via camera
    ├── models_experimental.py  # Modeles candidats, importes par AUCUN code de production
    ├── basin.py         # Polygone du bassin, point dans polygone (PUR)
    ├── probabilistic_store.py  # Lectures Influx radar_rain / glofas_forecast / ensemble_rain
    ├── probabilistic_jobs.py   # Jobs radar (15 min), GloFAS et ensemble (6 h)
    ├── indicators.py    # Catalogue des series indicateurs, lectures Influx, payload /api/indicators
    ├── climatology.py   # PUR : percentile_of, tendance, seuils (indicateurs)
    └── collectors/
        ├── hubeau.py        # Hub'Eau (temps reel) + Hydroportail (historique)
        ├── vigicrues.py     # Vigicrues (Libourne, metres)
        ├── tide.py          # SHOM / Open-Meteo Marine (marees estimees jamais stockees)
        ├── weather.py       # Open-Meteo (pluie, vent, sol), UTC
        ├── netatmo.py       # Netatmo (stations citoyennes du bassin)
        ├── infoclimat.py    # Infoclimat StatIC
        ├── meteo_agricole.py # Meteo Agricole (bilan hydrique, nowcast)
        ├── vigilance.py     # Vigilance Vigicrues (DO5, DO22) + Meteo-France (33, 24)
        ├── hydro_indicators.py # Debits, piezometre, maree observee (hors modele)
        ├── opera_radar.py   # Radar OPERA ACRR (COG lu par plages HTTP, numpy + zlib)
        ├── glofas.py        # GloFAS v4 maille Lidoire (Open-Meteo Flood)
        └── ensemble_rain.py # Open-Meteo Ensemble, pluie bassin et probabilites
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
├── groundtruth/  evaluation/  models/  followups/  notifier/  indicators/ (fixtures/ JSON enregistres)  outlook/
├── vigilance/ (fixtures/ : reponses enregistrees)  observations/  probabilistic/ (fixtures/)
```

Supprimes : `services/risk.py` (modele v1), `validate_model.py`, `analyze_episodes.py` (remplaces par `scripts/eval/`).

### Frontend (`frontend/src/`)

```
src/
├── App.tsx              # Router principal, navigation, pages en React.lazy (un chunk par route)
├── api.ts               # Client API (fetchJson, X-API-Key depuis localStorage 'lfw_api_key')
├── types.ts             # Types TypeScript (miroir des schemas backend)
├── utils/time.ts        # parseApiDate (UTC) + formatParis : tout l'affichage en Europe/Paris
├── utils/format.ts      # Libelles FR des niveaux de risque, formatDistanceKm
├── utils/observations.ts # Libelles et couleurs des types d'observation
├── tooltipContent.ts    # Dictionnaire centralise des textes de tooltips
├── pages/
│   ├── Dashboard.tsx    # Vue temps reel avec score de risque + TimeMachine ; fraicheur = station primaire (Pessac), HydroMap (leaflet) en lazy
│   ├── Timeline.tsx     # Graphiques historiques multi-sources + forecast overlay
│   ├── Episodes.tsx     # Liste/creation/analyse/issue des episodes
│   └── Configuration.tsx # Statut, simulation, cle API ; version du modele lue dans l'API (`risk_model.version`, sinon `RiskScore.model_version`)
└── components/
    ├── RiskGauge.tsx, RiskBreakdownPanel.tsx (contributions = poids x score / denominateur x conjonction x amortisseur, gate maree), StationsPanel.tsx, RainfallPanel.tsx
    ├── OutlookPanel.tsx (haut du Dashboard), ForecastSkillPanel.tsx (Configuration)
    ├── ForecastPanel.tsx, BasinRainfallGauge.tsx, SoilConditionsPanel.tsx, NowcastRainChart.tsx
    ├── HydroMap.tsx, MetricSparkline.tsx, TimeMachineBanner.tsx, TimelineSourceSelector.tsx
    ├── InfoTooltip.tsx, DataCard.tsx, DataSourcesStatus.tsx (bandeau des sources = `/api/status.sources`)
    ├── IndicatorsPanel.tsx  # Indicateurs hydrologiques (Dashboard, autonome, /api/indicators)
    ├── VigilanceBadges.tsx, ObservationQuickForm.tsx (Dashboard)
    ├── ObservationList.tsx, EpisodeObservationsPanel.tsx (Episodes)
    ├── EnsemblePanel.tsx  # Radar bassin, probabilites de pluie, eventail GloFAS (hors score)
    ├── BasinLayer.tsx   # Polygone du bassin sur HydroMap
    └── ApiKeyPanel.tsx  # Saisie de la cle API (localStorage)
```

## Modele de risque v3.4

Une seule implementation : `risk_core.compute_risk(features: RiskFeatures) -> RiskBreakdown` (pure, sans I/O ni `utcnow()`). Tous les chemins construisent un `RiskFeatures` et l'appellent : `/api/realtime` et `/api/risk` (via `risk_v2.gather_realtime_inputs`), `/api/risk/simulate`, TimeMachine `/api/realtime/at`, `risk_v2.backfill_risk_scores`, et chaque heure de `forecast.py`. Les parametres viennent de `config.py` (`get_model_params()`). v3.3 = corrections de bugs de la calibration v3.2 (14 episodes), poids et mecanismes inchanges. v3.4 = v3.3 + phase de maree corrigee (estimee a la confluence, embouchure + 6 h) + facteurs maree (`tide`, `tide_phase`) ponderes par le gate de Pessac ; poids inchanges.

Constat cle : le moulin est sur la Lidoire (affluent), PAS sur la Dordogne. Un niveau Dordogne eleve seul ne signifie pas inondation au moulin. Le signal le plus fort est la montee simultanee de plusieurs stations + pluie.

| Facteur | Cle `risk_weights` | Poids | Calcul |
|---------|--------------------|-------|--------|
| Niveaux rivieres | `river` | 28% | Moyenne ponderee des stations de poids > 0 (Libourne exclue) |
| Pluie 24h | `rain_24h` | 10% | Observee + 0.5 x prevision 24h ; seuils 25/40/60 mm |
| Pluie 72h | `rain_72h` | 13% | Observee + 0.3 x prevision 72h ; seuils 50/80/120 mm |
| Pluie 7j | `rain_7d` | 5% | Seuils 60/100/150 mm |
| Tendance | `tendency` | 13% | Vitesse de la station primaire (Pessac), cm/h |
| Momentum | `momentum` | 8% | Stations en hausse (> 1 cm/h), avec gate |
| Coefficient maree | `tide` | 5% | Seuils 70/85/110, x gate maree |
| Phase maree | `tide_phase` | 5% | 1.0 a <= 1 h de la PM estimee a la confluence, 0.1 au-dela de 4 h, x gate maree |
| Saturation sol | `soil_sat` | 5% | (facteur - 1.0) / 0.5, facteur 1.0-1.5 |
| Vent d'ouest | `wind_west` | 3% | Lineaire 30-100 km/h |
| Analyse video | `video` | 5% | Index camera 0-100 |

Somme des poids = 1.00. Score station : lineaire par morceaux (0.33 au seuil jaune, 0.66 a l'orange, 1.0 au rouge) puis courbe x^0.6. Une station ne compte que si son dernier point a moins de 3 h (`risk_level_max_age_hours`) ; vitesse par regression sur 3 h de points <= t.

- **Denominateur** : somme des poids des facteurs disponibles (`rain_7d`, `wind_west`, `soil_sat`, `video` peuvent etre indisponibles, listes dans `unavailable`). La maree reste au denominateur : absente ou estimee => `tide` et `tide_phase` valent 0 (note "tide data unavailable or estimated").
- **Gate maree** (v3.4, `tide_gate`) : `tide` et `tide_phase` multiplies par g = (H - jaune) / (orange - jaune) borne a [0, 1], seuils jaune/orange de Pessac (`hubeau_stations`, 3.0 / 3.8 m). H = `backwater_level_m` = max de Pessac sur les 12.5 h precedentes (points <= t, `risk_core.backwater_level`, aussi le H_aval du shadow). H absent : niveau courant de Pessac (note), sinon g = 1. La branche de conjonction pleine mer garde la phase non gatee. Sources de H : temps reel = points stockes + dernier Hub'Eau (`risk_v2.load_primary_levels`), backfill (historique charge 13.5 h avant), TimeMachine (requete 12.5 h en plus du snapshot +/-6 h), prevision = max des points observes et des niveaux projetes de Pessac dans la fenetre.
- **Sol** : n'entre que par `soil_saturation_factor` (multiplie les scores pluie, facteur `soil_sat`, branche de conjonction). Ne jamais pre-multiplier les millimetres.
- **Multiplicateur de conjonction** (1.0 a 2.5x, `conjunction_multiplier`) : branches cumulees "crue active" (>= 4 stations en hausse + riviere > 0.35 + pluie), "multi-stations au-dessus de l'orange" (>= 3 stations a hauteur >= leur propre seuil orange : +0.45 si >= 1 en hausse, +0.30 sinon), "drainage empeche" (riviere haute + pluie 72h, +0.15 a +0.45), sol sature +0.15, montee + pluie +0.20, pluie soutenue +0.15, pleine mer + riviere > 0.5 +0.15.
- **Amortisseur de stabilite** (0.65 a 1.0x, `stability_dampener`) : si score riviere >= 0.4, pluie 72h faible et moins de 3 stations au-dessus de l'orange : Pessac stable/en baisse avec 0 station en hausse => 0.65, 1 => 0.80 ; Pessac en hausse avec <= 1 station en hausse => 0.85.
- **Gate momentum** : base selon le nombre de stations en hausse (0 / 0.2 / 0.5 / 0.8 / 1.0) multipliee par 0.2 / 0.4 / 0.7 / 1.0 selon le score riviere normalise (< 0.30 / < 0.40 / < 0.55 / au-dela ; score, pas metres).
- **Score final** = min(100, somme ponderee x conjonction x amortisseur / denominateur x 100). Niveaux : vert < 15, jaune < 35, orange < 55, rouge.

Stockage : seul le job `risk_scorer` du scheduler (toutes les heures, premier passage 2 min apres le demarrage) ecrit le score temps reel ; les routes de lecture ne stockent jamais. Points `risk_score` : `level` en champ, tag `model_version=3.4` ; les lectures preferent la version courante (puis la plus recente), les anciens points sans tag restent lisibles. Le backfill ecrit la meme serie (pas 1 h jusqu'a 7 jours, 3 h jusqu'a 30 jours, 6 h au-dela).

Shadow `3.4-gate` (`risk_shadow.py`, version = `MODEL_VERSION` + `-gate`) : score v3.4 x gate de remous g(H_aval), H_aval = `backwater_level_m` des features de production (max Pessac sur 12.5 h, points <= t, `risk_core.backwater_level`), g = 0.5 a <= 3.0 m, 1.0 a >= 3.8 m, lineaire entre ; H_aval absent => 1.0. Meme semantique que `V33BackwaterModel` (qui reutilise la fonction de production, test d'equivalence). Ecrit par `risk_scorer` (via `compute_current_risk(store=True)`, un echec n'affecte jamais v3.4) dans la mesure dediee `risk_score_shadow` (tags `model_version=3.4-gate`, les anciens points `3.3-gate` restent en base, `source`, champs `factor_gate`, `factor_h_aval_m`, `factor_base_score`). Toutes les lectures de `risk_score` filtrent la mesure exacte et ecartent aussi toute version suffixee (`is_shadow_risk_version`) : score affiche, API, previsions, detection d'episodes et TimeMachine inchanges. Historique : `POST /api/episodes/backfill?...&risk_shadow=true` (off par defaut). Timeline : `include_risk_shadow=true` (serie `risk:shadow`, bouton "Shadow + remous").

Les entrees different selon le chemin : le temps reel fusionne Open-Meteo/Netatmo/Infoclimat (max) et a vent et sol ; backfill, TimeMachine et replay n'ont que les archives (vent, sol, video indisponibles).

### Previsions

Service dans `backend/app/services/forecast.py` :
- Previsions horaires, 168 h par defaut (`/api/risk/forecast?hours=` de 6 a 336)
- Chaque heure : features projetees -> `compute_risk` ; H+0 reprend les entrees temps reel (meme score que `/api/realtime`)
- Pluie Open-Meteo (AROME/ARPEGE + ECMWF) en UTC, pluie passee calibree sur la pluie temps reel multi-sources (facteur borne a [1/3, 3])
- Niveaux projetes : tendance actuelle (12 h max) + reponse a la pluie tombee apres t0, decalee par `delay_hours` de chaque station
- Nombre de stations en hausse : decroissance lineaire sur 48 h, plus apport de la pluie projetee
- Cache Redis 30 min ; overlay Timeline via `include_forecast=true`
- Le payload ajoute `horizons` (6/24/72 h, voir Outlook), `high_tides` (PM estimees a la confluence) et `inputs_summary` (pluie prevue apres t0 sur 24/72 h, max projete de Pessac sur 72 h, gate maree et H de H+0). `use_cache=False` recalcule (job d'archive) et rafraichit le cache.

### Outlook et archive des previsions

- `GET /api/risk/outlook` (`services/outlook.py`) : pour 6, 24 et 72 h (H+0 inclus), score max, niveau, heure du max, premiere heure a ce niveau, et pourquoi = top 3 des facteurs du RiskBreakdown de l'heure max, en points du score (poids x score / denominateur x conjonction x amortisseur x 100, ramenes a 100 si plafond ; somme = score), textes en francais, plus multiplicateurs et gate maree. Aussi : prochaine PM estimee a la confluence, confiance (`good`/`reduced`/`low` : facteurs indisponibles, maree estimee, sources de `/api/status` perimees, entrees de prevision en erreur ; `low` si source critique ou riviere/pluie prevue absente) et checklist du niveau le plus haut (`act_before` = premiere heure a ce niveau). Lit la prevision 168 h en cache, cache Redis 10 min (`forecast:outlook`, purge avec `invalidate_forecast_cache`), n'ecrit rien.
- `app/data/action_plan.json` : actions courtes par niveau (`yellow`/`orange`/`red`, vert = aucune). Une copie dans `/app/data/action_plan.json` (PVC `backend-data`) remplace le defaut sans reconstruire l'image ; un fichier invalide est ignore (log).
- Table PostgreSQL `forecast_runs` (creee par `init_db`) : une ligne par run horaire, `run_at` (UTC naif), `model_version`, `horizon_hours`, listes JSON `scores` / `levels` / `pessac_levels_m` indexees par echeance (0 = H+0), `rain_forecast_24h_mm`, `rain_forecast_72h_mm`, `pessac_projected_max_m`, `tide_gate`, `inputs` (resume + statut des sources), unique (`run_at`, `model_version`). Ecrite seulement par le job `forecast_archiver` (toutes les heures, 2 min apres `risk_scorer`, via `get_risk_forecast_v2(168, use_cache=False)` : meme chemin v3.4, aucun modele duplique ; un echec n'affecte jamais le score). Retention : tout garder (~3-4 Ko par ligne, ~30 Mo par an), la verification prospective porte sur plusieurs saisons.
- `GET /api/risk/forecast-skill?lead_hours=6,24,72&days=30` (`days` 1-180, 0-168 h, 10 echeances max, 422 sinon) : chaque run archive de la version courante, score a H+L apparie au `risk_score` temps reel stocke (source `realtime`, meme version) le plus proche de `run_at` + L a 35 min pres. Renvoie les paires, MAE, biais (prevu - observe) et table detection / manque / fausse alerte / rejet correct au seuil orange (POD, FAR, CSI) par echeance. Paires horaires, pas des evenements. Vide mais bien forme (`n_runs` = 0) tant que l'archive se remplit ; 503 si Postgres ou Influx en echec.

### Indicateurs hydrologiques (hors modele)

Series pour l'affichage, l'evaluation et un futur v4, SANS effet sur v3.4 : blocs de config separes `indicator_flow_stations`, `indicator_groundwater_stations`, `indicator_tide_gauges` (jamais dans `hubeau_stations`, que le modele itere) ; aucun module du modele ne les lit (`tests/indicators/test_indicators_v34_unchanged.py` : sorties `compute_risk` dorees, parametres identiques avec/sans ces blocs, snapshot de `hubeau_stations`). Collecteur `collectors/hydro_indicators.py`, tag `key` commun :
- `river_flow` (`q_m3s`, `h_m` pour l'Eyraud) : Eyraud P540401002 (73,5 km2, analogue du ruissellement de la Lidoire : ses 6 plus gros debits journaliers depuis 2022-11 = les 6 crues du moulin), Caudeau P524401001, Pessac P555001001, Lamonzie P532001001 (substitut de Bergerac, sans Q temps reel), Argentat P135001001 (laches EDF). Hub'Eau `observations_tr` (Q l/s, H mm, 5 min ; fenetre 24 h car latence Eyraud ~7 h), historique Hydroportail (`source=hydroportail`), repli `obs_elab` QmnJ journalier. Seuils sur la moyenne journaliere (comparee a la moyenne 24 h) : `watch` = p99 QmnJ 2022-11 -> 2026-10, Eyraud `alert` 17,5 m3/s (en echantillon).
- `groundwater` (`level_m_ngf`, `depth_m`) : piezometre alluvial Mouliets-et-Villemartin 08048X0088/P (p99-p100 a chaque crue, record 7,47 m NGF le 2026-02-12), `niveaux_nappes/chroniques_tr` (horaire, latence ~12 h) et `chroniques` (journalier). Seuil `watch` 7,0 m NGF.
- `tide_observed` (`height_m`) : SHOM REFMAR Bordeaux 196 (sans cle, 1 min moyenne a 10 min, zero hydrographique) et Hub'Eau Bec d'Ambes P916000101 (5 min). Maree reelle (surcote et debit compris), jamais une entree de v3.4.

Jobs : `indicator_flow_collector` et `tide_observed_collector` 15 min, `groundwater_collector` 3 h (EXTERNAL_TIMEOUT_S, un CircuitBreaker par amont : Hub'Eau, niveaux_nappes, SHOM). Aucune route n'appelle d'API externe : `GET /api/indicators` lit Influx (cache 2 min, historique journalier cache 6 h) et renvoie `runoff`, `dordogne`, `groundwater`, `tide_observed` : valeur, tendance, `percentile` (rang de la moyenne 24 h, du niveau ou du max 24 h parmi les agregats journaliers depuis 2022-11, `climatology.percentile_of`), distance aux seuils, `model_input=false`. Timeline : ids `flow:EYRAUD`, `gw:MOULIETS`, `tide_obs:BORDEAUX`... (`/timeline/sources.indicator_series`, `/timeline/unified?indicators=`, series stockees seulement, pas de cache-through). `/api/status.sources.indicator_*` informatifs (jamais critiques). Historique : `POST /api/episodes/backfill?...&indicators=true` (off par defaut, ~10-20 min depuis 2022-11, ~1,2 M points).

### Previsions probabilistes et pluie radar (hors score)

Nouvelles series et routes, JAMAIS lues par le modele v3.4 (test `test_risk_model_never_reads_the_new_series`). Reglages `RADAR_ENABLED`, `GLOFAS_ENABLED`, `ENSEMBLE_RAIN_ENABLED` (vrais par defaut, job non enregistre sinon). Toutes les sources sont en HTTPS, sans cle.
- **Bassin** : `app/data/lidoire_basin.geojson`, union des 5 bassins elementaires BD TOPAGE (Sandre WFS `BassinVersantTopographique_FXX`, Lidoire + Lechout), 230.8 km2, simplifie a 25 m ; source et licence (Licence Ouverte) dans `properties`. Servi par `GET /api/geo/basin`, trace sur HydroMap.
- **Radar OPERA** (`opera_radar.py`, CC BY 4.0, attribution affichee) : composite ACRR (cumul 1 h finissant a l'heure du fichier) toutes les 15 min, 2 km, LAEA, COG `s3.waw3-1.cloudferro.com/openradar-24h` (24 h glissantes, ~10-20 min de latence, des creneaux manquent). Listing S3, en-tete + seules tuiles sous le bassin par requetes Range (64 Ko + une tuile de ~100-200 Ko au lieu de 4 Mo), lecteur TIFF maison (deflate, predicteurs 1/2/3) et LAEA ellipsoidale (Snyder, ecart < 1 mm avec pyproj) : aucune dependance ajoutee. Moyenne ponderee par la part de chaque pixel dans le polygone (8 x 8 sous-points), NaN = pas d'echo (0 mm), -9999000 = hors couverture (`coverage`). Mesure `radar_rain` (tags `source=opera`, `basin=LIDOIRE`, `product=ACRR` ; champs `basin_mean_mm`, `max_pixel_mm`, `coverage`, `quality`, `pixels`), images manquantes des 6 dernieres heures reprises (12 max par passage). `GET /api/realtime/radar` : cumuls 1/3/6/24 h = somme des images aux heures pleines depuis la derniere, `complete` si aucune heure manque, heure et age de la derniere image.
- **GloFAS** (`glofas.py`) : Open-Meteo Flood `forecast_v4&ensemble=true`, 51 membres, 30 j, maille 44.875/-0.025 (proxy modelise, aucune jauge). Par jour : p10/p50/p90, min/max, P(> p95) et P(> p99) de la reanalyse `consolidated_v4` de la meme maille (1997-2025 : p95 3.12, p99 6.35 m3/s), climatologie calculee une fois (`glofas_climatology`). Chaque run est stocke (`glofas_forecast`, tags `cell`, `run` = date d'init via `meta.json`, `lead` en jours ; pas d'archive amont).
- **Pluie d'ensemble** (`ensemble_rain.py`) : Open-Meteo Ensemble, `icon_d2` (20 membres, ~2.5 j), `icon_eu` (40, ~5.5 j), `ecmwf_ifs025` (51, 15 j), 3 points du bassin ponderes par l'aire des sous-bassins (`ensemble_rain_points`). Pluie horaire = cumul de l'heure precedente, fenetre (debut, fin]. P(24 h > 25/40/60 mm) et P(72 h > 50/80/120 mm) (seuils lus dans `get_model_params`), quantiles ; `blend` = moyenne des modeles couvrant la fenetre. Stockage de chaque run (`ensemble_rain`, tags `model`, `run` = `YYYY-MM-DDTHHZ`, `window` = `24h`/`72h`/`run`, `lead` en heures), fenetres ancrees sur l'init du run ; `meta.json` DWD perime => run ancre sur l'heure de collecte (`run_init_known=0`).
- `GET /api/forecast/ensemble` : `rain.horizons["24h"|"72h"]` (fenetres depuis l'heure courante, par modele + `blend`) et `glofas.days` ; cache-through 3 h en memoire, la route n'ecrit jamais. Frontend `EnsemblePanel` (tableau des probabilites, eventail GloFAS, bande radar) sous `ForecastPanel`.
- Timeline : `include_radar=true` (`radar:LIDOIRE`, images aux heures pleines, sommes par fenetre au-dela de 1 h) et `include_glofas=true` (`glofas:LIDOIRE`, mediane du run le plus recent par jour) ; series stockees seulement, pas de cache-through.

### Notifications

`services/notifier.py`, job `notifier` (toutes les heures, 5 min apres `risk_scorer`), canal ntfy (`POST <NTFY_URL>/<NTFY_TOPIC>`, bearer `NTFY_TOKEN` optionnel, interface `Channel` pour d'autres canaux). Sans `NTFY_URL`/`NTFY_TOPIC` : aucun job, un log info au demarrage. Declencheurs : dernier score temps reel stocke passant en orange/rouge (immediat ; jaune seulement si `NOTIFY_YELLOW`), baisse envoyee apres `NOTIFY_DOWNGRADE_HOLD_HOURS` (3 h, le score oscille avec la maree) vers le plus haut niveau vu pendant l'attente ; max de la prevision sur `NOTIFY_FORECAST_HOURS` (48 h) en orange/rouge, une fois par evenement (nouvel envoi si escalade en rouge, fin apres la meme attente) ; Pessac sans point depuis 3 h ou aucun score stocke depuis 2 h (une alerte, un retablissement). Etat persistant en JSON dans la table PostgreSQL `notifier_state` (Redis non persistant en prod) ; une transition n'est acquittee qu'une fois le message delivre (reessai a l'execution suivante), un redemarrage ne renvoie rien. Messages en francais (niveau, score, Pessac, pluie 72 h, prochaine pleine mer, lien `FRONTEND_URL`). `POST /api/notify/test` envoie un message de test. En prod : cles `NTFY_*` dans le secret Scaleway `lidoire-external-apis` (`externalApis.enabled=true`).

### Stations surveillees

Distances a vol d'oiseau depuis le moulin (haversine sur `lat`/`lon` du config) ; delais et poids de `config.hubeau_stations`. `distance_km` n'est jamais code en dur : un validateur de `Settings` le calcule depuis `lat`/`lon` (`app/geo.py`, arrondi a 0.1 km), et toutes les routes (cartes du dashboard, `/api/timeline/sources`, `/api/sources-detail`) le reprennent. Affichage uniquement, jamais lu par le modele.

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

- **Marees** : SHOM (cle API, port de Bordeaux) ou Open-Meteo Marine (niveau marin modele au large de l'embouchure, 45.6 / -1.2 ; PM/BM extraites, coefficient estime). Les heures sont decalees une seule fois, a la collecte, de `tide_phase_lag_hours[source]` (Open-Meteo 6 h, SHOM 2 h non mesure) pour estimer la maree a la confluence de la Lidoire, et stockees sous `port=LIDOIRE` (`tide_port_confluence`) ; hauteurs et coefficients restent ceux de la source. PM mesuree apres celle de l'embouchure (archives 2023-06 -> 2025-12) : Libourne +4.0 h, Pessac +7.0 h. Les anciens points `BORDEAUX`/`LIBOURNE` (sans decalage) ne sont plus lus : apres deploiement, re-backfill des marees et des scores. Serie timeline `tide:LIDOIRE`. Marees estimees jamais stockees, `TideForecast.available=False`.
- **Niveaux rivieres** : Hub'Eau temps reel (30 derniers jours), Hydroportail pour l'historique, Vigicrues pour Libourne (metres)
- **Meteo prevue** : Open-Meteo (modeles AROME/ARPEGE), timestamps UTC
- **Meteo locale** : Netatmo (stations citoyennes du bassin Lidoire), Infoclimat StatIC
- **Sol** : Open-Meteo (humidite, temperature, ET0) + Meteo Agricole (bilan hydrique). Deux modeles distincts en "best match" : le temps reel (`/api/realtime` `soil`, entree du modele) lit les couches 0-1/1-3/3-9 cm et 9-27/27-81 cm servies par DWD ICON ; la Timeline (`soil_timeline`) lit 0-7/7-28/28-100 cm servies par ECMWF IFS (ERA5 en archive), nettement plus humides (ex. 0.195 vs 0.311 m3/m3 a la meme heure). Les series Timeline ne melangent jamais les deux (seul ET0 peut venir du temps reel).
- **Camera** : Flux RTSP configurable via `CAMERA_RTSP_URL`
- **Vigilance officielle** (`collectors/vigilance.py`, informative, PAS une entree du modele) : Vigicrues `services/1/InfoVigiCru.geojson` (~2.2 Mo, sans filtre ni gzip, `NivInfViCr` 1-4 par `CdEntCru`) pour les troncons DO5 "Dordogne aval" (Pessac) et DO22 "Confluence Garonne - Dordogne" (Libourne), SPC 32 ; Meteo-France via le miroir data.gouv `files.data.gouv.fr/meteofrance/data/vigilance/metropole/{AAAA}/{MM}/{JJ}/{HHMMSS}/CDP_CARTE_EXTERNE.json` (pas d'alias latest : index du jour, runs du plus recent au plus ancien, les runs texte seuls n'ont pas de carte), departements 33 et 24, phenomenes 2 pluie-inondation, 4 crues, 3 orages, 1 vent, periodes J/J1. Couleurs 1 vert, 2 jaune, 3 orange, 4 rouge. Aucun historique cote API : mesure Influx `vigilance` (tags `source` vigicrues|meteofrance, `zone` DO5|DO22|33|24, `phenomenon`, champ `level`) ecrite a chaque changement, au premier passage apres demarrage et toutes les 6 h (keepalive). `GET /api/vigilance` lit le cache (`peek_vigilance`, perime a 2 h), liens vers vigicrues.gouv.fr/territoire/32, /station/<code> et vigilance.meteofrance.fr/fr/<departement>.

Jobs du scheduler : rivieres, Netatmo et Infoclimat 15 min, meteo 1 h, marees et Meteo Agricole 6 h, nowcast 5 min, `risk_scorer` 1 h, `forecast_archiver` 1 h (2 min apres `risk_scorer`), `notifier` 1 h (5 min apres `risk_scorer`, si ntfy configure), indicateurs (debits et maree observee 15 min, piezometre 3 h), `vigilance_collector` 30 min, radar OPERA 15 min, GloFAS et pluie d'ensemble 6 h (premier passage 1 min apres le demarrage), camera si configuree.

APIs externes hors du chemin des requetes :
- Netatmo, Infoclimat, Meteo Agricole, nowcast et vigilance ne sont jamais appeles par une route ni par `gather_realtime_inputs` : lecture seule des caches remplis par les jobs (`peek_*`, perimes au-dela de 1 h pour Netatmo/Infoclimat, 15 min pour le nowcast, 12 h pour Meteo Agricole ; au-dela => indisponible, comme un echec).
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
# risk_score de la version courante (3.4, et anciens sans tag) sont lus, jamais les series shadow (3.4-gate) :
# re-backfiller apres le passage en v3.4 avant de relancer la detection.
POST /api/episodes/detect?start_date=2022-11-01&end_date=2026-10-26&threshold=45
```

Tous les POST/PUT/DELETE ci-dessus exigent `X-API-Key` quand `API_KEY` est defini.

### Observations terrain

Table PostgreSQL `observations` (creee par `init_db`) : `observed_at` (UTC), `kind` (`flooded` | `water_rising` | `near_miss` | `normal`), `water_height_cm` (nullable, relatif a `reference` en texte : terrasse, plancher, seuil du moulin), `note`, `photo_path`, `created_at`, `source`. Formulaire `ObservationQuickForm` sur le Dashboard (Inonde / L'eau monte / RAS, lien "Presque inonde"), liste sur la page Episodes.

- `POST /api/observations` (X-API-Key) : JSON `ObservationCreate`, ou multipart avec ce JSON dans le champ `data` et une `photo` optionnelle. `GET /api/observations?start&end` (public, plus recent d'abord, 500 max). `DELETE /api/observations/{id}` (ligne + fichier).
- Photo : format par octets magiques uniquement (JPEG, PNG, WebP, HEIC/HEIF ; content-type contradictoire = 415), 8 Mo max (413), nom serveur `<uuid hex>.<ext>` (0600) dans `observations_photo_dir` (`/app/data/observations`, PVC). GPS retire sans re-encodage : JPEG = IFD GPS efface et delie (orientation gardee) + XMP supprime, PNG = eXIf/tEXt/zTXt/iTXt supprimes, WebP = chunks EXIF/XMP supprimes ; HEIC garde ses metadonnees (GPS compris). `GET /api/observations/{id}/photo` exige X-API-Key meme en lecture (`deps.require_api_key`, le frontend la charge en blob), `nosniff`, `no-store`, nom stocke revalide (regex + repertoire) contre la traversee.
- `GET /api/episodes/{id}/observations` : observations dans [debut - 12 h, (fin ou pic ou debut) + 12 h] (`observation_episode_margin_hours`) et, si l'episode n'a pas d'issue, `suggested_outcome` (flooded avant near_miss) jamais applique : le bouton du detail appelle `POST /api/episodes/confirm/{id}` avec `source=observation`.

## Evaluation (`backend/scripts/eval/`)

Replay horaire causal (points <= t uniquement) de v3.4 (via `compute_risk`) et de baselines sur les archives publiques, sans InfluxDB. `build` telecharge une fois (~4 min) dans `backend/data/eval_cache/` ; ensuite `replay`/`report` avec `--offline` (lecture du cache en ~15 s). Sorties dans `backend/data/eval_out/` (gitignore). Options : `--merge-gap 12`, `--strict-start`, `--no-tide`, `--tide-lag 0` (decalage embouchure -> confluence, defaut config), `--sensitivity`, `--threshold orange=35`.

Definitions (`contingency.py`, `hourly.py`) :
- Alerte = suite d'heures consecutives avec score >= seuil. Elle detecte une crue si elle demarre entre pic - 72 h et pic + 6 h (ou est deja en cours a l'ouverture de la fenetre).
- POD = detections / crues ; FAR brut = (negatives labellisees + non labellisees) / alertes jugees ; FAR labellise = negatives labellisees seulement ; CSI = detections / (detections + manques + fausses alertes) ; avance = pic - debut d'alerte.
- AUC horaire : cible "crue dans les 24 h" (125 heures positives, 28 170 negatives).

Interface d'un modele candidat : `name`, `score(row: FeatureRow) -> float | None`, optionnellement `kind` (`score` | `probability`), `thresholds`, `reset()`. Voir la docstring de `scripts/eval/__init__.py`.

Resultats (2022-11-01 a 2026-10-25, 5 crues, alertes non fusionnees, phase maree a +6 h ; reference v3.3 a phase 0 h : 241 alertes orange, 57 fusionnees, CSI 0.09, AUC 0.952) :

| Modele | Protocole | Niveau | Crues | Alertes | FA brutes (lab.+non lab.) | FAR brut / lab. | CSI | Avance med./min (h) | Alertes/an | AUC |
|--------|-----------|--------|-------|---------|------|------|-----|------|------|-----|
| v3.4 | in-sample (~45 constantes) | orange | 5/5 | 154 | 140 (18+122) | 97% / 78% | 0.03 | 25 / 7 | 44.9 | 0.942 [0.86-0.99] |
| v3.4 | in-sample | rouge | 5/5 | 79 | 61 (11+50) | 92% / 69% | 0.08 | 20 / 1 | 23.6 | |
| pessac_level | sans label | orange | 4/5 | 20 | 16 (3+13) | 80% / 43% | 0.19 | 37 / -2 | 5.8 | 0.894 [0.78-0.97] |
| pessac_level | sans label | rouge | 3/5 | 9 | | 67% / 25% | 0.27 | 3 / -4 | 3.0 | |
| rain_72h | sans label | orange | 1/5 | 1 | | | 0.20 | | | 0.912 |
| v3.4 + gate remous | sans label, 0 param. | orange | 4/5 | 81 | 71 (13+58) | 95% / 76% | 0.05 | 46 / 20 | 25.4 | 0.938 [0.85-0.99] |
| v3.4 + gate remous | sans label | rouge | 4/5 | 66 | | | 0.07 | 43 / 11 | | |
| bw_rule_api_cv | LOEO bloque, 2 param. | orange | 3/5 | 8 | 5 (1+4) | 62% / 25% | 0.30 | 12 / 4 | 2.5 | 0.928 [0.86-0.98] |
| lr_gr4h_haval_cv | LOEO bloque, 3 param. | orange | 4/5 | 15 | 10 (2+8) | 71% / 33% | 0.27 | 3 / -2 | 4.0 | 0.909 [0.81-0.98] |

Alertes fusionnees a 12 h (orange) : v3.4 44 alertes / CSI 0.12 / 11.8 par an, POD 5/5 (rouge : 22 / CSI 0.23) ; v3.4 + gate remous 19 / CSI 0.20 / 5.0 par an, POD 4/5 ; pessac_level 13 / CSI 0.29 ; bw_rule_api_cv 8 / CSI 0.30 ; lr_gr4h_haval_cv 13 / CSI 0.29.

Precautions : 5 crues (une crue = 20 points de POD, IC 95% de 3/5 = 15-95%, aucun ecart hors echantillon significatif) ; labels non exhaustifs (FAR brut pessimiste) ; v3.4 est in-sample (constantes v3.2) donc optimiste. v3.3 avait ete calibre avec la phase non decalee (PM embouchure ~ BM a Libourne), qui compensait l'oscillation tidale de Pessac (PM a +7 h) : a +6 h sans gate, les alertes orange passent de 241 a 411 (122 avec `--no-tide`) ; le gate maree les ramene a 154. Le gate coute de l'avance (min 11 -> 7 h, mediane 63 -> 25 h). 2023-03-11 est une crue par infiltration (colmatee depuis).

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

Chart `deploy/helm/lidoire-flood-watch/` (cluster Scaleway Kapsule `sharedhosting`, contexte `admin@sharedhosting-production`, toujours en fin de commande). Images GHCR `ghcr.io/clark42/lidoire-flood-watch-{backend,frontend}` construites par `.github/workflows/images.yml` (push `main` / tags `v*` : amd64 + arm64, tags `sha-<7>`, `main`, semver ; PR : build amd64 sans push). Avant le push, scan Trivy de l'image amd64 (`aquasecurity/trivy-action` epingle par SHA) : HIGH/CRITICAL dans le resume du job, echec sur CRITICAL corrigeable. Mises a jour : `renovate.json` (hebdomadaire, minor/patch groupes, actions epinglees par digest).

- Prerequis : stack Terraform `11-lidoire` appliquee (namespace `lidoire` PSS restricted, NetworkPolicies, secrets Scaleway `lidoire-generated`, `lidoire-external-apis`, `lidoire-backup-s3`) et images publiees.
- Deploiement : `helm upgrade --install lidoire deploy/helm/lidoire-flood-watch -n lidoire --set image.tag=sha-<7> --kube-context admin@sharedhosting-production`. Verification : `helm lint` + `helm template ... | kubeconform -strict -ignore-missing-schemas -kubernetes-version 1.35.0 -summary`.
- Le chart ne cree ni namespace ni NetworkPolicy. Noms de Services fixes (`backend` impose par `frontend/nginx.conf`) : une release par namespace.
- Secrets : ExternalSecrets (`ClusterSecretStore` `scaleway-secret-manager`, `external-secrets.io/v1beta1`, cle `name:<secret>`). `lidoire-generated` (mots de passe bruts, sans `API_KEY`) et `lidoire-backend` (`DATABASE_URL`, `REDIS_URL` assembles par le template ESO avec `urlquery`, `INFLUXDB_TOKEN` = jeton admin, `API_KEY`). `INFLUXDB_ADMIN_*` jamais passes au backend. `lidoire-external-apis` derriere `externalApis.enabled` (envFrom optionnel).
- Backend : 1 replique, `Recreate` obligatoire (APScheduler), sondes `/api/health`, initContainer d'attente TCP des dependances, PVC `backend-data` 1 Gi (`helm.sh/resource-policy: keep`). StatefulSets `postgres` (5 Gi) et `influxdb` (10 Gi) sur `sbs-default-retain`, Redis sans persistance.
- PSS restricted partout, rootfs en lecture seule + emptyDir : uid 10001 (backend), 101 (nginx), 70 (postgres, `PGDATA` en sous-dossier), 1000 (influxdb, entrypoint non root), 999/1000 (redis).
- Acces `http://lidoire.ts.kamorion.cloud` via tailnet (Service `frontend` ClusterIP fixe `10.32.0.100`) ; Ingress optionnel desactive.
- Installation neuve, et apres le passage en v3.4 : reconstituer via `POST /api/episodes/backfill?start_date=2022-11-01&end_date=<aujourd'hui>` avec `X-API-Key` (Secret `lidoire-backend`, cle `API_KEY`). Il recharge les marees sous `port=LIDOIRE` puis recalcule les scores `model_version=3.4`.
- Sauvegarde : CronJob `backup` (pg_dump + `influx backup`, upload rclone vers S3 Scaleway, expiration a 30 jours par la regle de cycle de vie du bucket, le Job ne supprime rien : `backup.retentionDays: 0`). Restauration (`pg_restore`, `influx restore --full`) : voir README.

## Securite

- `POST`/`PUT`/`PATCH`/`DELETE` sur `/api/*` : en-tete `X-API-Key` compare en temps constant a `API_KEY` (401 sinon). `GET` public. `API_KEY` vide = mode dev (avertissement au demarrage). Le backend refuse de demarrer si un secret vaut un placeholder connu (`change-me`, ...).
- Frontend : cle stockee dans localStorage `lfw_api_key`, envoyee par `fetchJson` sur les methodes d'ecriture.
- `GET /api/auth/netatmo` exige `?api_key=` quand `API_KEY` est defini (navigation navigateur, pas d'en-tete possible).
- Photos d'observation privees : X-API-Key exige aussi en GET. nginx limite les corps a 1 Mo sauf `location = /api/observations` (9 Mo) ; un Ingress nginx eventuel aurait besoin de `proxy-body-size: 9m`.
- CORS : liste explicite (`CORS_ORIGINS` + `FRONTEND_URL`), pas de joker, `X-Backfill-Pending` expose.
- InfluxDB : `INFLUXDB_TOKEN` limite au bucket recommande, `INFLUXDB_READ_TOKEN` en lecture seule pour les requetes.

### Contrats d'API a respecter

- Dates : tous les `datetime` des schemas Pydantic (`UtcDatetime`) sortent en ISO 8601 UTC avec `Z` (naifs = UTC en interne). Quelques payloads dict anciens restent naifs : le frontend les lit en UTC (`parseApiDate`) et affiche tout en Europe/Paris (`formatParis`). Vigicrues est en UTC (`+00:00`) et sur le meme zero que Hub'Eau.
- Netatmo : `/api/realtime/basin-rainfall` expose `available`/`stations_count` (moyennes 0.0 = valeurs de remplissage si `available=false`) ; `/api/timeline/sources` marque les stations Netatmo `available=false` tant que Netatmo n'est pas autorise. La Timeline part par defaut sur Open-Meteo (Moulin) + Infoclimat.

- `/api/timeline/unified` : 422 si `start >= end`, periode > 400 jours ou id inconnu (detail `{error, unknown, valid}`) ; attente max 5 s des backfills, ceux en cours listes dans `X-Backfill-Pending`.
- `/api/status` : `services.database|influxdb|redis` = `connected` | `error` | `timeout` (2 s), detail dans `services.checks`, `status` = `healthy` | `degraded`. `sources.{river_primary, rain_openmeteo, infoclimat, netatmo, tide, soil, risk_score, vigilance}` = fraicheur du dernier point stocke (`last()` Flux sur 30 jours, cache 60 s) : `last_timestamp`, `age_hours`, `max_age_hours`, `critical`, `status` = `ok` | `stale` | `unavailable` (aucun point en 30 j) | `unknown` (requete en echec ou Influx down, non cache), `note` (non configure, jeton Netatmo absent). Seuils : Pessac 3 h (`risk_level_max_age_hours`), risk_score temps reel 2 h, Netatmo 1 h, Open-Meteo/Infoclimat/sol 3 h, vigilance 7 h (keepalive 6 h, informative), maree : la derniere PM/BM stockee doit etre dans le futur. `degraded` si une dependance n'est pas `connected` OU si une source critique (`river_primary`, `risk_score`) est `stale`/`unavailable` ; les autres sources sont informatives (dont `indicator_*` : debits 3-12 h, piezometre 36 h, maree observee 3 h ; `radar_opera` 1 h, `glofas` point lead 0 du dernier run 48 h, `ensemble_rain` ligne `window=run` 12 h). `services.collectors` reste une declaration statique (compatibilite). Le bandeau du Dashboard lit `sources` (seule source de verite, `/api/realtime/sources` n'est plus utilise par le frontend) ; pour la maree il affiche le temps jusqu'a la prochaine PM, jamais un age negatif.
- `/api/episodes/confirm/{id}` : `outcome` (`flooded`, `false_positive`, `near_miss`, `unknown`) ou ancien `was_flooded` ; remplace l'issue precedente.

## Variables d'environnement

Fichier `.env` a la racine (voir `.env.example`) :
- Obligatoires pour compose : `POSTGRES_PASSWORD`, `INFLUXDB_ADMIN_PASSWORD`, `INFLUXDB_ADMIN_TOKEN`, `REDIS_PASSWORD` (vides dans `.env.example`, donc compose echoue tant qu'ils ne sont pas remplis)
- `API_KEY` (`openssl rand -hex 32`), `BIND_ADDRESS`, `CORS_ORIGINS`, `FRONTEND_URL`
- `INFLUXDB_TOKEN` (optionnel, sinon jeton admin), `INFLUXDB_READ_TOKEN` (optionnel), `INFLUXDB_ORG`, `INFLUXDB_BUCKET`
- `DATABASE_URL`, `INFLUXDB_URL`, `REDIS_URL` : construits par compose, a definir seulement hors Docker
- `SHOM_API_KEY`, `METEOFRANCE_API_KEY`, `INFOCLIMAT_API_KEY` (optionnels)
- `NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN` (optionnels)
- `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` (optionnels, notifications) ; reglages `NOTIFY_*` (voir `config.py`)
- `CAMERA_RTSP_URL` (optionnel), `LOG_LEVEL`
- `RADAR_ENABLED`, `GLOFAS_ENABLED`, `ENSEMBLE_RAIN_ENABLED` (optionnels, vrais par defaut ; previsions probabilistes et radar, hors score)

Installation existante : conserver l'ancien mot de passe Postgres et l'ancien jeton Influx (`INFLUXDB_ADMIN_TOKEN` = ancien `INFLUXDB_TOKEN`) pour les volumes existants, puis `docker compose run --rm --user root backend chown -R 10001:10001 /app/data`. Detail dans le README.

## Pistes

- Capteur de niveau au moulin (seule mesure directe possible).
- Pluie radar (COMEPHORE, ANTILOPE) comme entree d'un modele pluie-debit. OPERA est collecte en temps reel (`radar_rain`) : le comparer a Lamothe-Montravel et a COMEPHORE avant tout usage dans un modele.
- Evaluer les runs stockes `ensemble_rain` / `glofas_forecast` (Brier, fiabilite) contre `radar_rain` et les episodes avant d'en faire une entree.
- Gate de remous sur v3.4 en mode shadow pendant une saison : en place (serie `risk_score_shadow`, version `3.4-gate`, voir Stockage) ; comparer les deux courbes et labelliser les periodes ci-dessus avant toute bascule.
- Regression logistique / GR4H seulement quand davantage d'episodes seront labellises.
- Archiver aussi le shadow `3.4-gate` dans `forecast_runs` (une ligne par version) et ajouter les scores evenementiels (POD/FAR par episode) a `forecast-skill`.
- Vigilance officielle (Vigicrues DO5, Meteo-France pluie-inondation/crues >= orange) comme feature candidate, une fois la serie `vigilance` accumulee ; observations terrain comme labels supplementaires.
