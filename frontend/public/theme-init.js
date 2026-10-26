// Applies the forced theme before the first paint (no flash of the wrong
// theme). Loaded as a classic script from index.html: the CSP forbids inline
// scripts. Same key and values as src/theme/theme.ts.
try {
  var pref = window.localStorage.getItem('lfw_theme')
  if (pref === 'light' || pref === 'dark') document.documentElement.setAttribute('data-theme', pref)
} catch (e) {
  // Storage blocked: follow the OS theme
}
