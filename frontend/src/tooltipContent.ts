export const TOOLTIPS = {
  // Stations
  rateOfRise: {
    text: "Vitesse de variation du niveau d'eau. +5 cm/h = montee moderee, +20 cm/h = montee rapide et dangereuse. Une valeur negative indique une decrue.",
    link: { url: 'https://www.vigicrues.gouv.fr/', label: 'Vigicrues' },
  },
  tendency: {
    text: 'Hausse = niveau en montee. Stable = pas de variation significative. Baisse = decrue en cours.',
  },
  delayHours: {
    text: "Temps estime pour qu'une crue observee a cette station atteigne le moulin. Permet d'anticiper.",
  },
  stationWeight: {
    text: 'Importance de cette station dans le calcul du risque. Les stations proches du moulin pesent plus.',
  },
  thresholds: {
    text: "Seuils d'alerte officiels Hub'Eau. Jaune = vigilance. Orange = risque de debordement. Rouge = crue majeure.",
    link: { url: 'https://hubeau.eaufrance.fr/page/api-hydrometrie', label: "Hub'Eau API" },
  },
  stationsHeader: {
    text: "Niveaux d'eau mesures en temps reel sur 7 stations du bassin Dordogne / Isle / Dronne. Le moulin est influence par ces 3 cours d'eau.",
  },

  // Marees
  tideCoefficient: {
    text: "Force de la maree de 20 (morte-eau) a 120 (vive-eau). Au-dessus de 90, les marees sont fortes et amplifient le risque de crue en bloquant l'ecoulement des rivieres.",
    link: { url: 'https://maree.info/82', label: 'Marees Libourne' },
  },
  tidePhaseBmPm: {
    text: "PM = Pleine Mer (maree haute), BM = Basse Mer (maree basse). Le risque est maximal a PM quand la maree empeche la riviere de s'ecouler vers l'ocean. Heures estimees a la confluence de la Lidoire : maree de l'embouchure de la Gironde decalee de 6 h (propagation mesuree : +4 h a Libourne, +7 h a Pessac).",
    link: { url: 'https://www.shom.fr/les-marees', label: 'SHOM - Les marees' },
  },
  tidePhaseHours: {
    text: "Heures avant/apres la pleine mer. PM-2h = 2 heures avant la pleine mer. Le risque est maximal dans l'heure autour de PM.",
  },

  // Pluie
  rain24h: {
    text: 'Cumul de pluie sur les dernieres 24 heures. Au-dela de 30mm, les sols commencent a saturer. Au-dela de 50mm, le ruissellement augmente fortement.',
  },
  rain72h: {
    text: 'Cumul sur 3 jours. Indique la saturation du bassin versant. Un sol deja gorge d\'eau ne peut plus absorber la pluie, qui ruisselle directement vers les rivieres.',
  },
  rain7d: {
    text: "Cumul sur 7 jours. Donne une vue de l'etat hydrique general du bassin de la Lidoire.",
  },
  rainfallForecast: {
    text: 'Prevision de pluie basee sur les modeles Meteo-France (AROME/ARPEGE) via Open-Meteo. Precision decroissante au-dela de 48h.',
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },

  // Risque
  riskScore: {
    text: 'Score composite 0-100 (modele v3.4) combinant 11 facteurs ponderes (rivieres, pluie, maree, vent, sol, video). La maree ne compte que si la Dordogne est haute (Pessac au-dessus de 3.0 m sur 12.5 h). Vert < 15 = normal. Jaune 15-35 = vigilance. Orange 35-55 = alerte. Rouge >= 55 = danger.',
  },
  conjunctionMultiplier: {
    text: "Bonus de risque (x1.0 a x1.5) quand maree haute + niveau eleve + montee des eaux se combinent. Ces conditions ensemble sont bien plus dangereuses que separement.",
  },

  // Facteurs de risque (pour le tableau de detail)
  factorRiver: {
    text: "Niveaux d'eau des stations Hub'Eau, moyenne ponderee par station (Pessac 30%, Libourne exclue car soumise a la maree). C'est le facteur dominant (28% du score).",
  },
  factorRain24h: {
    text: 'Pluie cumulee sur 24h. Contribue au ruissellement immediat vers les rivieres.',
  },
  factorRain72h: {
    text: 'Pluie cumulee sur 72h. Indique la saturation des sols du bassin versant.',
  },
  factorTide: {
    text: "Coefficient de maree. Les grandes marees bloquent l'ecoulement de la Dordogne vers l'ocean.",
  },
  factorTidePhase: {
    text: "Phase par rapport a la pleine mer. Le risque maximal est autour de PM (maree haute).",
  },
  factorTendency: {
    text: "Vitesse de montee des eaux. Une hausse rapide amplifie le risque meme si le niveau absolu est modere.",
  },
  factorMomentum: {
    text: "Nombre de stations en hausse simultanee. C'est le signal le plus fort d'inondation au moulin : les 5 episodes confirmes avaient en moyenne 4.4 stations en hausse vs 2.1 pour les faux positifs. Un niveau eleve mais stable (0 station en hausse) signifie que l'eau se retire, pas qu'elle arrive.",
  },
  factorRain7d: {
    text: 'Pluie cumulee sur 7 jours. Indique la saturation profonde du bassin versant de la Lidoire.',
  },
  factorWindWest: {
    text: "Vent d'ouest (composante westerly). Un vent fort d'ouest pousse la maree dans l'estuaire de la Gironde, amplifiant la surcote a Libourne.",
  },
  factorSoilSat: {
    text: "Saturation du sol (1.0=sec, 1.5=sature). Un sol sature ne peut plus absorber la pluie, qui ruisselle directement vers la Lidoire.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil' },
  },
  factorVideo: {
    text: "Niveau d'eau detecte par analyse video de la camera du moulin. Complement visuel aux capteurs.",
  },

  // Sol & Conditions agricoles
  soilMoisture: {
    text: "Humidite volumetrique du sol (m3/m3). 0.15 = sec, 0.25 = normal, 0.35 = humide, 0.45 = sature. Modele DWD ICON via Open-Meteo, couches 0-9 cm et 9-81 cm (entree du modele de risque). La Timeline affiche un autre modele (ECMWF, 0-7 / 7-100 cm), plus humide : les valeurs ne sont pas comparables.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil API' },
  },
  soilSaturationFactor: {
    text: "Facteur de saturation du sol (1.0 a 1.5). Amplifie l'impact de la pluie sur le ruissellement. 1.0 = sol sec (absorbe), 1.5 = sol sature (tout ruisselle).",
  },
  et0: {
    text: "Evapotranspiration de reference (mm/jour). Mesure l'eau qui s'evapore du sol et des plantes. Si < 0.5 mm/j, le sol seche tres lentement.",
  },
  waterBalance30d: {
    text: "Bilan hydrique sur 30 jours. Surplus = plus de pluie que la normale (sol gorge). Deficit = moins de pluie que la normale (sol sec). Source: La Meteo Agricole.",
    link: { url: 'https://www.lameteoagricole.net/', label: 'La Meteo Agricole' },
  },

  // Sources
  netatmoSource: {
    text: 'Reseau de stations meteo citoyennes. Fournit la pluviometrie locale en temps reel autour du bassin de la Lidoire.',
    link: { url: 'https://weathermap.netatmo.com/', label: 'Carte Netatmo' },
  },
  infoclimatSource: {
    text: 'Reseau de stations meteo amateur StatIC. Donnees complementaires aux capteurs Netatmo pour la pluviometrie du bassin.',
    link: { url: 'https://www.infoclimat.fr/stations-meteo/', label: 'Infoclimat' },
  },

  // Maree timeline
  timelineTide: {
    text: "Niveau marin modele (Open-Meteo Marine) a l'embouchure de la Gironde, en metres par rapport au niveau moyen, avec les heures decalees de 6 h pour estimer la maree a la confluence de la Lidoire. Seule la phase (heure des PM/BM) est representative : l'amplitude reste celle de l'embouchure. La sinusoide ~12h correspond au cycle naturel des marees. Attention : ce referentiel est different de celui des stations de riviere (zero local de chaque echelle).",
    link: { url: 'https://maree.info/82', label: 'Marees Libourne' },
  },

  // Timeline panels
  timelineLevels: {
    text: "Niveaux d'eau mesures en continu sur les stations du bassin (Dordogne, Isle, Dronne). Chaque courbe correspond a une station hydrometrique Hub'Eau. La maree (tirets, estimee a la confluence) a un referentiel different (niveau moyen de la mer) et est negative en basse mer.",
    link: { url: 'https://hubeau.eaufrance.fr/page/api-hydrometrie', label: "Hub'Eau API" },
  },
  timelineRainfall: {
    text: "Pluviometrie horaire mesuree et prevue. Sources : Open-Meteo (modeles AROME/ARPEGE), Netatmo (stations citoyennes) et Infoclimat (stations amateur). Un cumul > 10 mm/h indique une pluie forte.",
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },
  timelineRisk: {
    text: "Score de risque composite (0-100, modele v3.4) calcule toutes les heures. Combine niveaux de rivieres (28%), pluie (28%), tendance et momentum (21%), maree (10%) et autres facteurs. Vert < 15, Jaune < 35, Orange < 55, Rouge >= 55.",
  },
  timelineSoilMoisture: {
    text: "Humidite volumetrique du sol en m3/m3, modele ECMWF (IFS, ERA5 pour l'historique) via Open-Meteo. Couche superficielle (0-7cm) et profonde (7-100cm). Le Dashboard affiche le modele ICON (0-9 cm), plus sec : valeurs non comparables. Un sol sature (> 0.40) ne peut plus absorber la pluie.",
    link: { url: 'https://open-meteo.com/en/docs', label: 'Open-Meteo Soil API' },
  },
  timelineSoilTemp: {
    text: "Temperature du sol (0-7cm). Un sol gele (< 0C) est impermeable et provoque un ruissellement total. Donnees Open-Meteo historiques.",
  },
  timelineET0: {
    text: "Evapotranspiration de reference FAO (mm/jour). Quantite d'eau qui s'evapore du sol et transpire par les plantes. Si < 0.5 mm/j en hiver, le sol seche tres lentement et reste gorge d'eau.",
  },
  timelineNowcast: {
    text: "Prevision radar minute par minute de la pluie dans la prochaine heure. Donnees issues du radar meteorologique via La Meteo Agricole. Precision elevee mais horizon limite a 60 minutes.",
    link: { url: 'https://www.lameteoagricole.net/', label: 'La Meteo Agricole' },
  },

  // Bassin
  basinHeader: {
    text: "Moyenne ponderee de la pluie sur le bassin versant de la Lidoire, calculee a partir des stations Netatmo situees sur le trace de la riviere (amont 30%, milieu 40%, aval 30%, sur les positions qui ont des stations). Indisponible tant que Netatmo n'est pas autorise.",
  },

  // Previsions
  forecastHeader: {
    text: "Projection du score de risque sur les prochaines heures, basee sur les previsions de maree et de pluie. Les niveaux de riviere sont maintenus constants (pas de modele hydrologique).",
  },
  forecast: {
    text: "Prevision de risque sur 72h combinant : pluie horaire (Open-Meteo AROME), marees avec coefficients, et projection des niveaux de riviere basee sur les stations amont avec leurs delais de propagation. Cache 30 min, actualisation automatique.",
    link: { url: 'https://open-meteo.com/', label: 'Open-Meteo' },
  },
} as const

export type TooltipKey = keyof typeof TOOLTIPS
