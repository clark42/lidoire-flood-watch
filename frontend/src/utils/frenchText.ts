// Display normaliser for backend-provided French labels.
//
// The backend sends some names, notes and descriptions without accents
// ("Debit Lidoire GloFAS (mediane)", "Maree estimee confluence Lidoire",
// "Meteo-France"). accentLabel restores the accents of the known words, for
// display only: ids, keys and API values are never passed through it.
// Already accented text is returned unchanged, proper names such as
// "Open-Meteo" and "Hub'Eau" are left intact.

/** Unaccented (lowercase) -> accented word. Matched whole-word, case-insensitive. */
const WORDS: Record<string, string> = {
  ambes: 'ambès',
  apres: 'après',
  autorise: 'autorisé',
  autorisee: 'autorisée',
  calcule: 'calculé',
  calculee: 'calculée',
  camera: 'caméra',
  cameras: 'caméras',
  cle: 'clé',
  completee: 'complétée',
  completees: 'complétées',
  configure: 'configuré',
  configuree: 'configurée',
  connecte: 'connecté',
  connectee: 'connectée',
  debit: 'débit',
  debits: 'débits',
  decale: 'décalé',
  decalee: 'décalée',
  decalees: 'décalées',
  decales: 'décalés',
  decouverte: 'découverte',
  deficit: 'déficit',
  deja: 'déjà',
  delai: 'délai',
  delais: 'délais',
  derniere: 'dernière',
  dernieres: 'dernières',
  desactive: 'désactivé',
  desactivee: 'désactivée',
  donnee: 'donnée',
  donnees: 'données',
  duree: 'durée',
  echec: 'échec',
  eleve: 'élevé',
  elevee: 'élevée',
  elevees: 'élevées',
  eleves: 'élevés',
  episode: 'épisode',
  episodes: 'épisodes',
  estime: 'estimé',
  estimee: 'estimée',
  estimees: 'estimées',
  estimes: 'estimés',
  etat: 'état',
  evapotranspiration: 'évapotranspiration',
  evenement: 'événement',
  evenements: 'événements',
  genere: 'généré',
  generee: 'générée',
  humidite: 'humidité',
  hydrometrie: 'hydrométrie',
  hydrometrique: 'hydrométrique',
  hydrometriques: 'hydrométriques',
  interrogee: 'interrogée',
  interrogees: 'interrogées',
  maree: 'marée',
  marees: 'marées',
  mediane: 'médiane',
  mesuree: 'mesurée',
  mesurees: 'mesurées',
  meteo: 'météo',
  modele: 'modèle',
  modeles: 'modèles',
  modelise: 'modélisé',
  modelisee: 'modélisée',
  modelisees: 'modélisées',
  modelises: 'modélisés',
  montee: 'montée',
  observe: 'observé',
  observee: 'observée',
  observees: 'observées',
  observes: 'observés',
  periode: 'période',
  periodes: 'périodes',
  perigueux: 'périgueux',
  perime: 'périmé',
  perimee: 'périmée',
  perimees: 'périmées',
  perimes: 'périmés',
  pluviometrie: 'pluviométrie',
  precipitation: 'précipitation',
  precipitations: 'précipitations',
  prevision: 'prévision',
  previsions: 'prévisions',
  prevu: 'prévu',
  prevue: 'prévue',
  prevues: 'prévues',
  prevus: 'prévus',
  probabilite: 'probabilité',
  projete: 'projeté',
  projetee: 'projetée',
  projetes: 'projetés',
  reconstitue: 'reconstitué',
  reconstituee: 'reconstituée',
  reconstituees: 'reconstituées',
  reconstitues: 'reconstitués',
  reel: 'réel',
  reelle: 'réelle',
  reelles: 'réelles',
  reels: 'réels',
  reseau: 'réseau',
  riberac: 'ribérac',
  riviere: 'rivière',
  rivieres: 'rivières',
  sature: 'saturé',
  saturee: 'saturée',
  selection: 'sélection',
  simultanee: 'simultanée',
  simultanees: 'simultanées',
  simultanement: 'simultanément',
  surveilles: 'surveillés',
  systeme: 'système',
  temperature: 'température',
  temperatures: 'températures',
  tres: 'très',
  video: 'vidéo',
  zero: 'zéro',
}

// Whole word: no letter, digit, "_", "/", "@" or "." + letter around it, so
// identifiers (NETATMO_CLIENT_ID), paths (/api/...) and domains are untouched.
// "Meteo" right after "Open-" is a brand and keeps its spelling.
const WORD_RE = new RegExp(
  `(?<![\\p{L}\\p{N}_/@.])(?<!Open-)(${Object.keys(WORDS).join('|')})(?![\\p{L}\\p{N}_/@]|\\.\\p{L})`,
  'giu',
)

// " a " is the preposition "à" in labels ("Eyraud a La Force", "PM/BM a
// venir"), except after a subject where it is the verb ("il y a", "on a").
const VERB_SUBJECT_RE = /(?:^|[\s'’(])(?:il|elle|on|qui|y|ne|cela|ça)$/iu
// Lowercase "a" anywhere, uppercase "A" only at the very start ("A venir")
const PREPOSITION_RE = /(^|[\s(])(a|^A)(?=\s+[\p{L}\p{N}])/gu

function matchCase(source: string, accented: string): string {
  if (source.length > 1 && source === source.toUpperCase()) return accented.toUpperCase()
  if (source[0] === source[0].toUpperCase()) return accented[0].toUpperCase() + accented.slice(1)
  return accented
}

/** Restore the French accents of a backend-provided label (display only). */
export function accentLabel(label: string): string
export function accentLabel(label: string | null | undefined): string | null | undefined
export function accentLabel(label: string | null | undefined): string | null | undefined {
  if (!label) return label
  const words = label.replace(WORD_RE, (match: string) => {
    const accented = WORDS[match.toLowerCase()]
    return accented ? matchCase(match, accented) : match
  })
  return words.replace(
    PREPOSITION_RE,
    (match: string, before: string, letter: string, offset: number) => {
      const head = words.slice(0, offset + before.length).trimEnd()
      if (head && VERB_SUBJECT_RE.test(head)) return match
      return `${before}${letter === 'A' ? 'À' : 'à'}`
    },
  )
}
