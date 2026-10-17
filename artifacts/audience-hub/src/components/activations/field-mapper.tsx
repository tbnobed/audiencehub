import { useMemo, useState } from 'react';
import { GripVertical, Plus, Trash2, ArrowUp, ArrowDown, Search, Hash, Lock, Quote } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { Destination, ExportField, MappingValue } from '@/lib/activations';
import { PRESETS, allowedFields, enforceLocked, lockedSource, fieldId, fieldToValue, presetRows, rowId, valueLabel, type MapRow } from './logic';

const GROUPS: { kind: ExportField['kind']; label: string }[] = [{ kind: 'profile', label: 'Profile' }, { kind: 'trait', label: 'Traits' }, { kind: 'enrichment', label: 'Enrichment' }];
const DND = 'application/x-kin-field';

export function FieldMapper({ rows, onChange: rawChange, fields, destination }: { rows: MapRow[]; onChange: (r: MapRow[]) => void; fields: ExportField[]; destination: Destination | null }) {
  const onChange = (r: MapRow[]) => rawChange(enforceLocked(destination?.type, r));
  const [q, setQ] = useState('');
  const [over, setOver] = useState<string | null>(null);
  const fixed = destination ? PRESETS[destination.type] : undefined;
  const allowed = useMemo(() => allowedFields(fields, destination), [fields, destination]);
  const blockedPii = fields.length - allowed.length;
  const byId = useMemo(() => new Map(allowed.map(f => [fieldId(f), f])), [allowed]);
  const visible = allowed.filter(f => !q || `${f.label} ${f.key} ${f.source ?? ''}`.toLowerCase().includes(q.toLowerCase()));

  const patch = (id: string, p: Partial<MapRow>) => onChange(rows.map(r => (r.id === id ? { ...r, ...p } : r)));
  const move = (i: number, d: -1 | 1) => { const n = [...rows]; const j = i + d; if (j < 0 || j >= n.length) return; [n[i], n[j]] = [n[j], n[i]]; onChange(n); };
  const add = (value: MappingValue | null, column = '') => onChange([...rows, { id: rowId(), column, value }]);
  const drop = (rowIdx: string | null, raw: string) => {
    const f = byId.get(raw); if (!f) return;
    if (rowIdx && lockedSource(destination?.type, rows.find(r => r.id === rowIdx)?.column ?? '')) return;
    if (rowIdx) patch(rowIdx, { value: fieldToValue(f), column: rows.find(r => r.id === rowIdx)?.column || f.key });
    else if (!fixed) add(fieldToValue(f), f.key);
  };

  return <div className="grid grid-cols-[minmax(220px,300px)_1fr] gap-3 min-h-[360px]">
    <aside className="border border-line rounded-sm bg-surface flex flex-col min-h-0" aria-label="Export field registry">
      <div className="p-2 border-b border-line relative"><Search size={12} className="absolute left-4 top-1/2 -translate-y-1/2 text-ink-muted" /><Input className="h-7 pl-7 text-[12px]" placeholder="Search fields" value={q} onChange={e => setQ(e.target.value)} data-testid="input-search-fields" /></div>
      <div className="overflow-y-auto flex-1 max-h-[420px] p-1">
        {GROUPS.map(g => { const list = visible.filter(f => f.kind === g.kind); if (!list.length) return null; return <div key={g.kind} className="mb-2">
          <div className="text-[10px] uppercase tracking-wider text-ink-muted px-1.5 py-1">{g.label} <span className="font-mono">{list.length}</span></div>
          {list.map(f => <div key={fieldId(f)} draggable onDragStart={e => { e.dataTransfer.setData(DND, fieldId(f)); e.dataTransfer.effectAllowed = 'copy'; }}
            className="group flex items-center gap-1.5 h-7 px-1.5 rounded-sm hover:bg-surface-raised cursor-grab text-[12px]" data-testid={`field-${f.kind}-${f.key}`}>
            <GripVertical size={11} className="text-ink-muted shrink-0" /><span className="truncate flex-1">{f.label}</span>
            {f.source && <span className="font-mono text-[10px] text-ink-muted">{f.source}</span>}{f.pii && <Lock size={10} className="text-warn" aria-label="PII" />}
            {!fixed && <button type="button" className="opacity-0 group-hover:opacity-100 focus:opacity-100 text-signal" aria-label={`Add ${f.label} as column`} onClick={() => add(fieldToValue(f), f.key)} data-testid={`button-add-field-${f.key}`}><Plus size={12} /></button>}
          </div>)}
        </div>; })}
        {!visible.length && <p className="text-[12px] text-ink-muted p-3">No exportable fields match "{q}".</p>}
      </div>
      {blockedPii > 0 && <p className="text-[11px] text-warn border-t border-line p-2">{blockedPii} PII field{blockedPii === 1 ? '' : 's'} hidden: this CSV destination excludes PII.</p>}
    </aside>

    <div className="min-w-0 space-y-2">
      {fixed && <p className="text-[12px] text-ink-muted border border-line bg-surface-raised rounded-sm p-2">Fixed {destination?.type === 'google_customer_match' ? 'Google Customer Match' : 'Meta'} columns. The worker normalizes values per platform rules and SHA-256 hashes the columns marked <Hash size={10} className="inline" />; the file is for manual upload. Unmapped columns are left blank.</p>}
      <div className="border border-line rounded-sm bg-surface">
        <div className="grid grid-cols-[28px_minmax(120px,1fr)_minmax(160px,1.4fr)_88px] h-8 items-center text-[10px] uppercase tracking-wider text-ink-muted bg-surface-raised px-2 gap-2"><span>#</span><span>Column</span><span>Source</span><span /></div>
        {rows.map((r, i) => { const hashed = fixed?.find(c => c.column === r.column)?.hashed; const locked = lockedSource(destination?.type, r.column); return <div key={r.id}
          onDragOver={e => { if (!locked && e.dataTransfer.types.includes(DND)) { e.preventDefault(); setOver(r.id); } }} onDragLeave={() => setOver(o => (o === r.id ? null : o))}
          onDrop={e => { e.preventDefault(); setOver(null); drop(r.id, e.dataTransfer.getData(DND)); }}
          className={`grid grid-cols-[28px_minmax(120px,1fr)_minmax(160px,1.4fr)_88px] items-center gap-2 px-2 min-h-9 border-t border-line ${over === r.id ? 'bg-signal-soft' : ''}`} data-testid={`row-mapping-${i}`}>
          <span className="font-mono text-[11px] text-ink-muted">{i + 1}</span>
          {fixed ? <span className="font-mono text-[12px] flex items-center gap-1">{r.column}{hashed && <Hash size={10} className="text-signal" aria-label="hashed" />}</span>
            : <Input className="h-7 font-mono text-[12px]" aria-label={`Column ${i + 1} name`} value={r.column} onChange={e => patch(r.id, { column: e.target.value })} data-testid={`input-column-${i}`} />}
          {locked ? <div className="text-[12px] min-w-0" data-testid={`locked-source-${i}`}><span className="inline-flex items-center gap-1 font-mono"><Lock size={10} className="text-ink-muted" aria-label="Locked" />profile.id</span><p className="text-[10px] text-ink-muted leading-tight">Internal profile ID, SHA-256 hashed. A partner sees a stable opaque ID, never the raw value. Fixed by policy.</p></div>
          : <SourcePicker value={r.value} fields={allowed} allFields={fields} onChange={v => patch(r.id, { value: v })} allowConstant={!fixed} testId={`select-source-${i}`} />}
          <div className="flex justify-end gap-0.5">
            {!fixed && <><Button size="sm" variant="ghost" className="h-6 w-6 p-0" disabled={i === 0} aria-label="Move up" onClick={() => move(i, -1)} data-testid={`button-up-${i}`}><ArrowUp size={11} /></Button>
              <Button size="sm" variant="ghost" className="h-6 w-6 p-0" disabled={i === rows.length - 1} aria-label="Move down" onClick={() => move(i, 1)} data-testid={`button-down-${i}`}><ArrowDown size={11} /></Button></>}
            {!locked && <Button size="sm" variant="ghost" className="h-6 w-6 p-0 text-danger" aria-label={fixed ? `Clear ${r.column}` : `Remove ${r.column || 'column'}`} onClick={() => fixed ? patch(r.id, { value: null }) : onChange(rows.filter(x => x.id !== r.id))} data-testid={`button-remove-${i}`}><Trash2 size={11} /></Button>}
          </div>
        </div>; })}
        {!fixed && <div onDragOver={e => { if (e.dataTransfer.types.includes(DND)) { e.preventDefault(); setOver('new'); } }} onDragLeave={() => setOver(null)} onDrop={e => { e.preventDefault(); setOver(null); drop(null, e.dataTransfer.getData(DND)); }}
          className={`border-t border-dashed border-line-strong m-2 rounded-sm h-10 flex items-center justify-center gap-2 text-[12px] ${over === 'new' ? 'bg-signal-soft text-signal' : 'text-ink-muted'}`}>
          Drop a field here, or
          <Button size="sm" variant="outline" className="h-6 text-[11px]" onClick={() => add(null)} data-testid="button-add-column"><Plus size={11} /> Column</Button>
          <Button size="sm" variant="outline" className="h-6 text-[11px]" onClick={() => add({ kind: 'constant', value: '' }, 'constant')} data-testid="button-add-constant"><Quote size={11} /> Constant</Button>
        </div>}
      </div>
      {fixed && destination && <Button size="sm" variant="outline" className="h-6 text-[11px]" onClick={() => onChange(presetRows(destination.type, allowed))} data-testid="button-reapply-preset">Re-apply auto-map</Button>}
    </div>
  </div>;
}

/** Keyboard/select fallback for drag and drop. */
function SourcePicker({ value, fields, allFields, onChange, allowConstant, testId }: { value: MappingValue | null; fields: ExportField[]; allFields: ExportField[]; onChange: (v: MappingValue | null) => void; allowConstant: boolean; testId: string }) {
  const cur = value ? (value.kind === 'constant' ? '__const' : fieldId(value)) : '';
  const known = !value || value.kind === 'constant' || fields.some(f => fieldId(f) === cur);
  return <div className="flex gap-1 min-w-0">
    <select className={`h-7 min-w-0 flex-1 bg-ground border rounded-sm px-1.5 text-[12px] ${known ? 'border-line-strong' : 'border-danger'}`} value={cur} data-testid={testId}
      onChange={e => { const v = e.target.value; if (!v) return onChange(null); if (v === '__const') return onChange({ kind: 'constant', value: '' }); const f = fields.find(x => fieldId(x) === v); if (f) onChange(fieldToValue(f)); }}>
      <option value="">Unmapped</option>
      {!known && <option value={cur}>{valueLabel(value, allFields)} (not allowed)</option>}
      {GROUPS.map(g => <optgroup key={g.kind} label={g.label}>{fields.filter(f => f.kind === g.kind).map(f => <option key={fieldId(f)} value={fieldId(f)}>{f.label}{f.source ? ` (${f.source})` : ''}</option>)}</optgroup>)}
      {allowConstant && <option value="__const">Constant value…</option>}
    </select>
    {value?.kind === 'constant' && <Input className="h-7 w-28 font-mono text-[12px]" placeholder="value" value={value.value ?? ''} onChange={e => onChange({ kind: 'constant', value: e.target.value })} data-testid={`${testId}-constant`} />}
  </div>;
}
