Kinship is OBTV's partner data platform: one profile for every partner, viewer and caller, and the tools to understand and reach them. The brand is a working tool first. It should look like it belongs in a broadcast operations center, and feel like it is about people.

**Tagline:** Every partner, one relationship.

## The idea

The mark is a K drawn as a small graph. Four nodes are people; the strokes between them are the relationships that tie separate records into one person. That is literally what the product does: identity resolution links records into a single profile.

## Voice and copy

- Say **partner** for a donor, and **viewer** or **caller** where that is the relationship. Never "customer", "lead" or "consumer".
- Write the product name as **Kinship** in sentences and as the lowercase wordmark only in the logo. Never "KINSHIP" in running text.
- Plain, specific, calm. Name things by what the team recognizes: "Lapsed partners who gave $500 or more", not "Segment 12".
- Empty states say what to do next: "No data yet. Import a source to populate dashboards." Never reference build milestones or internal project names.
- Errors say what happened and how to fix it, without apologizing: "The file has no email or phone column. Map at least one identifier to continue."
- Numbers are exact in tables (`12,480`) and abbreviated only on KPI tiles (`1.24M`).
- No emoji in the product.

## Logo

Use the files in the Logos and App icons groups. Never redraw, re-type or recolor the mark.

- **Horizontal lockup** (`kinship-logo-horizontal-*`) is the default: sidebar, documents, slides, email signatures.
- **Stacked lockup** (`kinship-logo-stacked-*`) for square spaces: splash screens, title slides, signage.
- **Mark** (`kinship-mark-*`) alone when the name is already on screen or space is under `logo-min-lockup` (96px wide).
- **App icon and favicon** for browser tabs, bookmarks and home screens. At 16 to 32px always use `favicon.svg` or the favicon PNGs.
- **Sidebar wordmark** (`kinship-sidebar-wordmark.svg`) is sized for the console's 24px logo slot, top left of the sidebar.

Colorways:
- `-dark`: for `ground` and `surface` in the dark theme. Links in `ink` (Mist), nodes in `signal` (Signal Cyan). This is the primary logo.
- `-light`: for white and `ground` in the light theme. Links in Night Ink, nodes in Deep Cyan (`signal` light value).
- `-mono-white` and `-mono-black`: one-color printing, embroidery, photos and colored backgrounds.

Clear space: keep one node diameter (the width of a cyan dot) empty on every side. Minimum sizes: mark 16px, horizontal lockup 96px wide.

Don't: stretch or rotate it, add shadows, glows or gradients, put the two-color version on a busy photo, swap node and link colors, rebuild the wordmark in a different font, or place the dark colorway on a light background.

## Color

The console is dark-first. Build screens on `ground` with `surface` panels, `ink` text and `ink-muted` secondary text. `signal` is the only accent in the product: primary buttons (`signal` fill with `on-signal` text), links, selection (`signal-soft` background), the focus ring, and info states.

- Status colors `ok`, `warn` and `danger` mean state and nothing else. Every status also carries a word or icon, because `ok` and `danger` are not far enough apart in lightness to be told apart by color alone.
- `hearth` is the warm brand secondary. Use it in presentations, the social card and the brand cover, at most one element per layout. Keep it out of the console, where warm colors mean warnings.
- Charts use `chart-1` to `chart-8` in order. Every series holds at least 4:1 on `surface` in both themes. Axis labels use `ink-muted`, gridlines `line`.
- Control borders use `line-strong`; `line` is for dividers only.
- The light theme exists for printed reports, exported PDFs and documents. The console ships dark.

Focus ring: 2px solid `signal` with a 2px offset. It holds 3:1 or better on every surface in both themes.

## Type

- **Sora SemiBold** (`display`, `title`, `heading`) for the wordmark, page titles and panel headings. It is the brand's voice; use it sparingly.
- **Inter** (`body`, `body-strong`, `table`, `label`) for all interface text. Base size is 13px; tables are 12px.
- **JetBrains Mono** (`kpi`, `data`, `code`) for numbers, IDs, counts and JSON. Always set `font-variant-numeric: tabular-nums` on numbers so columns line up.
- `label` is set in uppercase with 0.06em tracking, for KPI eyebrows and table headers only.
- Bundle all three families with the app (the `fonts/` files, or `@fontsource/sora`, `@fontsource/inter`, `@fontsource/jetbrains-mono`). Never load them from a CDN at runtime.

## Layout and shape

- 8px grid (`space-2`). Page padding `space-6`, panel padding `space-4`, table rows `row-height` (32px).
- Dense and quiet. No hero sections or oversized cards; information density comes first.
- Radii are small: `radius-sm` for inputs, badges and chips; `radius-md` for buttons and panels; `radius-lg` for drawers and dialogs.
- Borders, not shadows, separate panels in the dark theme. Use one shadow only for floating layers (menus, drawers).
- Motion is short (120 to 180ms) and functional: drawers slide, toasts fade. Respect reduced-motion settings.

## Iconography

- Lucide icons (`lucide-react`) at 16px in the console, 1.75px stroke, colored `ink-muted` by default and `ink` when active.
- Icons label actions and navigation. Don't use them as decoration, and never use emoji in their place.
- The node-and-link motif from the mark can appear as a background pattern in presentations and the social card. Keep it out of the console.

## File reference

| Need | File |
| --- | --- |
| Console sidebar logo | `kinship-sidebar-wordmark.svg` |
| Browser tab | `favicon.svg`, `favicon-32.png`, `favicon-16.png` |
| Apple touch icon | `kinship-app-icon-180.png` |
| Web app manifest | `kinship-app-icon-192.png`, `kinship-app-icon-512.png` |
| Link previews (Open Graph) | `kinship-social-1200x630-1200.png` |
| Slides and documents, dark | `kinship-logo-horizontal-dark.svg` |
| Slides and documents, light | `kinship-logo-horizontal-light.svg` |
