// Backend notes reach the UI as they are written for operators ("aucun
// jeton : autoriser via /api/auth/netatmo"). The page shows a plain
// sentence instead, and the "Connecter Netatmo" action where it applies.
// Pure; the backend text is not changed.

const NETATMO_AUTH_PATH = /\/api\/auth\/netatmo\b/i

/** True when a backend note asks to (re)authorise Netatmo. */
export function needsNetatmoConnect(text: string | null | undefined): boolean {
  return !!text && NETATMO_AUTH_PATH.test(text)
}

/** Plain sentence of a backend note: no API path, no operator jargon. */
export function plainText(text: string): string {
  return text
    .replace(/aucun jeton\s*:\s*autoriser via \/api\/auth\/netatmo/gi, 'Netatmo n’est pas connecté')
    .replace(/\s*(?:[:,(]\s*)?(?:autoriser|visiter|voir|visit)\s+(?:via\s+)?\/api\/[\w/{}.-]+\)?/gi, '')
    .replace(/\s*\(?(?:via\s+)?\/api\/[\w/{}.?=&-]+\)?/g, '')
    .replace(/\(\s*\)/g, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([.,;)])/g, '$1')
    .trim()
}
