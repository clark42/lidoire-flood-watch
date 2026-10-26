// Honesty grammar of the redesign: labels and hints of the ValueTag kinds

/**
 * Nature of a displayed value (honesty grammar of the redesign):
 * - measured: observed by a station (solid line)
 * - estimated: interpolated or shifted (dashed line, "≈")
 * - model: computed or forecast by a model (dotted line, "~")
 * - analog: measured elsewhere and used as a proxy (dashed border)
 * - mockup: block waiting for an endpoint (hatched)
 * - unavailable: no value
 */
export type ValueKind = 'measured' | 'estimated' | 'model' | 'analog' | 'mockup' | 'unavailable'

export const VALUE_KIND_LABELS: Record<ValueKind, string> = {
  measured: 'mesuré',
  estimated: '≈ estimé',
  model: '~ modèle',
  analog: 'analogue',
  mockup: 'maquette',
  unavailable: 'indisponible',
}

export const VALUE_KIND_HINTS: Record<ValueKind, string> = {
  measured: 'Valeur mesurée par une station',
  estimated: 'Valeur estimée ou interpolée, pas mesurée à cet endroit',
  model: 'Valeur calculée ou prévue par un modèle',
  analog: 'Mesurée ailleurs, utilisée comme analogue',
  mockup: 'Maquette : la donnée n’est pas encore servie par l’API',
  unavailable: 'Donnée indisponible',
}

/** Prefix of a value of this kind ("≈ 3,61", "~ 0,08"). */
export const VALUE_KIND_PREFIX: Partial<Record<ValueKind, string>> = {
  estimated: '≈ ',
  model: '~ ',
}
