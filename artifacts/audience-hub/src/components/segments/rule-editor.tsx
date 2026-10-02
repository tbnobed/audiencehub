import { useEffect, useMemo, useState, type ReactNode, type SelectHTMLAttributes } from 'react';
import { Check, ChevronDown, Plus, X } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { useSegmentTypeahead } from '@/hooks/use-segments';
import type { Segment, SegmentField, SegmentRule } from '@/lib/segments';
import { GIFT_WHERE, OP_LABEL, VALUELESS, defaultValue, withOperator } from './model';

export const ctl = 'h-7 rounded-sm border border-line bg-surface-raised px-2 text-xs text-ink outline-none focus-visible:border-signal focus-visible:ring-1 focus-visible:ring-signal disabled:opacity-60 disabled:cursor-not-allowed';
const ctlErr = (bad?: boolean) => bad ? ' border-danger/70' : '';

export function NativeSelect({ invalid, className = '', children, ...props }: SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }) {
  return <select {...props} aria-invalid={invalid || undefined} className={`${ctl}${ctlErr(invalid)} pr-6 ${className}`}>{children}</select>;
}
function NumberBox({ value, onChange, disabled, invalid, label, min, testId, width = 'w-24' }: { value: unknown; onChange: (n: number | string) => void; disabled?: boolean; invalid?: boolean; label: string; min?: number; testId: string; width?: string }) {
  const [text, setText] = useState(value == null ? '' : String(value));
  useEffect(() => { setText(prev => (Number(prev) === value && prev !== '' ? prev : value == null ? '' : String(value))); }, [value]);
  return <input type="number" inputMode="decimal" aria-label={label} data-testid={testId} min={min} disabled={disabled} value={text}
    aria-invalid={invalid || undefined}
    onChange={e => { setText(e.target.value); const n = e.target.value === '' ? '' : Number(e.target.value); onChange(typeof n === 'number' && Number.isFinite(n) ? n : e.target.value); }}
    className={`${ctl}${ctlErr(invalid)} ${width} font-mono tabular-nums`} />;
}
function DateBox({ value, onChange, disabled, invalid, label, testId }: { value: unknown; onChange: (v: string) => void; disabled?: boolean; invalid?: boolean; label: string; testId: string }) {
  return <input type="date" aria-label={label} data-testid={testId} disabled={disabled} value={typeof value === 'string' ? value.slice(0, 10) : ''}
    aria-invalid={invalid || undefined} onChange={e => onChange(e.target.value)} className={`${ctl}${ctlErr(invalid)} w-36 font-mono [color-scheme:dark]`} />;
}

/** Searchable, grouped registry picker. */
export function FieldPicker({ fields, value, onPick, disabled, testId }: { fields: SegmentField[]; value?: SegmentField; onPick: (f: SegmentField) => void; disabled?: boolean; testId: string }) {
  const [open, setOpen] = useState(false);
  const groups = useMemo(() => {
    const m = new Map<string, SegmentField[]>();
    for (const f of fields) m.set(f.group, [...(m.get(f.group) ?? []), f]);
    return [...m.entries()];
  }, [fields]);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild disabled={disabled}>
        <button type="button" data-testid={testId} aria-label="Choose field" className={`${ctl} inline-flex items-center gap-1.5 min-w-[150px] max-w-[220px] justify-between ${value ? '' : 'text-ink-muted'}`}>
          <span className="truncate">{value ? <><span className="text-ink-muted">{value.group} · </span>{value.label}</> : 'Choose field…'}</span>
          <ChevronDown size={12} className="shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-0 border-line" align="start">
        <Command>
          <CommandInput placeholder="Search partner fields…" data-testid="input-field-search" className="text-xs" />
          <CommandList className="max-h-72">
            <CommandEmpty className="py-6 text-center text-xs text-ink-muted">No field matches. Try a group name like Giving.</CommandEmpty>
            {groups.map(([group, items]) => (
              <CommandGroup key={group} heading={group}>
                {items.map(f => (
                  <CommandItem key={f.key} value={`${f.group} ${f.label} ${f.key}`} onSelect={() => { onPick(f); setOpen(false); }} className="text-xs" data-testid={`option-field-${f.key}`}>
                    <Check size={12} className={`mr-1.5 ${value?.key === f.key ? 'opacity-100 text-signal' : 'opacity-0'}`} />
                    <span className="flex-1 truncate">{f.label}</span>
                    <span className="ml-2 font-mono text-[10px] text-ink-muted">{f.data_type}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

/** Multi-select chips with server typeahead (enums) or free entry (text). */
export function MultiValue({ value, onChange, typeaheadKey, options, allowFree, disabled, invalid, testId }: { value: unknown; onChange: (v: string[]) => void; typeaheadKey?: string; options?: string[]; allowFree?: boolean; disabled?: boolean; invalid?: boolean; testId: string }) {
  const selected = Array.isArray(value) ? value.map(String) : [];
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [dq, setDq] = useState('');
  useEffect(() => { const t = setTimeout(() => setDq(q), 200); return () => clearTimeout(t); }, [q]);
  const remote = useSegmentTypeahead(open && typeaheadKey ? typeaheadKey : '', dq);
  const items = (remote.data?.items ?? options ?? []).filter(v => !q || v.toLowerCase().includes(q.toLowerCase()));
  const toggle = (v: string) => onChange(selected.includes(v) ? selected.filter(x => x !== v) : [...selected, v]);
  return (
    <div className={`flex flex-wrap items-center gap-1 min-h-7 rounded-sm border bg-surface-raised px-1 py-0.5 max-w-[360px] ${invalid ? 'border-danger/70' : 'border-line'}`} data-testid={testId}>
      {selected.map(v => (
        <span key={v} className="inline-flex items-center gap-1 rounded-sm bg-signal-soft border border-signal/25 px-1.5 h-5 text-[11px] font-mono text-ink">
          {v}{!disabled && <button type="button" aria-label={`Remove ${v}`} onClick={() => toggle(v)} className="text-ink-muted hover:text-ink"><X size={10} /></button>}
        </span>
      ))}
      {!disabled && <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button type="button" aria-label="Add value" data-testid={`${testId}-add`} className="inline-flex items-center gap-1 h-5 px-1.5 text-[11px] text-ink-muted hover:text-signal"><Plus size={11} />{selected.length ? '' : 'Add value'}</button>
        </PopoverTrigger>
        <PopoverContent className="w-64 p-0 border-line" align="start">
          <Command shouldFilter={false}>
            <CommandInput value={q} onValueChange={setQ} placeholder={allowFree ? 'Type and press Enter…' : 'Search values…'} className="text-xs"
              onKeyDown={e => { if (e.key === 'Enter' && allowFree && q.trim() && !items.length) { e.preventDefault(); toggle(q.trim()); setQ(''); } }} />
            <CommandList className="max-h-60">
              {remote.isFetching && !items.length ? <div className="p-3 text-xs text-ink-muted">Loading values…</div>
                : remote.isError && !options?.length ? <div className="p-3 text-xs text-danger">Values unavailable{allowFree ? ' — type a value and press Enter.' : '.'}</div>
                : <CommandEmpty className="py-4 text-center text-xs text-ink-muted">{allowFree ? 'Press Enter to add this value.' : 'No matching values.'}</CommandEmpty>}
              {allowFree && q.trim() && !items.includes(q.trim()) && items.length > 0 && <CommandItem value={`__free_${q}`} onSelect={() => { toggle(q.trim()); setQ(''); }} className="text-xs">Add "{q.trim()}"</CommandItem>}
              {items.map(v => <CommandItem key={v} value={v} onSelect={() => toggle(v)} className="text-xs font-mono">
                <Check size={12} className={`mr-1.5 ${selected.includes(v) ? 'opacity-100 text-signal' : 'opacity-0'}`} />{v}
              </CommandItem>)}
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>}
    </div>
  );
}

/** Single text value; offers typeahead suggestions when a key is known. */
function TextValue({ value, onChange, typeaheadKey, disabled, invalid, testId, label }: { value: unknown; onChange: (v: string) => void; typeaheadKey?: string; disabled?: boolean; invalid?: boolean; testId: string; label: string }) {
  const [focus, setFocus] = useState(false);
  const text = typeof value === 'string' ? value : '';
  const [dq, setDq] = useState(text);
  useEffect(() => { const t = setTimeout(() => setDq(text), 200); return () => clearTimeout(t); }, [text]);
  const remote = useSegmentTypeahead(focus && typeaheadKey ? typeaheadKey : '', dq);
  const listId = `${testId}-list`;
  return <>
    <input aria-label={label} data-testid={testId} disabled={disabled} value={text} list={typeaheadKey ? listId : undefined}
      onFocus={() => setFocus(true)} onChange={e => onChange(e.target.value)} aria-invalid={invalid || undefined}
      className={`${ctl}${ctlErr(invalid)} w-40`} placeholder="value" />
    {typeaheadKey && <datalist id={listId}>{(remote.data?.items ?? []).slice(0, 50).map(v => <option key={v} value={v} />)}</datalist>}
  </>;
}

/** Operator + value controls that adapt to the data type. */
export function OperatorValue({ rule, dataType, operators, enumValues, typeaheadKey, onChange, disabled, issueAt, testId, valueField = 'value' }: {
  rule: { operator?: string; value?: unknown }; dataType: string; operators: string[]; enumValues?: string[]; typeaheadKey?: string;
  onChange: (r: { operator?: string; value?: unknown }) => void; disabled?: boolean; issueAt: (suffix: string) => boolean; testId: string; valueField?: string;
}) {
  const op = rule.operator ?? '';
  const v = rule.value;
  const setV = (value: unknown) => onChange({ ...rule, value });
  let input: ReactNode = null;
  const bad = issueAt(`.${valueField}`);
  if (VALUELESS.has(op)) input = null;
  else if (op === 'in' || op === 'not_in') input = <MultiValue value={v} onChange={setV} typeaheadKey={typeaheadKey} options={enumValues} allowFree={dataType !== 'enum'} disabled={disabled} invalid={bad} testId={`${testId}-values`} />;
  else if (op === 'between') {
    const pair = Array.isArray(v) ? v : ['', ''];
    const set = (i: number, x: unknown) => { const n = [...pair]; n[i] = x; setV(n); };
    input = <span className="inline-flex items-center gap-1">
      {dataType === 'number' ? <NumberBox value={pair[0]} onChange={x => set(0, x)} disabled={disabled} invalid={bad || issueAt(`.${valueField}[0]`)} label="From" testId={`${testId}-from`} />
        : <DateBox value={pair[0]} onChange={x => set(0, x)} disabled={disabled} invalid={bad || issueAt(`.${valueField}[0]`)} label="From date" testId={`${testId}-from`} />}
      <span className="text-ink-muted text-[11px]">and</span>
      {dataType === 'number' ? <NumberBox value={pair[1]} onChange={x => set(1, x)} disabled={disabled} invalid={bad || issueAt(`.${valueField}[1]`)} label="To" testId={`${testId}-to`} />
        : <DateBox value={pair[1]} onChange={x => set(1, x)} disabled={disabled} invalid={bad || issueAt(`.${valueField}[1]`)} label="To date" testId={`${testId}-to`} />}
    </span>;
  } else if (op === 'in_last_days' || op === 'not_in_last_days') input = <span className="inline-flex items-center gap-1"><NumberBox value={v} onChange={setV} min={1} disabled={disabled} invalid={bad} label="Days" testId={`${testId}-days`} width="w-16" /><span className="text-[11px] text-ink-muted">days</span></span>;
  else if (dataType === 'number') input = <NumberBox value={v} onChange={setV} disabled={disabled} invalid={bad} label="Value" testId={`${testId}-value`} />;
  else if (dataType === 'date' || dataType === 'timestamp') input = <DateBox value={v} onChange={setV} disabled={disabled} invalid={bad} label="Date" testId={`${testId}-value`} />;
  else input = <TextValue value={v} onChange={setV} typeaheadKey={typeaheadKey} disabled={disabled} invalid={bad} testId={`${testId}-value`} label="Value" />;
  return <>
    <NativeSelect aria-label="Operator" data-testid={`${testId}-operator`} value={op} disabled={disabled} invalid={issueAt('.operator')}
      onChange={e => onChange(withOperator({ type: '', ...rule } as SegmentRule, e.target.value, dataType))}>
      {!operators.includes(op) && <option value={op}>{op || 'operator'}</option>}
      {operators.map(o => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
    </NativeSelect>
    {input}
  </>;
}

export function WindowEditor({ window: w, onChange, disabled, issueAt, testId }: { window: unknown; onChange: (w: unknown) => void; disabled?: boolean; issueAt: (s: string) => boolean; testId: string }) {
  const win = (w ?? null) as Record<string, unknown> | null;
  const kind = win ? String(win.kind) : 'all';
  const set = (k: string) => onChange(k === 'all' ? undefined : k === 'last_days' ? { kind: k, days: 90 } : k === 'between' ? { kind: k, from: '', to: '' } : { kind: k, date: '' });
  return <span className="inline-flex items-center gap-1">
    <NativeSelect aria-label="Time window" data-testid={`${testId}-window`} value={kind} disabled={disabled} invalid={issueAt('.window') || issueAt('.window.kind')} onChange={e => set(e.target.value)}>
      <option value="all">all time</option><option value="last_days">in last N days</option><option value="between">between dates</option><option value="before">before</option><option value="after">after</option>
    </NativeSelect>
    {win?.kind === 'last_days' && <><NumberBox value={win.days} min={1} onChange={d => onChange({ ...win, days: d })} disabled={disabled} invalid={issueAt('.window.days')} label="Window days" testId={`${testId}-window-days`} width="w-16" /><span className="text-[11px] text-ink-muted">days</span></>}
    {win?.kind === 'between' && <><DateBox value={win.from} onChange={d => onChange({ ...win, from: d })} disabled={disabled} invalid={issueAt('.window')} label="Window from" testId={`${testId}-window-from`} /><span className="text-[11px] text-ink-muted">to</span><DateBox value={win.to} onChange={d => onChange({ ...win, to: d })} disabled={disabled} invalid={issueAt('.window')} label="Window to" testId={`${testId}-window-to`} /></>}
    {(win?.kind === 'before' || win?.kind === 'after') && <DateBox value={win.date} onChange={d => onChange({ ...win, date: d })} disabled={disabled} invalid={issueAt('.window.date')} label="Window date" testId={`${testId}-window-date`} />}
  </span>;
}

type Cond = { operator?: string; value?: unknown };
const GIFT_OPS: Record<string, string[]> = { text: ['eq', 'neq', 'in', 'not_in'], number: ['eq', 'gt', 'gte', 'lt', 'lte', 'between'], boolean: ['is_true', 'is_false'] };
const WHERE_LABEL: Record<string, string> = { fund: 'Fund', campaign: 'Campaign', appeal_code: 'Appeal code', channel: 'Channel', amount: 'Amount', is_recurring: 'Recurring' };

/** Gift filters on fund, campaign, appeal_code, channel, is_recurring, amount. */
export function GiftWhere({ where, onChange, giftValues, disabled, issueAt, testId }: { where: unknown; onChange: (w: Record<string, Cond> | undefined) => void; giftValues: Record<string, string[]>; disabled?: boolean; issueAt: (s: string) => boolean; testId: string }) {
  const w = (where ?? {}) as Record<string, Cond>;
  const keys = Object.keys(w);
  const unused = Object.keys(GIFT_WHERE).filter(k => !keys.includes(k));
  const put = (k: string, c: Cond | null) => { const n = { ...w }; if (c) n[k] = c; else delete n[k]; onChange(Object.keys(n).length ? n : undefined); };
  return <div className="flex flex-col gap-1">
    {keys.map(k => {
      const t = GIFT_WHERE[k] ?? 'text';
      return <div key={k} className="flex flex-wrap items-center gap-1 pl-3 border-l border-line" data-testid={`${testId}-where-${k}`}>
        <span className="text-[11px] text-ink-muted w-20">{WHERE_LABEL[k] ?? k}</span>
        {t === 'boolean'
          ? <NativeSelect aria-label={`${k} value`} value={w[k].operator} disabled={disabled} onChange={e => put(k, { operator: e.target.value })} invalid={issueAt(`.where.${k}`)}><option value="is_true">yes</option><option value="is_false">no</option></NativeSelect>
          : <OperatorValue rule={w[k]} dataType={t} operators={GIFT_OPS[t]} enumValues={giftValues[k]} typeaheadKey={t === 'text' ? `gift.${k}` : undefined} onChange={c => put(k, c)} disabled={disabled} issueAt={s => issueAt(`.where.${k}${s}`) || issueAt(`.where.${k}`)} testId={`${testId}-where-${k}`} />}
        {!disabled && <button type="button" aria-label={`Remove ${k} filter`} onClick={() => put(k, null)} className="text-ink-muted hover:text-danger p-0.5"><X size={12} /></button>}
      </div>;
    })}
    {!disabled && unused.length > 0 && <NativeSelect aria-label="Add gift filter" data-testid={`${testId}-add-where`} value="" className="w-fit text-ink-muted"
      onChange={e => { const k = e.target.value; if (!k) return; const t = GIFT_WHERE[k]; put(k, t === 'boolean' ? { operator: 'is_true' } : { operator: 'eq', value: defaultValue('eq', t) }); }}>
      <option value="">+ gift filter</option>{unused.map(k => <option key={k} value={k}>{WHERE_LABEL[k]}</option>)}
    </NativeSelect>}
  </div>;
}

/** Event property filters: properties.<key> text equality only. */
export function EventWhere({ where, onChange, disabled, issueAt, testId }: { where: unknown; onChange: (w: Record<string, Cond> | undefined) => void; disabled?: boolean; issueAt: (s: string) => boolean; testId: string }) {
  const entries = Object.entries((where ?? {}) as Record<string, Cond>);
  const write = (list: [string, Cond][]) => onChange(list.length ? Object.fromEntries(list) : undefined);
  return <div className="flex flex-col gap-1">
    {entries.map(([k, c], i) => <div key={i} className="flex items-center gap-1 pl-3 border-l border-line">
      <span className="text-[11px] text-ink-muted font-mono">properties.</span>
      <input aria-label="Property key" data-testid={`${testId}-prop-key-${i}`} disabled={disabled} value={k.slice(11)} aria-invalid={issueAt(`.where.${k}`) || undefined}
        onChange={e => { const n = [...entries]; n[i] = [`properties.${e.target.value.replace(/[^\w.-]/g, '')}`, c]; write(n); }} className={`${ctl}${ctlErr(issueAt(`.where.${k}`))} w-28 font-mono`} placeholder="key" />
      <span className="text-[11px] text-ink-muted">=</span>
      <input aria-label="Property value" data-testid={`${testId}-prop-value-${i}`} disabled={disabled} value={String(c.value ?? '')}
        onChange={e => { const n = [...entries]; n[i] = [k, { operator: 'eq', value: e.target.value }]; write(n); }} className={`${ctl} w-32`} placeholder="text value" />
      {!disabled && <button type="button" aria-label="Remove property filter" onClick={() => write(entries.filter((_, j) => j !== i))} className="text-ink-muted hover:text-danger p-0.5"><X size={12} /></button>}
    </div>)}
    {!disabled && <button type="button" data-testid={`${testId}-add-prop`} onClick={() => write([...entries, [`properties.key${entries.length + 1}`, { operator: 'eq', value: '' }]])} className="w-fit text-[11px] text-ink-muted hover:text-signal inline-flex items-center gap-1"><Plus size={11} /> property filter</button>}
  </div>;
}

/** Type-specific controls for a single rule. Fields stay exactly as the DSL defines them. */
export function RuleControls({ rule, field, fields, giftValues, segments, currentId, onChange, disabled, issueAt, testId }: {
  rule: SegmentRule; field?: SegmentField; fields: SegmentField[]; giftValues: Record<string, string[]>; segments: Segment[]; currentId?: number;
  onChange: (r: SegmentRule) => void; disabled?: boolean; issueAt: (s: string) => boolean; testId: string;
}) {
  const set = (patch: Partial<SegmentRule>) => onChange({ ...rule, ...patch });
  if (rule.type === 'consent') {
    const channels = [...new Set(fields.filter(f => f.type === 'consent').map(f => f.channel ?? f.field ?? ''))].filter(Boolean);
    return <>
      <NativeSelect aria-label="Channel" data-testid={`${testId}-channel`} value={String(rule.channel ?? '')} disabled={disabled} invalid={issueAt('.channel')} onChange={e => set({ channel: e.target.value })}>
        {!channels.includes(String(rule.channel)) && <option value={String(rule.channel ?? '')}>{String(rule.channel || 'channel')}</option>}
        {channels.map(c => <option key={c} value={c}>{c}</option>)}
      </NativeSelect>
      <span className="text-[11px] text-ink-muted">permission is</span>
      <NativeSelect aria-label="Permission status" data-testid={`${testId}-status`} value={String(rule.status ?? '')} disabled={disabled} invalid={issueAt('.status')} onChange={e => set({ status: e.target.value })}>
        {(field?.enum_values.length ? field.enum_values : ['opted_in', 'opted_out', 'unknown']).map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
      </NativeSelect>
    </>;
  }
  if (rule.type === 'source') {
    const options = field?.enum_values ?? [];
    return <>
      <NativeSelect aria-label="Membership" data-testid={`${testId}-operator`} value={String(rule.operator)} disabled={disabled} invalid={issueAt('.operator')} onChange={e => set({ operator: e.target.value })}><option value="in">includes</option><option value="not_in">excludes</option></NativeSelect>
      <NativeSelect aria-label="Source" data-testid={`${testId}-source`} value={String(rule.source_key ?? '')} disabled={disabled} invalid={issueAt('.source_key')} onChange={e => set({ source_key: e.target.value })}>
        <option value="">choose source…</option>
        {rule.source_key && !options.includes(String(rule.source_key)) ? <option value={String(rule.source_key)}>{String(rule.source_key)}</option> : null}
        {options.map(s => <option key={s} value={s}>{s}</option>)}
      </NativeSelect>
    </>;
  }
  if (rule.type === 'segment') {
    const choices = segments.filter(s => s.id !== currentId && s.status !== 'archived');
    const id = Number(rule.segment_id) || 0;
    return <>
      <NativeSelect aria-label="Membership" data-testid={`${testId}-operator`} value={String(rule.operator)} disabled={disabled} invalid={issueAt('.operator')} onChange={e => set({ operator: e.target.value })}><option value="in">is a member of</option><option value="not_in">is not a member of</option></NativeSelect>
      <NativeSelect aria-label="Saved audience" data-testid={`${testId}-segment`} value={String(id)} disabled={disabled} invalid={issueAt('.segment_id')} onChange={e => set({ segment_id: Number(e.target.value) })} className="max-w-[220px]">
        <option value="0">choose audience…</option>
        {id > 0 && !choices.some(s => s.id === id) && <option value={id}>#{id} (unavailable)</option>}
        {choices.map(s => <option key={s.id} value={s.id}>{s.name}{s.status === 'draft' ? ' (draft)' : ''}</option>)}
      </NativeSelect>
    </>;
  }
  if (!field) return <span className="text-[11px] text-danger">Field not in registry — pick another.</span>;
  if (rule.type === 'gift' || rule.type === 'event') {
    return <div className="flex flex-col gap-1.5 w-full">
      <div className="flex flex-wrap items-center gap-1">
        {rule.type === 'event' && <input aria-label="Event name" data-testid={`${testId}-event-name`} disabled={disabled} value={String(rule.name ?? '')} placeholder="Event name, e.g. Video Watched"
          aria-invalid={issueAt('.name') || undefined} onChange={e => set({ name: e.target.value })} className={`${ctl}${ctlErr(issueAt('.name'))} w-48`} />}
        <OperatorValue rule={rule} dataType={field.data_type} operators={field.operators} onChange={c => onChange({ ...rule, ...c } as SegmentRule)} disabled={disabled} issueAt={issueAt} testId={testId} />
        <WindowEditor window={rule.window} onChange={w => { const n = { ...rule }; if (w) n.window = w; else delete n.window; onChange(n); }} disabled={disabled} issueAt={issueAt} testId={testId} />
      </div>
      {rule.type === 'gift'
        ? <GiftWhere where={rule.where} giftValues={giftValues} onChange={w => { const n = { ...rule }; if (w) n.where = w; else delete n.where; onChange(n); }} disabled={disabled} issueAt={issueAt} testId={testId} />
        : <EventWhere where={rule.where} onChange={w => { const n = { ...rule }; if (w) n.where = w; else delete n.where; onChange(n); }} disabled={disabled} issueAt={issueAt} testId={testId} />}
    </div>;
  }
  return <OperatorValue rule={rule} dataType={field.data_type} operators={field.operators} enumValues={field.enum_values}
    typeaheadKey={field.data_type === 'enum' ? field.key : undefined}
    onChange={c => onChange({ ...rule, ...c } as SegmentRule)} disabled={disabled} issueAt={issueAt} testId={testId} />;
}
