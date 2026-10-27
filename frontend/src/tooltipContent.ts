export const TOOLTIPS = {
  // Stations
  rateOfRise: {
    text: "Vitesse de variation du niveau d'eau. +5 cm/h = montée modérée, +20 cm/h = montée rapide et dangereuse. Une valeur négative indique une décrue.",
    link: { url: 'https://www.vigicrues.gouv.fr/', label: 'Vigicrues' },
  },
  tendency: {
    text: 'Hausse = niveau en montée. Stable = pas de variation significative. Baisse = décrue en cours.',
  },
  delayHours: {
    text: "Temps estimé pour qu'une crue observée à cette station atteigne le moulin. Permet d'anticiper.",
  },
  stationWeight: {
    text: 'Importance de cette station dans le calcul du risque. Les stations proches du moulin pèsent plus.',
  },
  thresholds: {
    text: "Seuils d'alerte officiels Hub'Eau. Jaune = vigilance. Orange = risque de débordement. Rouge = crue majeure.",
    link: { url: 'https://hubeau.eaufrance.fr/page/api-hydrometrie', label: "Hub'Eau API" },
  },
  stationsHeader: {
    text: "Niveaux d'eau mesurés en temps réel sur 7 stations du bassin Dordogne / Isle / Dronne. Le moulin est influencé par ces 3 cours d'eau.",
  },

  // Marees
  tideCoefficient: {
    text: "Force de la marée de 20 (morte-eau) à 120 (vive-eau). Au-dessus de 90, les marées sont fortes et amplifient le risque de crue en bloquant l'écoulement des rivières.",
    link: { url: 'https://maree.info/82', label: 'Marées Libourne' },
  },
  tidePhaseBmPm: {
    text: "PM = Pleine Mer (marée haute), BM = Basse Mer (marée basse). Le risque est maximal à PM quand la marée empêche la rivière de s'écouler vers l'océan. Heures estimées à la confluence de la Lidoire : marée de l'embouchure de la Gironde décalée de 6 h (propagation mesurée : +4 h à Libourne, +7 h à Pessac).",
    link: { url: 'https://www.shom.fr/les-marees', label: 'SHOM - Les marées' },
  },
  tidePhaseHours: {
    text: "Heures avant/après la pleine mer. PM-2h = 2 heures avant la pleine mer. Le risque est maximal dans l'heure autour de PM.",
  },

  // Pluie
  rain24h: {
    text: 'Cumul de pluie sur les dernières 24 heures. Au-delà de 30mm, les sols commencent à saturer. Au-delà de 50mm, le ruissellement augmente fortement.',
  },
  rain72h: {
    text: 'Cumul sur 3 jours. Indique la saturation du bassin versant. Un sol déjà gorgé d\'eau ne peut plus absorber la pluie, qui ruisselle directement vers les rivières.',
  },
  rain7d: {
    text: "Cumul sur 7 jours. Donne une vue de l'état hydrique général du bassin de la Lidoire.",
  },
  rainfallForecast: {
    text: 'Prévision de pluie basée sur les modèles Météo-France (AROME/ARPEGE) via Open-Meteo. Précision décroissante au-delà de 48h.',
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },

  // Risque
  riskScore: {
    text: 'Score composite 0-100 (modèle v3.4) combinant 11 facteurs pondérés (rivières, pluie, marée, vent, sol, vidéo). La marée ne compte que si la Dordogne est haute (Pessac au-dessus de 3.0 m sur 12.5 h). Vert < 15 = normal. Jaune 15-35 = vigilance. Orange 35-55 = alerte. Rouge >= 55 = danger.',
  },
  conjunctionMultiplier: {
    text: "Bonus de risque (x1.0 à x1.5) quand marée haute + niveau élevé + montée des eaux se combinent. Ces conditions ensemble sont bien plus dangereuses que séparément.",
  },

  // Facteurs de risque (pour le tableau de detail)
  factorRiver: {
    text: "Niveaux d'eau des stations Hub'Eau, moyenne pondérée par station (Pessac 30%, Libourne exclue car soumise à la marée). C'est le facteur dominant (28% du score).",
  },
  factorRain24h: {
    text: 'Pluie cumulée sur 24h. Contribue au ruissellement immédiat vers les rivières.',
  },
  factorRain72h: {
    text: 'Pluie cumulée sur 72h. Indique la saturation des sols du bassin versant.',
  },
  factorTide: {
    text: "Coefficient de marée. Les grandes marées bloquent l'écoulement de la Dordogne vers l'océan.",
  },
  factorTidePhase: {
    text: "Phase par rapport à la pleine mer. Le risque maximal est autour de PM (marée haute).",
  },
  factorTendency: {
    text: "Vitesse de montée des eaux. Une hausse rapide amplifie le risque même si le niveau absolu est modéré.",
  },
  factorMomentum: {
    text: "Nombre de stations en hausse simultanée. C'est le signal le plus fort d'inondation au moulin : les 5 épisodes confirmés avaient en moyenne 4.4 stations en hausse vs 2.1 pour les faux positifs. Un niveau élevé mais stable (0 station en hausse) signifie que l'eau se retire, pas qu'elle arrive.",
  },
  factorRain7d: {
    text: 'Pluie cumulée sur 7 jours. Indique la saturation profonde du bassin versant de la Lidoire.',
  },
  factorWindWest: {
    text: "Vent d'ouest (composante westerly). Un vent fort d'ouest pousse la marée dans l'estuaire de la Gironde, amplifiant la surcote à Libourne.",
  },
  factorSoilSat: {
    text: "Saturation du sol (1.0=sec, 1.5=saturé). Un sol saturé ne peut plus absorber la pluie, qui ruisselle directement vers la Lidoire.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil' },
  },
  factorVideo: {
    text: "Niveau d'eau détecté par analyse vidéo de la caméra du moulin. Complément visuel aux capteurs.",
  },

  // Sol & Conditions agricoles
  soilMoisture: {
    text: "Humidité volumétrique du sol (m3/m3). 0.15 = sec, 0.25 = normal, 0.35 = humide, 0.45 = saturé. Modèle DWD ICON via Open-Meteo, couches 0-9 cm et 9-81 cm (entrée du modèle de risque). La Timeline affiche un autre modèle (ECMWF, 0-7 / 7-100 cm), plus humide : les valeurs ne sont pas comparables.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil API' },
  },
  soilSaturationFactor: {
    text: "Facteur de saturation du sol (1.0 à 1.5). Amplifie l'impact de la pluie sur le ruissellement. 1.0 = sol sec (absorbe), 1.5 = sol saturé (tout ruisselle).",
  },
  et0: {
    text: "Évapotranspiration de référence (mm/jour). Mesure l'eau qui s'évapore du sol et des plantes. Si < 0.5 mm/j, le sol sèche très lentement.",
  },
  waterBalance30d: {
    text: "Bilan hydrique sur 30 jours. Surplus = plus de pluie que la normale (sol gorgé). Déficit = moins de pluie que la normale (sol sec). Source : La Météo Agricole.",
    link: { url: 'https://www.lameteoagricole.net/', label: 'La Météo Agricole' },
  },

  // Sources
  netatmoSource: {
    text: 'Réseau de stations météo citoyennes. Fournit la pluviométrie locale en temps réel autour du bassin de la Lidoire.',
    link: { url: 'https://weathermap.netatmo.com/', label: 'Carte Netatmo' },
  },
  infoclimatSource: {
    text: 'Réseau de stations météo amateur StatIC. Données complémentaires aux capteurs Netatmo pour la pluviométrie du bassin.',
    link: { url: 'https://www.infoclimat.fr/stations-meteo/', label: 'Infoclimat' },
  },

  // Maree timeline
  timelineTide: {
    text: "Niveau marin modélisé (Open-Meteo Marine) à l'embouchure de la Gironde, en mètres par rapport au niveau moyen, avec les heures décalées de 6 h pour estimer la marée à la confluence de la Lidoire. Seule la phase (heure des PM/BM) est représentative : l'amplitude reste celle de l'embouchure. La sinusoïde ~12h correspond au cycle naturel des marées. Attention : ce référentiel est différent de celui des stations de rivière (zéro local de chaque échelle).",
    link: { url: 'https://maree.info/82', label: 'Marées Libourne' },
  },

  // Timeline panels
  timelineLevels: {
    text: "Niveaux d'eau mesurés en continu sur les stations du bassin (Dordogne, Isle, Dronne). Chaque courbe correspond à une station hydrométrique Hub'Eau. La marée (tirets, estimée à la confluence) a un référentiel différent (niveau moyen de la mer) et est négative en basse mer.",
    link: { url: 'https://hubeau.eaufrance.fr/page/api-hydrometrie', label: "Hub'Eau API" },
  },
  timelineRainfall: {
    text: "Pluviométrie horaire mesurée et prévue. Sources : Open-Meteo (modèles AROME/ARPEGE), Netatmo (stations citoyennes) et Infoclimat (stations amateur). Un cumul > 10 mm/h indique une pluie forte.",
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },
  timelineRisk: {
    text: "Score de risque composite (0-100, modèle v3.4) calculé toutes les heures. Combine niveaux de rivières (28%), pluie (28%), tendance et momentum (21%), marée (10%) et autres facteurs. Vert < 15, Jaune < 35, Orange < 55, Rouge >= 55.",
  },
  timelineSoilMoisture: {
    text: "Humidité volumétrique du sol en m3/m3, modèle ECMWF (IFS, ERA5 pour l'historique) via Open-Meteo. Couche superficielle (0-7cm) et profonde (7-100cm). Le Dashboard affiche le modèle ICON (0-9 cm), plus sec : valeurs non comparables. Un sol saturé (> 0.40) ne peut plus absorber la pluie.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil API' },
  },
  timelineSoilTemp: {
    text: "Température du sol (0-7cm). Un sol gelé (< 0C) est imperméable et provoque un ruissellement total. Données Open-Meteo historiques.",
  },
  timelineET0: {
    text: "Évapotranspiration de référence FAO (mm/jour). Quantité d'eau qui s'évapore du sol et transpire par les plantes. Si < 0.5 mm/j en hiver, le sol sèche très lentement et reste gorgé d'eau.",
  },
  timelineNowcast: {
    text: "Prévision radar minute par minute de la pluie dans la prochaine heure. Données issues du radar météorologique via La Météo Agricole. Précision élevée mais horizon limité à 60 minutes.",
    link: { url: 'https://www.lameteoagricole.net/', label: 'La Météo Agricole' },
  },

  // Bassin
  basinHeader: {
    text: "Moyenne pondérée de la pluie sur le bassin versant de la Lidoire, calculée à partir des stations Netatmo situées sur le tracé de la rivière (amont 30%, milieu 40%, aval 30%, sur les positions qui ont des stations). Indisponible tant que Netatmo n'est pas autorisé.",
  },

  // Previsions
  outlook: {
    text: "Pour chaque horizon (6 h, 24 h, 72 h) : le score maximal prévu, son heure, et les facteurs qui y contribuent le plus (en points du score). La liste d'actions correspond au niveau le plus élevé prévu ; elle est modifiable dans backend/app/data/action_plan.json. Prévision recalculée chaque heure : un score, pas une probabilité.",
  },
  forecastSkill: {
    text: "Chaque heure, la prévision est archivée. Ici, le score prévu à 6 h, 24 h et 72 h est comparé au score temps réel effectivement calculé à l'heure visée. MAE = écart moyen en points ; biais > 0 = prévision trop pessimiste. Détection = prévu et observé au-dessus du seuil orange ; manque = observé seulement ; fausse alerte = prévu seulement.",
  },
  forecastHeader: {
    text: "Projection du score de risque sur les prochaines heures, basée sur les prévisions de marée et de pluie. Les niveaux de rivière sont maintenus constants (pas de modèle hydrologique).",
  },
  forecast: {
    text: "Prévision de risque sur 72h combinant : pluie horaire (Open-Meteo AROME), marées avec coefficients, et projection des niveaux de rivière basée sur les stations amont avec leurs délais de propagation. Cache 30 min, actualisation automatique.",
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },

  // Previsions probabilistes (indicatives, hors score v3.4)
  ensembleRain: {
    text: "Part des membres des ensembles météo (ECMWF 51, ICON-EU 40, ICON-D2 20) dont la pluie moyenne sur le bassin dépasse chaque seuil du modèle (24 h : 25/40/60 mm ; 72 h : 50/80/120 mm). Moyenne des modèles disponibles pour la fenêtre. N'entre pas dans le score.",
    link: { url: 'https://open-meteo.com/en/docs/ensemble-api', label: 'Open-Meteo Ensemble' },
  },
  glofas: {
    text: "Débit modélisé par GloFAS v4 (Copernicus) sur la maille Lidoire : médiane, bandes p10-p90 et min-max des 51 membres. Seuils p95/p99 de la réanalyse 1997-2025 de la même maille. Aucune station ne mesure la Lidoire : c'est un indicateur, pas une mesure.",
    link: { url: 'https://open-meteo.com/en/docs/flood-api', label: 'Open-Meteo Flood (GloFAS)' },
  },
  radarBasin: {
    text: "Pluie observée par le composite radar européen OPERA (2 km, cumul 1 h toutes les 15 min), moyennée sur le bassin versant de la Lidoire. Les cumuls 3/6/24 h additionnent les cumuls horaires ; * = heures manquantes.",
    link: { url: 'https://eumetnet.github.io/openradardata-documentation/', label: 'OPERA Open Radar Data' },
  },
  // Indicateurs hydrologiques (hors modele)
  indicatorsHeader: {
    text: "Indicateurs collectés pour l'affichage et l'évaluation, sans effet sur le score v3.4. Aucune station ne mesure la Lidoire : l'Eyraud (73,5 km2, rive droite) sert d'analogue de son ruissellement (ses 6 plus gros débits journaliers depuis 2022-11 tombent sur les 6 crues du moulin). La nappe alluviale de Mouliets et la marée réellement observée complètent le tableau.",
    link: { url: 'https://hubeau.eaufrance.fr/page/api-hydrometrie', label: "Hub'Eau" },
  },
  indicatorsPercentile: {
    text: "Rang de la valeur actuelle dans l'historique stocké depuis 2022-11 (moyennes journalières ; max journaliers pour la marée). p99 = seuls 1 % des jours ont fait plus. Les seuils de débit portent sur la moyenne 24 h.",
  },
} as const

export type TooltipKey = keyof typeof TOOLTIPS
