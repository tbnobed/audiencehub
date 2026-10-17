import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, Check, ChevronRight, ShieldAlert, ShieldCheck, Search } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { Textarea } from '@/components/ui/textarea';
import { toast } from '@/hooks/use-toast';
import { useActivation, useActivationActions, useActivationFields, useDestinations } from '@/hooks/use-activations';
import { useSegment, useSegments } from '@/hooks/use-segments';
import type { Destination } from '@/lib/activations';
import {
  ADS_CONSENT, CONSENT_CHANNELS, DAYS, PRESETS, activationFormErrors, activationToForm, buildActivationWrite, buildCron, can, describeSchedule,
  effectiveConsent, fmtInt, isAdsType, plural, mappingProblems, presetRows, timezoneList, type ActivationForm, type Role, type SchedulePreset,
} from './logic';
import { FieldMapper } from './field-mapper';
import { TypeBadge, apiToast } from './shared';
import type { ReactNode } from 'react';
const fmtCount = fmtInt;

const STEPS = ['Segment', 'Destination', 'Field mapping', 'Consent', 'Schedule', 'Review'] as const;

export function ActivationWizard({ id, role }: { id?: number; role: Role }) {
  const nav = useNavigate();
  const existing = useActivation(id);
  const [form, setForm] = useState<ActivationForm>(() => activationToForm(null));
  const [step, setStep] = useState(0);
  const [showErrors, setShowErrors] = useState(false);
  const initFor = useRef<number | null>(null);
  // Poll-safe init: hydrate from the server exactly once per activation id; 5s refetches never overwrite edits.
  useEffect(() => {
    if (id && existing.data && initFor.current !== id) { initFor.current = id; setForm(activationToForm(existing.data)); }
  }, [id, existing.data]);

  const dests = useDestinations();
  const fieldsQ = useActivationFields();
  const fields = useMemo(() => fieldsQ.data?.fields ?? [], [fieldsQ.data]);
  const destination = dests.data?.items.find(d => d.id === form.destination_id) ?? null;
  const destType = destination?.type ?? existing.data?.destination_type;
  const { save } = useActivationActions();
  const set = <K extends keyof ActivationForm>(k: K, v: ActivationForm[K]) => setForm(p => ({ ...p, [k]: v }));

  const errors = activationFormErrors(form, destType, role);
  const mapProblems = fieldsQ.data ? mappingProblems(form.rows, fields, destination) : [];
  const stepErr = [errors.segment || (!form.name.trim() ? errors.name : ''), errors.destination, mapProblems[0], errors.consent, errors.schedule || errors.timezone, ''];
  const allErrors = [...Object.values(errors), ...mapProblems];

  if (id && existing.isLoading) return <div className="p-4 space-y-2">{[0, 1, 2].map(i => <div key={i} className="h-8 bg-surface-raised animate-pulse rounded-sm" />)}</div>;
  if (id && existing.isError) return <div className="p-6 text-center"><p className="text-danger mb-2">Couldn't load activation #{id}: {(existing.error as Error).message}</p><Button size="sm" variant="outline" onClick={() => existing.refetch()} data-testid="button-retry-activation">Retry</Button></div>;

  const pickDestination = (d: Destination) => setForm(p => ({
    ...p, destination_id: d.id,
    rows: PRESETS[d.type]
      ? presetRows(d.type, fields, d.type === destType ? Object.fromEntries(p.rows.filter(r => r.value).map(r => [r.column, r.value!])) : undefined)
      : destType && PRESETS[destType] ? [] : p.rows,
    required_consent: isAdsType(d.type) ? ADS_CONSENT : (isAdsType(destType) ? null : p.required_consent),
    consent_override: isAdsType(d.type) ? p.consent_override : false,
  }));

  const submit = () => {
    setShowErrors(true);
    if (allErrors.length) { toast({ title: 'Fix the highlighted steps', description: allErrors[0], variant: 'destructive' }); return; }
    save.mutate(buildActivationWrite(form, destType, role), {
      onSuccess: a => { toast({ title: `${id ? 'Saved' : 'Created'} ${a.name}`, description: 'Nothing has been sent. Use Run now or wait for the schedule.' }); nav(`/activations/${a.id}`); },
      onError: e => apiToast('Save failed', e),
    });
  };

  return <div className="flex flex-col gap-3 min-h-0">
    <div className="flex items-center gap-2">
      <Link to={id ? `/activations/${id}` : '/activations'} className="text-ink-muted hover:text-ink inline-flex items-center gap-1 text-[12px]" data-testid="link-back"><ArrowLeft size={13} /> {id ? 'Activation' : 'Activations'}</Link>
      <ChevronRight size={12} className="text-ink-muted" />
      <h1 className="kin-title text-[18px]">{id ? `Edit ${existing.data?.name ?? ''}` : 'New activation'}</h1>
    </div>
    <div className="grid grid-cols-[200px_1fr] gap-3 min-h-0">
      <nav className="border border-line rounded-sm bg-surface p-1 h-fit" aria-label="Wizard steps">
        <div className="px-2 py-1.5"><Label htmlFor="a-name" className="text-[10px] uppercase tracking-wider text-ink-muted">Name</Label>
          <Input id="a-name" className="h-7 mt-1 text-[12px]" value={form.name} placeholder="Lapsed LTV 500+ to partner" onChange={e => set('name', e.target.value)} data-testid="input-activation-name" />
          {showErrors && errors.name && <p className="text-danger text-[11px]">{errors.name}</p>}</div>
        {STEPS.map((s, i) => <button key={s} type="button" onClick={() => setStep(i)} aria-current={step === i ? 'step' : undefined}
          className={`w-full flex items-center gap-2 h-8 px-2 rounded-sm text-left text-[12px] ${step === i ? 'bg-signal-soft text-signal' : 'hover:bg-surface-raised text-ink'}`} data-testid={`button-step-${i}`}>
          <span className={`h-4 w-4 rounded-full grid place-items-center font-mono text-[10px] border ${showErrors && stepErr[i] ? 'border-danger text-danger' : step > i ? 'border-ok text-ok' : 'border-line-strong text-ink-muted'}`}>{step > i && !(showErrors && stepErr[i]) ? <Check size={9} /> : i + 1}</span>{s}
        </button>)}
      </nav>
      <div className="border border-line rounded-sm bg-surface p-3 min-w-0">
        {step === 0 && <SegmentStep value={form.segment_id} onPick={v => set('segment_id', v)} />}
        {step === 1 && <DestinationStep q={dests} value={form.destination_id} onPick={pickDestination} />}
        {step === 2 && (!destination ? <p className="text-ink-muted text-[12px]">Pick a destination first; its type decides which columns exist.</p>
          : fieldsQ.isLoading ? <div className="h-40 bg-surface-raised animate-pulse rounded-sm" />
          : fieldsQ.isError ? <p className="text-danger text-[12px]">Couldn't load the export field registry. <button className="underline" onClick={() => fieldsQ.refetch()} data-testid="button-retry-fields">Retry</button></p>
          : <><FieldMapper rows={form.rows} onChange={r => set('rows', r)} fields={fields} destination={destination} />{mapProblems.length > 0 && <ul className="mt-2 text-[11px] text-warn list-disc pl-4">{mapProblems.map(p => <li key={p}>{p}</li>)}</ul>}</>)}
        {step === 3 && <ConsentStep form={form} set={set} destType={destType} role={role} showErrors={showErrors} error={errors.consent} />}
        {step === 4 && <ScheduleStep form={form} set={set} error={errors.schedule || errors.timezone} />}
        {step === 5 && <Review form={form} destination={destination} destType={destType} role={role} problems={allErrors} />}
        <div className="flex justify-between mt-4 pt-3 border-t border-line">
          <Button size="sm" variant="outline" disabled={step === 0} onClick={() => setStep(s => s - 1)} data-testid="button-prev-step">Back</Button>
          <div className="flex gap-2">
            {step < STEPS.length - 1 && <Button size="sm" variant="outline" onClick={() => setStep(s => s + 1)} data-testid="button-next-step">Next</Button>}
            <Button size="sm" onClick={submit} disabled={save.isPending} data-testid="button-save-activation">{save.isPending ? 'Saving…' : id ? 'Save activation' : 'Create activation'}</Button>
          </div>
        </div>
      </div>
    </div>
  </div>;
}

function SegmentStep({ value, onPick }: { value: number | null; onPick: (id: number) => void }) {
  const q = useSegments('active');
  const sel = useSegment(value ?? undefined);
  const [s, setS] = useState('');
  const items = (q.data?.items ?? []).filter(x => x.status === 'active' && x.name.toLowerCase().includes(s.toLowerCase()));
  return <div className="space-y-2">
    <p className="text-[12px] text-ink-muted">Only active segments can be activated. Each run re-materializes the segment first.</p>
    {sel.data && sel.data.status !== 'active' && <p className="text-[12px] text-warn">Current segment "{sel.data.name}" is {sel.data.status}. Pick an active segment before saving.</p>}
    <div className="relative w-72"><Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-muted" /><Input className="h-7 pl-7 text-[12px]" placeholder="Filter segments" value={s} onChange={e => setS(e.target.value)} data-testid="input-filter-segments" /></div>
    <div className="border border-line rounded-sm max-h-[380px] overflow-y-auto" role="radiogroup" aria-label="Segment">
      {q.isLoading ? [0, 1, 2, 3].map(i => <div key={i} className="h-8 border-t border-line first:border-0 px-3 flex items-center"><div className="h-2.5 w-1/2 bg-surface-raised animate-pulse rounded-sm" /></div>)
        : q.isError ? <p className="p-4 text-danger text-[12px]">Couldn't load segments. <button className="underline" onClick={() => q.refetch()} data-testid="button-retry-segments">Retry</button></p>
        : !items.length ? <p className="p-6 text-center text-ink-muted text-[12px]">No active segments{s && ` matching "${s}"`}. Activate a segment in <Link to="/segments" className="text-signal">Segments</Link> first.</p>
        : items.map(x => <button key={x.id} type="button" role="radio" aria-checked={value === x.id} onClick={() => onPick(x.id)}
          className={`w-full grid grid-cols-[1fr_100px_120px] items-center h-8 px-3 border-t border-line first:border-0 text-left text-[12px] ${value === x.id ? 'bg-signal-soft' : 'hover:bg-surface-raised'}`} data-testid={`option-segment-${x.id}`}>
          <span className="truncate">{value === x.id && <Check size={11} className="inline text-signal mr-1" />}{x.name}</span>
          <span className="font-mono tabular-nums text-right">{fmtCount(x.last_count)}</span><span className="text-ink-muted text-right truncate">{x.owner ?? '—'}</span></button>)}
    </div>
  </div>;
}

function DestinationStep({ q, value, onPick }: { q: ReturnType<typeof useDestinations>; value: number | null; onPick: (d: Destination) => void }) {
  const items = (q.data?.items ?? []).filter(d => d.is_active || d.id === value);
  if (q.isLoading) return <div className="h-32 bg-surface-raised animate-pulse rounded-sm" />;
  if (q.isError) return <p className="text-danger text-[12px]">Couldn't load destinations. <button className="underline" onClick={() => q.refetch()} data-testid="button-retry-dests">Retry</button></p>;
  if (!items.length) return <p className="text-ink-muted text-[12px] p-6 text-center">No active destinations. An admin can create one on the Destinations tab.</p>;
  return <div className="grid grid-cols-2 gap-2" role="radiogroup" aria-label="Destination">
    {items.map(d => <button key={d.id} type="button" role="radio" aria-checked={value === d.id} onClick={() => onPick(d)}
      className={`text-left border rounded-sm p-2.5 ${value === d.id ? 'border-signal bg-signal-soft' : 'border-line hover:border-line-strong'}`} data-testid={`option-destination-${d.id}`}>
      <div className="flex items-center justify-between"><span className="text-[13px] font-medium">{d.name}</span><TypeBadge type={d.type} /></div>
      <p className="text-[11px] text-ink-muted mt-1 font-mono truncate">{d.type === 'webhook' ? `${d.config.url} · ${d.config.mode ?? 'full'}` : d.type === 'csv' ? `PII ${d.config.include_pii ? 'included' : 'excluded'}` : 'Hashed file for manual upload — no platform connection'}</p>
    </button>)}
  </div>;
}

function ConsentStep({ form, set, destType, role, showErrors, error }: { form: ActivationForm; set: <K extends keyof ActivationForm>(k: K, v: ActivationForm[K]) => void; destType?: string; role: Role; showErrors: boolean; error?: string }) {
  const ads = isAdsType(destType as never);
  const eff = effectiveConsent(form, destType as never, role);
  if (!ads) return <div className="space-y-2 max-w-lg">
    <Label className="text-[11px] text-ink-muted">Required consent</Label>
    <select className="h-8 w-64 bg-ground border border-line-strong rounded-sm px-2 text-[12px]" value={form.required_consent ?? ''} onChange={e => set('required_consent', e.target.value || null)} data-testid="select-required-consent">
      <option value="">None — send all unsuppressed members</option>{CONSENT_CHANNELS.map(c => <option key={c} value={c}>{c} opted in</option>)}</select>
    <p className="text-[12px] text-ink-muted">Profiles without <span className="font-mono">opted_in</span> for the chosen channel drop out at the consent stage. Suppressions always apply.</p>
  </div>;
  return <div className="space-y-3 max-w-xl">
    <div className="flex items-start gap-2 border border-ok/40 bg-ok/5 rounded-sm p-2.5"><ShieldCheck size={16} className="text-ok mt-0.5" />
      <div className="text-[12px]"><p className="text-ink">Ad audiences require <span className="font-mono">ads_personalization</span> opt-in.</p><p className="text-ink-muted">Effective requirement: <span className="font-mono text-ink" data-testid="text-effective-consent">{eff ?? 'none'}</span>{!can(role).overrideAdsConsent && ' — analysts cannot change this.'}</p></div></div>
    {can(role).overrideAdsConsent && <div className="border border-warn/40 bg-warn/5 rounded-sm p-2.5 space-y-2">
      <label className="flex items-center gap-2 text-[12px]"><Checkbox checked={form.consent_override} onCheckedChange={v => set('consent_override', !!v)} data-testid="checkbox-consent-override" /><ShieldAlert size={13} className="text-warn" /> Admin override: do not require ads_personalization</label>
      {form.consent_override && <>
        <p className="text-[11px] text-ink-muted">Members without ad consent may be uploaded to an ad platform. The override, your reason and identity are written to the audit log.</p>
        <select className="h-8 w-64 bg-ground border border-line-strong rounded-sm px-2 text-[12px]" value={form.required_consent ?? ''} onChange={e => set('required_consent', e.target.value || null)} data-testid="select-override-consent">
          <option value="">No consent requirement</option>{CONSENT_CHANNELS.filter(c => c !== ADS_CONSENT).map(c => <option key={c} value={c}>{c} opted in</option>)}</select>
        <Textarea className="text-[12px] min-h-16" placeholder="Reason (required, audited)" value={form.override_note} onChange={e => set('override_note', e.target.value)} data-testid="input-override-note" />
      </>}
    </div>}
    {(showErrors || form.consent_override) && error && <p className="text-danger text-[11px]" data-testid="error-consent">{error}</p>}
  </div>;
}

const PRESET_OPTS: { v: SchedulePreset; l: string }[] = [{ v: 'manual', l: 'Manual' }, { v: 'daily', l: 'Daily' }, { v: 'weekly', l: 'Weekly' }, { v: 'monthly', l: 'Monthly' }, { v: 'custom', l: 'Custom cron' }];
function ScheduleStep({ form, set, error }: { form: ActivationForm; set: <K extends keyof ActivationForm>(k: K, v: ActivationForm[K]) => void; error?: string }) {
  const s = form.schedule; const tzs = useMemo(timezoneList, []);
  const up = (p: Partial<typeof s>) => set('schedule', { ...s, ...p });
  const sel = 'h-8 bg-ground border border-line-strong rounded-sm px-2 text-[12px] font-mono';
  return <div className="space-y-3 max-w-xl">
    <div className="flex gap-1" role="radiogroup" aria-label="Schedule preset">{PRESET_OPTS.map(o => <button key={o.v} type="button" role="radio" aria-checked={s.preset === o.v} onClick={() => up({ preset: o.v })}
      className={`h-7 px-3 rounded-sm border text-[12px] ${s.preset === o.v ? 'border-signal bg-signal-soft text-signal' : 'border-line hover:border-line-strong'}`} data-testid={`button-preset-${o.v}`}>{o.l}</button>)}</div>
    {s.preset !== 'manual' && s.preset !== 'custom' && <div className="flex items-center gap-2 text-[12px]">
      {s.preset === 'weekly' && <select className={sel} value={s.weekday} onChange={e => up({ weekday: Number(e.target.value) })} aria-label="Weekday" data-testid="select-weekday">{DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</select>}
      {s.preset === 'monthly' && <select className={sel} value={s.monthday} onChange={e => up({ monthday: Number(e.target.value) })} aria-label="Day of month" data-testid="select-monthday">{Array.from({ length: 28 }, (_, i) => <option key={i} value={i + 1}>day {i + 1}</option>)}</select>}
      <span className="text-ink-muted">at</span>
      <select className={sel} value={s.hour} onChange={e => up({ hour: Number(e.target.value) })} aria-label="Hour" data-testid="select-hour">{Array.from({ length: 24 }, (_, i) => <option key={i} value={i}>{String(i).padStart(2, '0')}</option>)}</select>:
      <select className={sel} value={s.minute} onChange={e => up({ minute: Number(e.target.value) })} aria-label="Minute" data-testid="select-minute">{[0, 15, 30, 45].map(m => <option key={m} value={m}>{String(m).padStart(2, '0')}</option>)}</select>
    </div>}
    {s.preset === 'custom' && <div><Label htmlFor="cron" className="text-[11px] text-ink-muted">Cron (minute hour day month weekday)</Label><Input id="cron" className="h-8 w-64 font-mono" placeholder="0 6 * * 1-5" value={s.custom} onChange={e => up({ custom: e.target.value })} data-testid="input-cron" /></div>}
    <div><Label htmlFor="tz" className="text-[11px] text-ink-muted">Timezone</Label>
      <Input id="tz" list="tz-list" className="h-8 w-64 font-mono text-[12px]" value={form.timezone} onChange={e => set('timezone', e.target.value)} data-testid="input-timezone" /><datalist id="tz-list">{tzs.map(t => <option key={t} value={t} />)}</datalist></div>
    <div className="border border-line bg-surface-raised rounded-sm p-2 text-[12px]"><span className="text-ink-muted">Preview: </span><span className="text-ink" data-testid="text-schedule-preview">{describeSchedule(buildCron(s), form.timezone)}</span>{buildCron(s) && <span className="ml-2 font-mono text-ink-muted">{buildCron(s)}</span>}</div>
    {error && <p className="text-danger text-[11px]" data-testid="error-schedule">{error}</p>}
    <p className="text-[11px] text-ink-muted">The server validates the exact expression and computes the next run on save.</p>
  </div>;
}

function Review({ form, destination, destType, role, problems }: { form: ActivationForm; destination: Destination | null; destType?: string; role: Role; problems: string[] }) {
  const seg = useSegment(form.segment_id ?? undefined);
  const row = (k: string, v: ReactNode) => <div className="grid grid-cols-[140px_1fr] py-1.5 border-t border-line first:border-0 text-[12px]"><span className="text-ink-muted">{k}</span><span>{v}</span></div>;
  return <div className="max-w-2xl space-y-3">
    <div className="border border-line rounded-sm px-3">
      {row('Segment', seg.data ? `${seg.data.name} · ${seg.data.last_count == null ? 'count pending' : plural(seg.data.last_count, 'member')}` : '—')}
      {row('Destination', destination ? <span className="inline-flex items-center gap-2">{destination.name}<TypeBadge type={destination.type} /></span> : '—')}
      {row('Columns', <span className="font-mono">{form.rows.filter(r => r.value).map(r => r.column).join(', ') || '—'}</span>)}
      {row('Consent', <span className="font-mono">{effectiveConsent(form, destType as never, role) ?? 'none'}{form.consent_override && isAdsType(destType as never) ? ' (admin override)' : ''}</span>)}
      {row('Schedule', describeSchedule(buildCron(form.schedule), form.timezone))}
    </div>
    <label className="flex items-center gap-2 text-[12px]"><Checkbox checked={form.is_active} onCheckedChange={() => undefined} disabled /> {form.is_active ? 'Active — scheduled runs enabled' : 'Paused'}</label>
    {problems.length ? <ul className="text-[12px] text-danger list-disc pl-4" data-testid="list-review-problems">{problems.map(p => <li key={p}>{p}</li>)}</ul> : <p className="text-[12px] text-ok">Ready. Saving does not send anything.</p>}
  </div>;
}
