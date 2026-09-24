# UI specification

## Look and feel

A dense, dark operations console in the style of a broadcast NOC. It is a working tool for analysts, so the priority is information density, legibility, and speed.

- Background `#0b0f14`, panels `#121821`, panel borders `#1f2a37`, primary text `#e6edf3`, secondary text `#8b98a5`.
- Accent `#3fb6ff` (cyan) for primary actions and selection. Status colors: ok `#3ecf8e`, warn `#f5a524`, error `#f0506e`, info `#3fb6ff`.
- Chart palette (categorical, in order): `#3fb6ff, #3ecf8e, #f5a524, #b48cff, #f0506e, #6ee7d8, #ffd166, #8b98a5`.
- Fonts bundled via `@fontsource`: Inter for UI (13 px base, 12 px in tables), JetBrains Mono for IDs, counts, JSON, and code.
- Tabular numbers everywhere (`font-variant-numeric: tabular-nums`). Large numbers abbreviated in KPI tiles (`1.24M`), full in tables with thousands separators.
- Compact spacing: 8 px grid, table row height 32 px, no oversized cards or hero sections.
- Icons from `lucide-react`.
- Minimum width 1280 px is fine; this is a desktop tool. Pages must still not break at 1024 px.
- Keyboard: `/` focuses global search, `g` then `d/p/s/a/i` jumps to Dashboards/Profiles/Segments/Activations/Imports, `Esc` closes drawers.

## Layout

- Left sidebar (collapsible to icons): Dashboards, Profiles, Segments, Activations, Imports, Sources, Admin (admin only), System (admin only).
- Top bar: global profile search (typeahead showing name, masked email, donor status), job activity indicator (spinner with count of running jobs; click opens a drawer listing them with progress bars), environment badge (`DEV` in amber when `APP_ENV != production`), user menu with role shown.
- Main area uses tabs within pages rather than deep navigation.
- Toasts bottom-right for job completions and errors.

## Pages

### Dashboards
Tabs: Overview, Giving, Retention, Engagement, Sources, Data Health. Date range picker (presets: 30 d, 90 d, 12 m, YTD, last FY, custom; fiscal year start month configurable in admin settings) applies to all tabs.

- **Overview:** KPI tile row (value, delta vs prior period with arrow and color, sparkline). Below: monthly giving bar chart with 12 m trailing line, donor status stacked area over time, top 5 campaigns table.
- **Giving:** amount and count by month, new vs returning donors, by channel (bar), by fund (horizontal bar), top appeal codes table with response counts and totals, gift size distribution histogram (buckets $1–24, 25–49, 50–99, 100–249, 250–499, 500–999, 1k–4.9k, 5k+).
- **Retention:** retention rate by year (line), cohort heatmap table (first-gift year rows × year columns, cells show %), donor status distribution now vs 12 months ago.
- **Engagement:** events per day by source (line), top event names (table), viewer-to-donor conversion count per month.
- **Sources:** profiles per source (bar), overlap matrix (heatmap table), identifier coverage per source (% email, phone, address).
- **Data Health:** pending resolution count, merges per day, rejected rows by recent import, auto-blocklisted identifiers awaiting review (with approve/unblock actions for admins), enrichment attributes expiring within 60 days.

Every chart has a small "table" toggle to show the underlying numbers and a CSV download (analyst+, audit logged).

### Profiles
- Search bar plus filters (donor status, source, has email, consent). Results table: name, email (masked for viewer), phone (masked), city/state, donor status badge, LTV, last gift date, sources (small chips).
- Profile detail (full page, with back navigation that keeps list state):
  - Header: name, profile ID (mono, copyable), donor status badge, RFM score, LTV, last gift, sources.
  - Tabs: **Overview** (traits grid with descriptions on hover, consent per channel with source and date, segments this profile is in), **Gifts** (table), **Activity** (event timeline, newest first, grouped by day, collapsible JSON properties), **Identity** (identifiers, source records with priority and raw attributes, merge history), **Enrichment** (by vendor with license expiry date; expired values not shown).

### Segments
- List: name, status, count, change since last refresh (+/−), last refreshed, owner, used by N activations. Filters by status and owner.
- Builder (see `SEGMENTS.md`): tree editor on the left (≈60% width), live count panel on the right with count history sparkline, breakdown by donor status, and sample profiles. Summary sentence above the tree. JSON tab.
- Detail: count history chart, members table (analyst), linked activations, audit trail for this segment.

### Activations
- List: segment → destination, schedule, last run status and count, next run.
- Create/edit wizard: pick segment → pick destination → field mapping (drag fields from registry to destination columns; presets auto-map for Google and Meta) → consent requirement → schedule (cron builder with presets: daily, weekly, monthly, and a readable preview "Every Monday at 06:00 Central").
- Run history with the funnel (selected → consent → suppressed → missing fields → sent), duration, download link, error text, re-run.

### Imports
- Wizard: choose source and record type → upload (drag and drop, progress bar, chunked) → mapping screen (left: file columns with 5 sample values each; right: target dropdown with auto-suggest pre-filled and a confidence dot; unmapped columns default to `attributes.<snake_case_header>` for contacts, or ignored for others) → validate (error summary grouped by error type with example rows) → run.
- Import list: file, source, type, status, rows ok/rejected, started by, duration. Detail shows live progress while running and a link to the error CSV.

### Sources
- Table with kind, record types, priority (editable inline by admin, drag to reorder), record count, last import or last event received, active toggle.
- Event source detail: write key (masked, rotate button), SDK snippet with the key and host filled in, events received in the last 24 h chart, last 50 events live tail (auto-refresh every 5 s).
- Enrichment source detail: vendor, license expiry date (editable), attribute catalog.

### Admin
Users (role override, deactivate), blocklist, deletion requests (create, approve, view execution result), audit log (filterable, CSV export), settings (fiscal year start, default timezone, deletion two-person rule).

### System
Job queue (status counts, running jobs with progress, failed jobs with error and retry), scheduled task last/next run times, event partitions and sizes, DB size, app version and migration head.

## Component rules

- Tables: TanStack Table with sticky header, column resize, column visibility menu, server-side pagination and sorting where the endpoint supports it.
- Empty states say what to do next ("No sources yet. Create a source to start importing.").
- Every destructive action uses a confirm dialog that names the thing being destroyed.
- Loading: skeleton rows in tables, not spinners over the whole page.
- Errors from the API surface in a toast with the error code and a "details" expander.
- PII masking is enforced by the API; the UI never un-masks client-side.
