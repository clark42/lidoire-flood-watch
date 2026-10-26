// The four ways the mill floods (redesign spec section 2, physics of the
// CLAUDE.md "Controle physique du remous"): the Lidoire's own flood (runoff)
// first, the downstream Dordogne as a modulating factor, the direct overflow
// of the Dordogne (unknown threshold, rare), the alluvial water table.
// Pure: builds what MechanismCard draws from the API payloads.
import type {
  DriverValues,
  EnsembleForecast,
  EpisodeDriversData,
  EpisodeOutcome,
  FlowIndicator,
  GroundwaterIndicator,
  IndicatorsData,
  IndicatorTrend,
  LidoireProfileData,
  RealtimeData,
  RiskOutlook,
  StationData,
} from '../types'
import type { ValueKind } from '../components/ui/valueKinds'
import { formatNumber, formatQuantity } from './format'
import { formatAge, formatParisRelative, parseApiDate } from './time'
import { ensembleWindow, formatEnsembleStats, modelLabel, preferredModel } from './ensemble'
import {
  compareToFloods,
  comparisonSentence,
  railDomain,
  type RailEpisode,
  type RailSpec,
  type RailTick,
} from './rail'

export type MechanismId = 'runoff' | 'backwater' | 'overflow' | 'groundwater'
export type MechanismState = 'calm' | 'watch' | 'active' | 'unknown'

export const MECHANISM_STATE_LABELS: Record<MechanismState, string> = {
  calm: 'Calme',
  watch: 'À surveiller',
  active: 'Actif',
  unknown: 'Inconnu',
}

export const OUTCOME_LABELS: Record<EpisodeOutcome, string> = {
  flooded: 'inondé',
  near_miss: 'presque',
  false_positive: 'pas inondé',
  unknown: 'à annoter',
}

export const PESSAC_STATION_ID = 'P555001001'
const EYRAUD_KEY = 'EYRAUD'

/**
 * Historical marks of the Dordogne at Pessac (Vigicrues), not served by the
 * API: the recent record and the reference flood.
 */
export const PESSAC_REFERENCE_MARKS = [
  { value: 7.88, label: '7,88 (14/02/2026)' },
] as const

export interface MechanismFact {
  label: string
  value: string
  kind?: ValueKind
}

export interface MechanismRail {
  title: string
  spec: RailSpec
  comparison: string
  /** Basis of the compared values (now vs at the floods) */
  basis: string
}

export interface MechanismView {
  id: MechanismId
  /** Role in the chain: "Mécanisme principal", "Modulateur"... */
  role: string
  title: string
  state: MechanismState
  stateRule: string
  value: number | null
  valueText: string
  unit: string
  kind: ValueKind
  valueNote: string
  /** One-sentence physical reading of the card */
  lead: string
  rails: MechanismRail[]
  facts: MechanismFact[]
  source: string
}

export interface MechanismInputs {
  indicators?: IndicatorsData | null
  drivers?: EpisodeDriversData | null
  profile?: LidoireProfileData | null
  realtime?: RealtimeData | null
  outlook?: RiskOutlook | null
  ensemble?: EnsembleForecast | null
  now?: Date
}

const fmtM = (v: number) => `${formatNumber(v, 2)} m`
const fmtQ = (v: number) => `${formatQuantity(v)} m³/s`
/** Thresholds keep their decimal (17,5 m³/s, not 18) */
const fmtQThreshold = (v: number) => `${formatNumber(v, 1)} m³/s`
const fmtMm = (v: number) => `${formatNumber(v, 1)} mm`
const fmtNgf = (v: number) => `${formatNumber(v, 2)} m NGF`
const km = (m: number) => `${formatNumber(m / 1000, 1)} km`

export const TREND_LABELS: Record<IndicatorTrend, string> = {
  rising: 'en hausse',
  falling: 'en baisse',
  stable: 'stable',
}

function episodeDate(key: string): string {
  const [y, m, d] = key.split('-')
  return d && m && y ? `${d}/${m}/${y}` : key
}

/** Episodes of /api/episodes/drivers with a value on `pick`, as rail dots. */
export function railEpisodes(
  drivers: EpisodeDriversData | null | undefined,
  pick: (v: DriverValues) => number | null,
  format: (v: number) => string,
): RailEpisode[] {
  const out: RailEpisode[] = []
  for (const e of drivers?.episodes ?? []) {
    const value = pick(e.values)
    if (value == null || !Number.isFinite(value)) continue
    const infiltration = e.flags.includes('mechanism_infiltration_since_sealed')
    out.push({
      key: e.key,
      value,
      outcome: e.outcome,
      title: `${episodeDate(e.key)} · ${OUTCOME_LABELS[e.outcome]}${infiltration ? ' (infiltration)' : ''} · ${format(value)}`,
    })
  }
  return out
}

function eyraudOf(indicators: IndicatorsData | null | undefined): FlowIndicator | null {
  const runoff = indicators?.runoff ?? []
  return runoff.find((r) => r.key === EYRAUD_KEY) ?? runoff.find((r) => r.role === 'runoff_analog') ?? null
}

function pessacOf(realtime: RealtimeData | null | undefined): StationData | null {
  return realtime?.stations?.stations.find((s) => s.station_id === PESSAC_STATION_ID) ?? null
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Pessac max over 12.5 h: the profile envelope, else the outlook's H (same production function). */
export function pessacEnvelope(
  profile: LidoireProfileData | null | undefined,
  outlook: RiskOutlook | null | undefined,
): number | null {
  return num(profile?.confluence?.inputs?.pessac_max_m) ?? num(outlook?.inputs_summary?.backwater_level_m)
}

function pessacZeroNgf(inputs: MechanismInputs): number | null {
  const zeros = inputs.profile?.confluence?.ngf_zeros_m ?? inputs.drivers?.confluence?.ngf_zeros_m
  return num(zeros?.[PESSAC_STATION_ID])
}

/** Pessac stage that would put a flat Dordogne water level at the mill ground (upper bound, Pessac only). */
export function pessacAtMillGround(inputs: MechanismInputs): number | null {
  const ground = num(inputs.profile?.mill?.ground_z_m_ngf)
  const zero = pessacZeroNgf(inputs)
  return ground != null && zero != null ? ground - zero : null
}

function ageText(timestamp: string | null | undefined, now: Date): string {
  const at = parseApiDate(timestamp)
  return at ? formatAge(at, now.getTime()) : '–'
}

function rail(
  title: string,
  spec: RailSpec,
  format: (v: number) => string,
  basis: string,
): MechanismRail {
  return {
    title,
    spec,
    comparison: comparisonSentence(spec.value, compareToFloods(spec.value, spec.episodes), format),
    basis,
  }
}

// --- 1. The Lidoire's own flood (runoff), Eyraud as the analog ------------

function runoffCard(inputs: MechanismInputs, now: Date): MechanismView {
  const eyraud = eyraudOf(inputs.indicators)
  const q = eyraud?.available ? eyraud.q_m3s : null
  const watch = eyraud?.thresholds_daily_m3s?.watch ?? null
  const alert = eyraud?.thresholds_daily_m3s?.alert ?? null
  const exceeded = eyraud?.threshold?.exceeded ?? []
  const state: MechanismState =
    q == null ? 'unknown' : exceeded.includes('alert') ? 'active' : exceeded.includes('watch') ? 'watch' : 'calm'

  const floodsQ = railEpisodes(inputs.drivers, (v) => v.eyraud_q_m3s, fmtQ)
  const ticks: RailTick[] = []
  if (watch != null) ticks.push({ value: watch, label: 'p99', kind: 'threshold' })
  if (alert != null) ticks.push({ value: alert, label: 'alerte', kind: 'threshold', provisional: true })
  const qSpec: RailSpec = {
    domain: railDomain([q, ...floodsQ.map((e) => e.value), watch, alert], { log: true }),
    log: true,
    value: q,
    valueLabel: q == null ? '' : `maint. ${formatQuantity(q)}`,
    valueKind: 'analog',
    episodes: floodsQ,
    ticks,
    ariaLabel: `Débit de l'Eyraud maintenant (${q == null ? 'indisponible' : fmtQ(q)}) comparé aux épisodes passés, échelle logarithmique`,
  }

  const rain = inputs.realtime?.rainfall
  const rainObs = num(rain?.last_72h_mm)
  const rainFc = num(rain?.forecast_72h_mm)
  const floodsRain = railEpisodes(inputs.drivers, (v) => v.rain_72h_mm, fmtMm)
  const thresholds72 = inputs.ensemble?.rain?.thresholds?.['72h'] ?? []
  const rainSpec: RailSpec = {
    domain: railDomain([rainObs, rainFc, ...floodsRain.map((e) => e.value), ...thresholds72.slice(0, 1)], { floor: 0, minSpan: 20 }),
    value: rainObs,
    valueLabel: rainObs == null ? '' : `72 h passées ${formatNumber(rainObs, 1)}`,
    valueKind: 'measured',
    episodes: floodsRain,
    ticks: thresholds72.slice(0, 1).map((t) => ({ value: t, label: `${formatNumber(t, 0)} mm`, kind: 'threshold' as const })),
    marker: rainFc == null ? null : { value: rainFc, label: `~ 72 h à venir ${formatNumber(rainFc, 1)} mm` },
    ariaLabel: `Pluie des 72 dernières heures (${rainObs == null ? 'indisponible' : fmtMm(rainObs)}) et prévue, comparée aux épisodes passés`,
  }

  const window = ensembleWindow(inputs.ensemble, '72h', 0)
  const model = preferredModel(window)
  const facts: MechanismFact[] = []
  if (eyraud?.q_mean_24h_m3s != null) facts.push({ label: 'Moyenne 24 h', value: fmtQ(eyraud.q_mean_24h_m3s), kind: 'analog' })
  if (eyraud?.trend) facts.push({ label: 'Tendance', value: TREND_LABELS[eyraud.trend] })
  if (rainObs != null) facts.push({ label: 'Pluie 72 h passées', value: fmtMm(rainObs), kind: 'measured' })
  if (rainFc != null) facts.push({ label: 'Pluie 72 h à venir (déterministe)', value: `${fmtMm(rainFc)}, sans bande`, kind: 'model' })
  if (window && model) {
    const stats = window.models[model]
    facts.push({
      label: `Ensemble ${modelLabel(inputs.ensemble, model)}, 72 h`,
      value: `${formatEnsembleStats(stats)}, membre max ${fmtMm(stats.max)}`,
      kind: 'model',
    })
  }
  if (eyraud?.last_timestamp) facts.push({ label: 'Dernière mesure Eyraud', value: ageText(eyraud.last_timestamp, now) })

  return {
    id: 'runoff',
    role: 'Mécanisme principal',
    title: 'Crue propre de la Lidoire (ruissellement)',
    state,
    stateRule:
      alert != null && watch != null
        ? `Actif au-delà de ${fmtQThreshold(alert)} en moyenne 24 h à l'Eyraud (seuil provisoire, en échantillon) ; à surveiller au-delà de ${fmtQThreshold(watch)} (p99 journalier).`
        : 'Seuils de l’Eyraud indisponibles.',
    value: q,
    valueText: q == null ? '–' : formatQuantity(q),
    unit: "m³/s à l'Eyraud",
    kind: q == null ? 'unavailable' : 'analog',
    valueNote: eyraud
      ? `instantané, ${eyraud.name}${eyraud.distance_km != null ? ` à ${formatNumber(eyraud.distance_km, 0)} km` : ''}`
      : 'Eyraud indisponible',
    lead:
      "Les inondations du moulin tombent au pic de l'Eyraud, petit bassin voisin qui réagit comme la Lidoire (aucune station sur la Lidoire). Pluie passée et prévue = l'avance.",
    rails: [
      rail("Débit de l'Eyraud", qSpec, fmtQ, 'Maintenant : instantané. Crues : max sur 12,5 h au pic.'),
      rail('Pluie sur 72 h', rainSpec, fmtMm, 'Maintenant : max des sources temps réel. Crues : Open-Meteo au moulin, 72 h avant le pic.'),
    ],
    facts,
    source:
      "Eyraud à La Force P540401002 (Hub'Eau, bassin 73,5 km²) : ses plus gros débits depuis 2022-11 tombent sur les dates de crue du moulin. Analogue : mesuré ailleurs, jamais au moulin.",
  }
}

// --- 2. Downstream Dordogne (raised level, backwater): modulating ----------

function backwaterCard(inputs: MechanismInputs, now: Date): MechanismView {
  const h = pessacEnvelope(inputs.profile, inputs.outlook)
  const station = pessacOf(inputs.realtime)
  const yellow = num(station?.threshold_yellow)
  const orange = num(station?.threshold_orange)
  const state: MechanismState =
    h == null ? 'unknown' : orange != null && h >= orange ? 'active' : yellow != null && h >= yellow ? 'watch' : 'calm'
  const atMill = pessacAtMillGround(inputs)

  const floods = railEpisodes(inputs.drivers, (v) => v.pessac_m, fmtM)
  const ticks: RailTick[] = []
  if (yellow != null) ticks.push({ value: yellow, label: 'jaune', kind: 'threshold' })
  if (orange != null) ticks.push({ value: orange, label: 'orange', kind: 'threshold' })
  if (atMill != null) ticks.push({ value: atMill, label: `≈ ${formatNumber(atMill, 1)} moulin`, kind: 'estimated' })
  const spec: RailSpec = {
    domain: railDomain([h, ...floods.map((e) => e.value), yellow, orange, atMill], { floor: 0, minSpan: 4 }),
    value: h,
    valueLabel: h == null ? '' : `maint. ${formatNumber(h, 2)}`,
    valueKind: 'measured',
    episodes: floods,
    ticks,
    ariaLabel: `Niveau de Pessac, max sur 12,5 h (${h == null ? 'indisponible' : fmtM(h)}), comparé aux épisodes passés`,
  }

  const bw = inputs.profile?.backwater
  const conf = inputs.profile?.confluence
  const facts: MechanismFact[] = []
  let reach = 'Portée du remous indisponible (profil en long non servi).'
  if (bw && bw.status !== 'unknown') {
    if (bw.reaches_mill) reach = 'Le remous atteindrait le moulin (estimation, plan d’eau horizontal).'
    else if (bw.status === 'none') reach = 'Pas de remous dans la Lidoire : la Dordogne est sous le lit à la confluence.'
    else if (bw.connected_distance_below_mill_m != null) {
      reach = `Le remous s'arrête à ≈ ${km(bw.connected_distance_below_mill_m)} sous le moulin.`
    }
  }
  if (conf?.z_c_m_ngf != null) facts.push({ label: 'Niveau à la confluence Z_c', value: `≈ ${fmtNgf(conf.z_c_m_ngf)}`, kind: 'estimated' })
  const margin = inputs.profile?.margin?.mill_ground_minus_z_c_m
  if (margin != null) facts.push({ label: 'Sous le terrain du moulin', value: `≈ ${formatNumber(margin, 1)} m`, kind: 'estimated' })
  if (station?.current_level) {
    facts.push({ label: 'Pessac maintenant', value: fmtM(station.current_level.height_m), kind: 'measured' })
  }
  const lib = conf?.inputs?.libourne_max_m
  if (lib != null) facts.push({ label: 'Libourne, max 12,5 h (marée)', value: fmtM(lib), kind: 'measured' })
  const gate = num(inputs.outlook?.inputs_summary?.tide_gate)
  if (gate != null) facts.push({ label: 'Gate de marée du score v3.4', value: formatNumber(gate, 2), kind: 'model' })
  const pm = inputs.outlook?.next_high_tide
  if (pm) {
    facts.push({
      label: 'Prochaine pleine mer, confluence',
      value: `${formatParisRelative(pm.timestamp, now)}${pm.coefficient != null ? `, coef. ${pm.coefficient}` : ''}`,
      kind: 'estimated',
    })
  }

  return {
    id: 'backwater',
    role: 'Modulateur',
    title: 'Dordogne aval (niveau relevé, remous)',
    state,
    stateRule:
      yellow != null && orange != null
        ? `Relevé (actif) au-delà de ${fmtM(orange)} à Pessac (seuil orange), à surveiller au-delà de ${fmtM(yellow)} (jaune) : max sur 12,5 h, une marée complète.`
        : 'Seuils de Pessac indisponibles.',
    value: h,
    valueText: h == null ? '–' : formatNumber(h, 2),
    unit: 'm à Pessac',
    kind: h == null ? 'unavailable' : 'measured',
    valueNote: 'max sur 12,5 h, à 7 km en amont de la confluence',
    lead: `Une Dordogne haute freine l'évacuation de la Lidoire, sans atteindre le moulin seule : aux crues labellisées, le remous s'est arrêté 2,7 à 3,4 km en aval. ${reach}`,
    rails: [rail('Niveau de Pessac', spec, fmtM, 'Maintenant et crues : max sur 12,5 h.')],
    facts,
    source:
      "Hub'Eau Pessac P555001001 (mesuré). Remous : profil en long IGN RGE ALTI de la Lidoire, plan d'eau horizontal à Z_c (estimé, borne haute : ignore le débit propre de la Lidoire).",
  }
}

// --- 3. Direct overflow of the Dordogne (unknown threshold, rare) ---------

function overflowCard(inputs: MechanismInputs): MechanismView {
  const station = pessacOf(inputs.realtime)
  const h = station?.current_level ? station.current_level.height_m : null
  const atMill = pessacAtMillGround(inputs)
  // Highest Pessac stage at which the mill stayed dry (labelled)
  const dry = (inputs.drivers?.episodes ?? [])
    .filter((e) => (e.outcome === 'false_positive' || e.outcome === 'near_miss') && e.values.pessac_m != null)
    .sort((a, b) => (b.values.pessac_m as number) - (a.values.pessac_m as number))[0]
  const low = dry ? (dry.values.pessac_m as number) : null
  const bandOk = low != null && atMill != null && atMill > low
  const state: MechanismState =
    h == null || !bandOk ? 'unknown' : h >= atMill ? 'active' : h >= low ? 'watch' : 'calm'

  const floods = railEpisodes(inputs.drivers, (v) => v.pessac_m, fmtM)
  const ticks: RailTick[] = PESSAC_REFERENCE_MARKS.map((m) => ({ ...m, kind: 'reference' as const }))
  const spec: RailSpec = {
    domain: railDomain([h, ...floods.map((e) => e.value), atMill, ...PESSAC_REFERENCE_MARKS.map((m) => m.value)], {
      floor: 0,
      minSpan: 6,
    }),
    value: h,
    valueLabel: h == null ? '' : `maint. ${formatNumber(h, 2)}`,
    valueKind: 'measured',
    episodes: floods,
    ticks,
    band: bandOk ? { from: low, to: atMill, label: 'seuil inconnu' } : null,
    ariaLabel: `Niveau instantané de Pessac (${h == null ? 'indisponible' : fmtM(h)}) face à la bande du seuil de débordement, inconnu`,
  }

  const facts: MechanismFact[] = []
  if (dry && low != null) facts.push({ label: 'Plus haut niveau resté sec', value: `${fmtM(low)} le ${episodeDate(dry.key)}`, kind: 'measured' })
  if (atMill != null) facts.push({ label: 'Dordogne au terrain du moulin', value: `Pessac ≈ ${fmtM(atMill)}`, kind: 'estimated' })
  const pessacQ = inputs.indicators?.dordogne.find((d) => d.station_id === PESSAC_STATION_ID)
  if (pessacQ?.q_m3s != null) facts.push({ label: 'Débit à Pessac', value: fmtQ(pessacQ.q_m3s), kind: 'measured' })
  const projected = num(inputs.outlook?.inputs_summary?.pessac_projected_max_m)
  if (projected != null) facts.push({ label: 'Max projeté sur 72 h', value: `${fmtM(projected)}, sans bande`, kind: 'model' })

  return {
    id: 'overflow',
    role: 'Rare, seuil inconnu',
    title: 'Débordement direct de la Dordogne',
    state,
    stateRule: bandOk
      ? `Seuil inconnu, montré comme une bande : de ${fmtM(low)} (resté sec) à ≈ ${fmtM(atMill)} (Dordogne au terrain du moulin). À surveiller dans la bande, actif au-delà.`
      : 'Bande du seuil indisponible (valeurs aux crues ou profil absents).',
    value: h,
    valueText: h == null ? '–' : formatNumber(h, 2),
    unit: 'm à Pessac',
    kind: h == null ? 'unavailable' : 'measured',
    valueNote: 'instantané',
    lead:
      "Au-delà d'un seuil jamais observé, la Dordogne inonderait le moulin quel que soit le débit de la Lidoire. Même le record du 14/02/2026 (7,88 m) est resté sous le terrain du moulin.",
    rails: [rail('Niveau de Pessac', spec, fmtM, 'Maintenant : instantané. Crues : max sur 12,5 h.')],
    facts,
    source: "Hub'Eau Pessac (mesuré), terrain du moulin 12,0 m NGF (IGN, estimé, le plancher est inconnu), record Vigicrues.",
  }
}

// --- 4. Alluvial water table ----------------------------------------------

function groundwaterCard(inputs: MechanismInputs, now: Date): MechanismView {
  const gw: GroundwaterIndicator | null = inputs.indicators?.groundwater?.[0] ?? null
  const level = gw?.available ? gw.level_m_ngf : null
  const watch = gw?.thresholds_m_ngf?.watch ?? null
  const exceeded = gw?.threshold?.exceeded ?? []
  const state: MechanismState =
    level == null
      ? 'unknown'
      : exceeded.includes('watch')
        ? 'active'
        : gw?.percentile != null && gw.percentile >= 90
          ? 'watch'
          : 'calm'
  const floods = railEpisodes(inputs.drivers, (v) => v.piezo_m_ngf, fmtNgf)
  const spec: RailSpec = {
    domain: railDomain([level, ...floods.map((e) => e.value), watch], { minSpan: 1.5 }).map((v, i) =>
      i === 0 ? Math.floor((v - 0.3) * 2) / 2 : v,
    ) as [number, number],
    value: level,
    valueLabel: level == null ? '' : `maint. ${formatNumber(level, 2)}`,
    valueKind: 'measured',
    episodes: floods,
    ticks: watch != null ? [{ value: watch, label: formatNumber(watch, 1), kind: 'threshold', provisional: true }] : [],
    ariaLabel: `Niveau de la nappe alluviale (${level == null ? 'indisponible' : fmtNgf(level)}) comparé aux épisodes passés`,
  }
  const facts: MechanismFact[] = []
  if (gw?.change_24h_m != null) facts.push({ label: 'Variation 24 h', value: `${gw.change_24h_m >= 0 ? '+' : ''}${formatNumber(gw.change_24h_m * 100, 0)} cm`, kind: 'measured' })
  if (gw?.percentile != null) facts.push({ label: 'Percentile', value: `p${formatNumber(gw.percentile, 0)} (${gw.percentile_basis})` })
  if (gw?.last_timestamp) facts.push({ label: 'Dernière mesure', value: `${ageText(gw.last_timestamp, now)} (latence ~12 h)` })

  return {
    id: 'groundwater',
    role: 'Contexte lent',
    title: 'Nappe alluviale',
    state,
    stateRule:
      watch != null
        ? `Active au-dessus de ${fmtNgf(watch)} (atteint à chaque crue connue : seuil provisoire, en échantillon) ; à surveiller au-delà du p90.`
        : 'Seuil de la nappe indisponible.',
    value: level,
    valueText: level == null ? '–' : formatNumber(level, 2),
    unit: 'm NGF',
    kind: level == null ? 'unavailable' : 'measured',
    valueNote: gw ? `${gw.name}${gw.distance_km != null ? `, ${formatNumber(gw.distance_km, 1)} km` : ''}` : 'piézomètre indisponible',
    lead: 'Une nappe haute sature les sols et prolonge les crues ; elle a causé la crue par infiltration de 2023 (colmatée depuis).',
    rails: [rail('Niveau de la nappe', spec, fmtNgf, 'Maintenant : dernière mesure. Crues : dernière mesure avant le pic.')],
    facts,
    source: "Piézomètre BRGM 08048X0088/P (Hub'Eau niveaux_nappes), alluvions de la Dordogne.",
  }
}

/** The four mechanism cards, in reading order (runoff first). */
export function buildMechanisms(inputs: MechanismInputs): MechanismView[] {
  const now = inputs.now ?? new Date()
  return [runoffCard(inputs, now), backwaterCard(inputs, now), overflowCard(inputs), groundwaterCard(inputs, now)]
}
