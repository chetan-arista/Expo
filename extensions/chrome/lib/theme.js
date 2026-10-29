// Light / Dark / System and Glass effects On / Off, as the dashboard's Settings > User Interface does it: the choice
// goes on <html> (data-theme, data-glass) and styles.css does the rest. System follows the computer's setting.

export function applyAppearance(settings, root = document.documentElement) {
  const theme = settings && settings.theme;
  if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
  else root.removeAttribute('data-theme');
  if (settings && settings.glass === 'off') root.setAttribute('data-glass', 'off');
  else root.removeAttribute('data-glass');
}

/** Small DOM helper: an element with a class and text (always textContent, never HTML). */
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
