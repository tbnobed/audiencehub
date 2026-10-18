const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const src = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const mod = { exports: {} };
new Function('module', 'exports', ts.transpileModule(src('src/lib/identity.ts'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText)(mod, mod.exports);
const { unmergeState, coveragePercent, errorMessage } = mod.exports;
const base = { merge_id: 1, survivor_id: 'a', retired_id: 'b', merged_at: null, undone_at: null, reversible: true };

test('unmerge allowed only for admin on reversible, not-undone merges', () => {
  assert.equal(unmergeState(base, true).allowed, true);
  assert.match(unmergeState(base, false).reason, /Admin/);
  assert.match(unmergeState({ ...base, reversible: false }, true).reason, /provenance/);
  assert.equal(unmergeState({ ...base, reversible: null }, true).allowed, false);
  assert.match(unmergeState({ ...base, undone_at: '2024-01-01' }, true).reason, /reversed/);
});
test('coverage and error helpers', () => {
  assert.equal(coveragePercent(472, 1000), '47.2%');
  assert.equal(coveragePercent(3, 0), '—');
  assert.equal(errorMessage(new Error('Reverse later dependent merges first')), 'Reverse later dependent merges first');
  assert.equal(errorMessage(null), 'Request failed. Try again.');
});
test('uses exact backend identity endpoints and invalidates after unmerge', () => {
  const h = src('src/hooks/use-identity.ts');
  for (const s of ['/api/identity/health', '/api/identity/master/', '/unmerge`, { method: \'POST\' }', "queryKey: ['profiles']", "queryKey: ['identity-health']"]) assert.ok(h.includes(s), s);
});
test('master id surfaced on detail, preview, search hint and data health', () => {
  assert.ok(src('src/pages/profile-detail.tsx').includes('<MasterIdChip masterId={profile.master_id}'));
  assert.ok(src('src/components/profiles/profile-preview.tsx').includes('masterId={data.master_id}'));
  assert.ok(src('src/pages/profiles.tsx').includes('Master ID"'));
  assert.ok(src('src/pages/data-health.tsx').includes('<IdentityHealthCard />'));
  assert.ok(src('src/components/identity/master-identity-panel.tsx').includes('data-testid="button-confirm-unmerge"'));
});

// ---- Step 2: canonical attributes + segment resource filters ----
function loadTs(rel, deps = {}) {
  const m = { exports: {} };
  const out = ts.transpileModule(src(rel), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  new Function('module', 'exports', 'require', out)(m, m.exports, id => deps[id] ?? require(id));
  return m.exports;
}
const display = { displayValue: v => (v == null || v === '' ? '—' : String(v)), displayNumber: v => String(Number(v)), displayAmount: v => `$${Number(v).toFixed(2)}`, displayDate: v => String(v).slice(0, 10) };
const canon = loadTs('src/lib/canonical-display.ts', { './profile-display': display });
const catalog = {
  donor_status: { data_type: 'text', group: 'Donor', sensitive: false },
  inmate: { data_type: 'boolean', group: 'Donor', sensitive: true },
  rfm_rfm_score: { data_type: 'number', group: 'Imported models', sensitive: false, model_date: 'rfm_date' },
  rfm_date: { data_type: 'date', group: 'Imported models', sensitive: false },
  mobile_consent: { data_type: 'boolean', group: 'SMS evidence', sensitive: false },
};
test('canonical groups ordered, model dates attached, booleans formatted', () => {
  const g = canon.groupCanonical(catalog, { donor_status: 'active', rfm_rfm_score: 412, rfm_date: '2024-03-01', mobile_consent: 'Y' }, { hideSensitive: false });
  assert.deepEqual(g.map(x => x.group), ['Donor', 'Imported models', 'SMS evidence']);
  const models = g[1];
  assert.equal(models.rows[0].modelDate, '2024-03-01');
  assert.deepEqual(models.modelDates.map(d => d.key), ['rfm_date']);
  assert.equal(g[2].rows[0].value, 'Yes');
});
test('sensitive fields omitted entirely (no placeholder row) when hidden or absent from catalog', () => {
  const g = canon.groupCanonical(catalog, {}, { hideSensitive: true });
  assert.ok(!g.flatMap(x => x.rows).some(r => r.key === 'inmate'));
  const { inmate, ...viewerCatalog } = catalog; void inmate;
  assert.ok(!canon.groupCanonical(viewerCatalog, {}, { hideSensitive: false }).flatMap(x => x.rows).some(r => r.key === 'inmate'));
});
test('segment validate accepts backend resource filters on gift and event, rejects bad ops', () => {
  const model = loadTs('src/components/segments/model.ts');
  const fields = [
    { key: 'gift.count', type: 'gift', field: 'count', aggregate: 'count', data_type: 'number', operators: ['gte'], enum_values: [], label: '', group: '' },
    { key: 'event.exists', type: 'event', field: 'exists', aggregate: 'exists', data_type: 'boolean', operators: ['is_true', 'is_false'], enum_values: [], label: '', group: '' },
    { key: 'attribute.wealth_score', type: 'attribute', field: 'wealth_score', data_type: 'number', operators: ['gte'], enum_values: [], label: '', group: 'Imported models' },
  ];
  const def = rules => ({ version: 1, root: { op: 'and', rules } });
  const ok = def([
    { type: 'gift', aggregate: 'count', operator: 'gte', value: 1, where: { 'source_codes.name': { operator: 'contains', value: 'Telethon' }, fund: { operator: 'eq', value: 'Missions' } } },
    { type: 'event', name: 'vod_viewed', aggregate: 'exists', operator: 'is_true', window: { kind: 'last_days', days: 30 }, where: { 'vod_titles.series': { operator: 'in', value: ['Praise'] } } },
    { type: 'attribute', field: 'wealth_score', operator: 'gte', value: 5 },
  ]);
  assert.deepEqual(model.validate(ok, fields), []);
  const bad = def([{ type: 'gift', aggregate: 'count', operator: 'gte', value: 1, where: { 'products.key': { operator: 'gt', value: 'x' }, 'products.sku': { operator: 'eq', value: 'x' } } }]);
  const issues = model.validate(bad, fields).map(i => i.path);
  assert.ok(issues.includes('root.rules[0].where.products.key'));
  assert.ok(issues.includes('root.rules[0].where.products.sku'));
  assert.match(model.summarize(ok, fields), /Source code name contains/);
  const rule = model.ruleFromField(fields[2]);
  assert.deepEqual(rule, { type: 'attribute', field: 'wealth_score', operator: 'gte', value: 0 });
});
