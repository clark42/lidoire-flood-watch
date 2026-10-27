// Sections of the Dashboard, in reading order (redesign spec, section 2):
// decide (5 s), understand why (30 s), then explore. Sections still to
// come (analogs, 30-day GloFAS) are added with their panels.

export interface DashboardSectionDef {
  id: string
  /** Label in the sticky section bar */
  label: string
  /** Heading of the section (absent: the section supplies its own) */
  title?: string
  description?: string
}

export const DASHBOARD_SECTIONS = [
  { id: 'maintenant', label: 'Maintenant' },
  {
    id: 'pourquoi',
    label: 'Pourquoi',
    title: 'Pourquoi ce niveau',
    description:
      "Le moulin n'est jamais mesuré : on regarde ce qui l'inonde. D'abord la crue propre de la Lidoire (ruissellement), puis la Dordogne aval qui la freine, le débordement direct (rare) et la nappe. Chaque rail place la valeur du moment parmi les crues passées.",
  },
  {
    id: 'prochaines-heures',
    label: '72 h',
    title: 'Les prochaines heures',
    description: 'Score prévu, pluie d’ensemble, pleines mers et Pessac sur un même axe (heure de Paris, nuits grisées).',
  },
  {
    id: 'agir',
    label: 'Agir',
    title: 'Agir',
    description: 'La liste du moulin pour le niveau prévu, le relevé terrain qui manque au modèle, et ce qui ferait changer d’avis.',
  },
  {
    id: 'replay',
    label: 'Coupe & replay',
    title: 'De la confluence au moulin',
    description:
      'Profil en long de la Lidoire en m NGF : jusqu’où remonte la Dordogne. Rejouez 10 jours ou une crue, les séries défilent ensemble.',
  },
  {
    id: 'plan',
    label: 'Plan des crues',
    title: 'Plan des crues',
    description:
      'Chaque épisode labellisé selon ses deux moteurs : le débit de l’Eyraud (ruissellement, analogue de la Lidoire) et le niveau de Pessac (remous). Le point du jour et sa trace.',
  },
  {
    id: 'analogues',
    label: 'Analogues',
    title: 'Ça ressemble à…',
    description:
      'Les épisodes labellisés les plus proches de la situation du moment, moteur par moteur (Pessac, Eyraud, pluie 72 h, nappe).',
  },
  {
    id: 'carte',
    label: 'Carte',
    title: 'Le bassin',
    description:
      'Bassin topographique de la Lidoire (BD TOPAGE), rivières colorées par le percentile du niveau de leur station, tronçons Vigicrues et radar.',
  },
  {
    id: 'glofas',
    label: '30 jours',
    title: 'Le mois qui vient',
    description:
      'Débit simulé par GloFAS dans la maille Lidoire (5 km) : 51 membres, comparés à la climatologie de la maille.',
  },
  {
    id: 'pluie-sol',
    label: 'Pluie et sol',
    title: 'Pluie et sol',
    description: 'Cumuls par source, pluie du bassin et état des sols.',
  },
  {
    id: 'fiabilite',
    label: 'Fiabilité',
    title: 'Fiabilité',
    description: 'Ce qui était prévu, ce qui est arrivé, et l’état des sources.',
  },
] as const satisfies readonly DashboardSectionDef[]

export type DashboardSectionId = (typeof DASHBOARD_SECTIONS)[number]['id']

export function sectionDef(id: DashboardSectionId): DashboardSectionDef {
  return DASHBOARD_SECTIONS.find((s) => s.id === id)!
}
