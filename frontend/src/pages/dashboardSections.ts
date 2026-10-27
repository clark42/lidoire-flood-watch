// Sections of the Dashboard, in reading order (redesign spec, section 2):
// decide (5 s), understand why (30 s), then explore. Sections still to
// come (flood plan, analogs, 30-day GloFAS) are added with their panels.

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
    label: 'Replay',
    title: 'Revoir une date',
    description: 'TimeMachine : le tableau de bord tel qu’il était à une date passée ou lors d’une crue.',
  },
  {
    id: 'carte',
    label: 'Carte',
    title: 'Le bassin',
    description: 'Stations, marée et pluviomètres autour du moulin.',
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
    description: 'Fraîcheur des sources et dernière mise à jour.',
  },
] as const satisfies readonly DashboardSectionDef[]

export type DashboardSectionId = (typeof DASHBOARD_SECTIONS)[number]['id']

export function sectionDef(id: DashboardSectionId): DashboardSectionDef {
  return DASHBOARD_SECTIONS.find((s) => s.id === id)!
}
