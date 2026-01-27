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
    text: "PM = Pleine Mer (maree haute), BM = Basse Mer (maree basse). Le risque est maximal a PM quand la maree empeche la riviere de s'ecouler vers l'ocean.",
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
    text: 'Score composite 0-100 combinant 7 facteurs ponderes. Vert (0-25) = normal. Jaune (25-50) = vigilance. Orange (50-75) = alerte. Rouge (75-100) = danger.',
  },
  conjunctionMultiplier: {
    text: "Bonus de risque (x1.0 a x1.5) quand maree haute + niveau eleve + montee des eaux se combinent. Ces conditions ensemble sont bien plus dangereuses que separement.",
  },

  // Facteurs de risque (pour le tableau de detail)
  factorRiver: {
    text: "Niveaux d'eau sur 7 stations ponderes par distance au moulin. C'est le facteur dominant (50% du score).",
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
  factorVideo: {
    text: "Niveau d'eau detecte par analyse video de la camera du moulin. Complement visuel aux capteurs.",
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

  // Bassin
  basinHeader: {
    text: "Moyenne ponderee de la pluie sur le bassin versant de la Lidoire, calculee a partir des stations Netatmo situees sur le trace de la riviere. Amont pese plus car l'eau met du temps a descendre.",
  },

  // Previsions
  forecastHeader: {
    text: "Projection du score de risque sur les prochaines heures, basee sur les previsions de maree et de pluie. Les niveaux de riviere sont maintenus constants (pas de modele hydrologique).",
  },
} as const

export type TooltipKey = keyof typeof TOOLTIPS
