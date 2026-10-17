/**
 * Tailwind CSS config for the precis web UI.
 *
 * Production build: `scripts/ship` regenerates `src/precis_web/static/tailwind.css`
 * from this content scan on every ship (replacing the in-browser Play CDN, which
 * warns "should not be used in production" and is slower on first paint).
 *
 * `content` must cover EVERY source that emits Tailwind class names — the
 * templates, the inline `<script>` blocks inside them, the Python that renders
 * HTML chips (linkify.py), and the hand-written static JS. A class that never
 * appears literally in one of these files is purged, so keep this list honest.
 * Regenerate by hand with (unminified, like scripts/ship, so concurrent
 * rebuilds stay mergeable):
 *   npx tailwindcss@3 -c tailwind.config.js \
 *     -i src/precis_web/static/tailwind.src.css \
 *     -o src/precis_web/static/tailwind.css
 *
 * Colours: every token below is a CSS variable defined (light and dark) in
 * tailwind.src.css, which explains the vocabulary. A token name `a-b` becomes
 * the class `bg-a-b` / `text-a-b` / `border-a-b`; a token added here needs its
 * variable there, in both schemes.
 */
const FAMILIES = ['success', 'danger', 'warning', 'info', 'accent', 'teal', 'indigo', 'fuchsia']
const per = (steps) => FAMILIES.flatMap((f) => steps.map((s) => (s ? `${f}-${s}` : f)))

const TOKENS = {
  bg: [
    'canvas', 'surface', 'surface-subtle', 'surface-muted', 'surface-emphasis', 'surface-strong',
    'solid-soft', 'solid', 'solid-strong', 'scrim',
    'chrome', 'chrome-raised', 'chrome-hover', 'chrome-active',
    ...per(['subtle', 'muted', 'emphasis', 'strong', 'solid-light', 'solid-soft', 'solid', 'solid-strong']),
  ],
  text: [
    'fg-strong', 'fg', 'fg-secondary', 'fg-muted', 'fg-subtle', 'fg-faint', 'fg-ghost',
    'on-solid', 'on-bright',
    'chrome', 'chrome-strong', 'chrome-soft', 'chrome-muted', 'chrome-faint', 'chrome-brand', 'chrome-link',
    ...per(['faint', 'soft', '', 'strong', 'deep']),
  ],
  border: [
    'line-subtle', 'line', 'line-strong', 'line-heavy', 'line-solid',
    'chrome', 'chrome-brand',
    ...per(['subtle', '', 'strong', 'emphasis', 'solid']),
  ],
}

// Nested so `teal`/`indigo` merge with Tailwind's own palette objects instead
// of replacing them: {success: {DEFAULT, soft, ...}, surface: {DEFAULT, subtle}}.
function palette(prop) {
  const out = {}
  for (const token of TOKENS[prop]) {
    const [head, ...rest] = token.split('-')
    out[head] = out[head] || {}
    out[head][rest.length ? rest.join('-') : 'DEFAULT'] = `rgb(var(--${prop}-${token}) / <alpha-value>)`
  }
  return out
}

module.exports = {
  content: [
    './src/precis_web/templates/**/*.j2',
    './src/precis_web/**/*.py',
    './src/precis_web/static/paper-viewer.js',
    './src/precis_web/static/cad-tessellate.js',
    './src/precis_web/static/blocktree-3d.js',
    './src/precis_web/static/topology-cloud.js',
  ],
  theme: {
    extend: {
      backgroundColor: palette('bg'),
      textColor: palette('text'),
      // DEFAULT is what a bare `border` (and preflight) paints.
      borderColor: { DEFAULT: 'rgb(var(--border-line) / <alpha-value>)', ...palette('border') },
      ringColor: palette('border'),
      placeholderColor: palette('text'),
      // SVG: inks from the text tokens; a shape's body (`fill-surface`) and
      // gridlines (`stroke-line`) follow the page surface and its lines.
      fill: { ...palette('text'), surface: 'rgb(var(--bg-surface) / <alpha-value>)' },
      stroke: { ...palette('text'), line: palette('border').line },
    },
  },
}
