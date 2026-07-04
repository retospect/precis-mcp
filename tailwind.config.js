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
 * Regenerate by hand with:
 *   npx tailwindcss@3 -c tailwind.config.js \
 *     -i src/precis_web/static/tailwind.src.css \
 *     -o src/precis_web/static/tailwind.css --minify
 */
module.exports = {
  content: [
    './src/precis_web/templates/**/*.j2',
    './src/precis_web/**/*.py',
    './src/precis_web/static/paper-viewer.js',
    './src/precis_web/static/cad-tessellate.js',
  ],
}
