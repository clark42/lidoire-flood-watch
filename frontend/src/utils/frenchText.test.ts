import { describe, expect, it } from 'vitest'
import { accentLabel } from './frenchText'

describe('accentLabel', () => {
  it.each([
    // backend/app/api/timeline.py series names
    ['Debit Lidoire GloFAS (mediane)', 'Débit Lidoire GloFAS (médiane)'],
    ['Maree estimee confluence Lidoire', 'Marée estimée confluence Lidoire'],
    ['Humidite sol 0-7 cm (ECMWF)', 'Humidité sol 0-7 cm (ECMWF)'],
    ['Temperature sol 0-7 cm', 'Température sol 0-7 cm'],
    ['Evapotranspiration (ET0)', 'Évapotranspiration (ET0)'],
    ['Composante Ouest (maree)', 'Composante Ouest (marée)'],
    ['Prevision risque (7j)', 'Prévision risque (7j)'],
    ['Niveau projete', 'Niveau projeté'],
    // backend/app/services/indicators.py + config.py station names
    ['Debit Eyraud a La Force', 'Débit Eyraud à La Force'],
    ['Debit Dordogne a Lamonzie-Saint-Martin', 'Débit Dordogne à Lamonzie-Saint-Martin'],
    ["Maree observee Bec d'Ambes (Hub'Eau)", "Marée observée Bec d'Ambès (Hub'Eau)"],
    // backend/app/api/status.py labels and notes
    ['Marees (PM/BM a venir)', 'Marées (PM/BM à venir)'],
    ['Score de risque v3.4 (temps reel)', 'Score de risque v3.4 (temps réel)'],
    ['Vigilance officielle (Vigicrues, Meteo-France)', 'Vigilance officielle (Vigicrues, Météo-France)'],
    ['non configure (NETATMO_CLIENT_ID/SECRET)', 'non configuré (NETATMO_CLIENT_ID/SECRET)'],
    ['desactive (RADAR_ENABLED=false)', 'désactivé (RADAR_ENABLED=false)'],
    ['aucun jeton : autoriser via /api/auth/netatmo', 'aucun jeton : autoriser via /api/auth/netatmo'],
    ["Hub'Eau Hydrometrie", "Hub'Eau Hydrométrie"],
    ['La Meteo Agricole', 'La Météo Agricole'],
    [
      'Previsions meteo (AROME/ARPEGE) et donnees historiques',
      'Prévisions météo (AROME/ARPEGE) et données historiques',
    ],
    [
      'Cle API requise ; seules les 5 stations les plus proches sont interrogees',
      'Clé API requise ; seules les 5 stations les plus proches sont interrogées',
    ],
    ['A venir', 'À venir'],
  ])('%s -> %s', (raw, expected) => {
    expect(accentLabel(raw)).toBe(expected)
  })

  it('leaves brand names intact', () => {
    expect(accentLabel('Open-Meteo (Moulin)')).toBe('Open-Meteo (Moulin)')
    expect(accentLabel('Open-Meteo Marine')).toBe('Open-Meteo Marine')
    expect(accentLabel("Niveau Pessac-sur-Dordogne (Hub'Eau)")).toBe(
      "Niveau Pessac-sur-Dordogne (Hub'Eau)",
    )
    expect(accentLabel('https://api.open-meteo.com/v1/meteofrance')).toBe(
      'https://api.open-meteo.com/v1/meteofrance',
    )
  })

  it('keeps an already accented label unchanged', () => {
    const accented = 'Débit Lidoire GloFAS (médiane) à venir, Météo-France, marée observée'
    expect(accentLabel(accented)).toBe(accented)
    expect(accentLabel(accentLabel('Maree estimee a la confluence'))).toBe(
      'Marée estimée à la confluence',
    )
  })

  it('keeps the verb "a" after a subject', () => {
    expect(accentLabel('il y a 3 h')).toBe('il y a 3 h')
    expect(accentLabel('on a mesure')).toBe('on a mesure')
  })

  it('passes empty values through', () => {
    expect(accentLabel('')).toBe('')
    expect(accentLabel(null)).toBeNull()
    expect(accentLabel(undefined)).toBeUndefined()
  })
})
