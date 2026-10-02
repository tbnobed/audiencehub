import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, Eye, RefreshCw, Save, Zap } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useSegment, useSegmentActions, useSegmentDelta, useSegmentFields, useSegmentHistory, useSegmentPreview, useSegmentSample, useSegments } from '@/hooks/use-segments';
import { toast } from '@/hooks/use-toast';
import type { SegmentDefinition } from '@/lib/segments';
import { ExplainPanel, JsonPanel } from './json-panel';
import { LivePanel } from './live-panel';
import { SCHEDULES, blankDefinition, normalizeSchedule, countRules, parseIssue, summarize, validate, type Issue } from './model';
import { ctl, NativeSelect } from './rule-editor';
import { StatusPill, errorMessage, toastError, useSegmentRole, useUnsavedGuard } from './shared';
import { TreeEditor, type DeltaState } from './tree-editor';

type Draft = { name: string; description: string; schedule: string; definition: SegmentDefinition };
const blankDraft = (): Draft => ({ name: '', description: '', schedule: SCHEDULES[0].value, definition: blankDefinition() });
const snapshot = (d: Draft) => JSON.stringify(d);

export function SegmentBuilder({ id }: { id?: number }) {
  const navigate = useNavigate();
  const { canEdit, isAdmin } = useSegmentRole();
  const fieldsQ = useSegmentFields();
  const segmentQ = useSegment(id);
  const historyQ = useSegmentHistory(id);
  const allQ = useSegments('', '');
  const { save } = useSegmentActions();
  const delta = useSegmentDelta();
  const fields = useMemo(() => fieldsQ.data?.fields ?? [], [fieldsQ.data]);
  const giftValues = fieldsQ.data?.gift_values ?? {};
  const segments = useMemo(() => allQ.data?.items ?? [], [allQ.data]);
  const existing = segmentQ.data;
  const archived = existing?.status === 'archived';
  const readOnly = !canEdit || archived;

  const [draft, setDraft] = useState<Draft>(blankDraft);
  const baseline = useRef(snapshot(blankDraft()));
  const initFor = useRef<number | 'new' | null>(null);
  useEffect(() => {
    const key = id ?? 'new';
    if (initFor.current === key) return;
    if (!id) { initFor.current = 'new'; const b = blankDraft(); setDraft(b); baseline.current = snapshot(b); return; }
    if (existing && existing.id === id) {
      initFor.current = id;
      const d: Draft = { name: existing.name, description: existing.description ?? '', schedule: normalizeSchedule(existing.refresh_schedule), definition: existing.definition ?? blankDefinition() };
      setDraft(d); baseline.current = snapshot(d);
    }
  }, [id, existing]);
  const dirty = !readOnly && snapshot(draft) !== baseline.current;
  const guard = useUnsavedGuard(dirty);

  // Validation: client mirror + last server error (with node path when the API provides one).
  const [serverIssue, setServerIssue] = useState<Issue | null>(null);
  const [nameError, setNameError] = useState('');
  const clientIssues = useMemo(() => fields.length ? validate(draft.definition, fields) : [], [draft.definition, fields]);
  const ruleCount = countRules(draft.definition.root);

  // 800 ms debounce for live preview; null = do not query (no fake counts).
  const defKey = JSON.stringify(draft.definition);
  const [debouncedKey, setDebouncedKey] = useState(defKey);
  useEffect(() => { const t = setTimeout(() => setDebouncedKey(defKey), 800); return () => clearTimeout(t); }, [defKey]);
  const pending = debouncedKey !== defKey;
  const idleReason = !fields.length ? 'loading-fields' as const : ruleCount === 0 ? 'empty' as const : clientIssues.length ? 'invalid' as const : null;
  const liveDef = useMemo<SegmentDefinition | null>(() => idleReason || pending ? null : JSON.parse(debouncedKey) as SegmentDefinition, [idleReason, pending, debouncedKey]);
  // Keep the previous definition queried while typing so the panel dims instead of blanking.
  const [queried, setQueried] = useState<SegmentDefinition | null>(null);
  useEffect(() => { if (liveDef) setQueried(liveDef); else if (idleReason) setQueried(null); }, [liveDef, idleReason]);
  const preview = useSegmentPreview(queried);
  const sample = useSegmentSample(queried);
  const previewIssue = preview.isError ? parseIssue(errorMessage(preview.error)) : null;
  const issues = useMemo(() => [...clientIssues, ...(previewIssue ? [previewIssue] : []), ...(serverIssue ? [serverIssue] : [])], [clientIssues, previewIssue, serverIssue]);
  useEffect(() => { setServerIssue(null); }, [defKey]);

  // Lazy per-node exclusion delta, computed on hover, cached per definition.
  const [deltas, setDeltas] = useState<Record<string, DeltaState>>({});
  const deltaFor = useRef<string>('');
  const mutateDelta = useRef(delta.mutateAsync);
  mutateDelta.current = delta.mutateAsync;
  useEffect(() => { setDeltas({}); deltaFor.current = queried ? JSON.stringify(queried) : ''; }, [queried]);
  const requested = useRef(new Set<string>());
  useEffect(() => { requested.current = new Set(); }, [queried]);
  const requestDelta = useCallback((path: string) => {
    if (!queried || pending || requested.current.has(path)) return;
    requested.current.add(path);
    const forKey = deltaFor.current;
    setDeltas(p => ({ ...p, [path]: { status: 'loading' } }));
    mutateDelta.current({ definition: queried, path })
      .then(r => { if (deltaFor.current === forKey) setDeltas(p => ({ ...p, [path]: { status: 'ok', delta: r.delta } })); })
      .catch(e => { if (deltaFor.current === forKey) setDeltas(p => ({ ...p, [path]: { status: 'error', message: errorMessage(e) } })); });
  }, [queried, pending]);

  const segmentName = useCallback((sid: number) => segments.find(s => s.id === sid)?.name, [segments]);
  const summary = fields.length ? summarize(draft.definition, fields, segmentName) : '';

  const submit = (status: 'draft' | 'active') => {
    if (!draft.name.trim()) { setNameError('Name this audience before saving.'); document.getElementById('segment-name')?.focus(); return; }
    if (clientIssues.length) { toast({ variant: 'destructive', title: 'Fix rule errors first', description: `${clientIssues[0].path}: ${clientIssues[0].message}` }); return; }
    save.mutate({ id, name: draft.name.trim(), description: draft.description.trim() || null, definition: draft.definition, status, refresh_schedule: draft.schedule === 'manual' ? null : draft.schedule }, {
      onSuccess: saved => {
        baseline.current = snapshot(draft);
        toast({ title: status === 'active' ? 'Audience saved and active' : 'Draft saved', description: status === 'active' ? `${saved.name} will materialize now.` : saved.name });
        navigate(`/segments/${saved.id}`, { replace: !id });
      },
      onError: e => { const issue = parseIssue(errorMessage(e)); setServerIssue(issue); toastError('Save failed', e); },
    });
  };

  if (id && segmentQ.isLoading) return <BuilderSkeleton />;
  if (id && segmentQ.isError) return <div className="p-6 text-sm" data-testid="error-segment"><p className="text-danger">Unable to load this audience.</p><p className="text-ink-muted text-xs mt-1">{errorMessage(segmentQ.error)}</p><div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => segmentQ.refetch()}><RefreshCw size={12} className="mr-1" /> Retry</Button><Button size="sm" variant="ghost" asChild><Link to="/segments">Back to audiences</Link></Button></div></div>;

  return <div className="flex flex-col gap-3 pb-8 min-w-0" data-testid="page-segment-builder">
    <header className="flex flex-wrap items-start gap-3">
      <button type="button" onClick={() => guard(() => navigate(id ? `/segments/${id}` : '/segments'))} className="mt-1 text-ink-muted hover:text-ink" aria-label="Back" data-testid="button-back"><ArrowLeft size={16} /></button>
      <div className="flex-1 min-w-[260px] flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <input id="segment-name" aria-label="Audience name" data-testid="input-segment-name" value={draft.name} disabled={readOnly} maxLength={200} placeholder="Untitled partner audience"
            onChange={e => { setDraft(d => ({ ...d, name: e.target.value })); setNameError(''); }}
            className={`kin-title bg-transparent outline-none border-b ${nameError ? 'border-danger' : 'border-transparent focus:border-signal'} placeholder:text-ink-muted/60 min-w-0 flex-1 disabled:opacity-100`} />
          {existing && <StatusPill status={existing.status} testId="status-segment" />}
          {dirty && <span className="text-[10px] font-mono uppercase tracking-wider text-warn" data-testid="text-unsaved">unsaved</span>}
        </div>
        {nameError && <p className="text-[11px] text-danger" role="alert">{nameError}</p>}
        <input aria-label="Description" data-testid="input-segment-description" value={draft.description} disabled={readOnly} placeholder="What is this audience for? Who uses it?"
          onChange={e => setDraft(d => ({ ...d, description: e.target.value }))} className={`${ctl} border-transparent bg-transparent px-0 text-ink-muted w-full`} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1.5 text-[11px] text-ink-muted">Refresh
          <NativeSelect aria-label="Refresh schedule" data-testid="select-schedule" value={draft.schedule} disabled={readOnly} onChange={e => setDraft(d => ({ ...d, schedule: e.target.value }))}>
            {SCHEDULES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
          </NativeSelect>
        </label>
        {!readOnly && <>
          <Button size="sm" variant="outline" className="h-8 text-xs" disabled={save.isPending} onClick={() => submit('draft')} data-testid="button-save-draft"><Save size={12} className="mr-1" />{existing?.status === 'active' ? 'Save as draft' : 'Save draft'}</Button>
          <Button size="sm" className="h-8 text-xs" disabled={save.isPending} onClick={() => submit('active')} data-testid="button-save-active"><Zap size={12} className="mr-1" />{save.isPending ? 'Saving…' : existing?.status === 'active' ? 'Save active' : 'Save & activate'}</Button>
        </>}
      </div>
    </header>

    {readOnly && <div className="flex items-center gap-2 rounded-sm border border-line bg-surface px-3 py-2 text-xs text-ink-muted" data-testid="banner-readonly">
      <Eye size={13} />{archived ? 'Archived audiences are read-only. Duplicate it from the detail page to reuse these rules.' : 'You have view access. Counts and samples are live; editing needs an analyst role.'}
    </div>}

    <section className="rounded-sm border border-line bg-surface px-3 py-2" aria-label="Summary">
      <div className="text-[10px] uppercase tracking-wider text-ink-muted mb-0.5">Reads as</div>
      {fields.length ? <p className="text-[13px] leading-5 text-ink" data-testid="text-summary">{summary}</p> : fieldsQ.isError ? <p className="text-xs text-danger">Field registry unavailable: {errorMessage(fieldsQ.error)} <button className="text-signal underline" onClick={() => fieldsQ.refetch()}>Retry</button></p> : <Skeleton className="h-4 w-2/3 bg-surface-raised" />}
    </section>

    <div className="grid gap-3 lg:grid-cols-[minmax(0,3fr)_minmax(300px,2fr)] items-start">
      <Tabs defaultValue="rules" className="min-w-0">
        <div className="flex items-center justify-between gap-2">
          <TabsList className="h-8 bg-surface border border-line">
            <TabsTrigger value="rules" className="text-xs h-6" data-testid="tab-rules">Rules <span className="ml-1.5 font-mono text-[10px] text-ink-muted">{ruleCount}/50</span></TabsTrigger>
            <TabsTrigger value="json" className="text-xs h-6" data-testid="tab-json">JSON</TabsTrigger>
          </TabsList>
          {issues.length > 0 && <span className="inline-flex items-center gap-1 text-[11px] text-danger" data-testid="text-issue-count"><AlertTriangle size={12} />{issues.length} issue{issues.length === 1 ? '' : 's'}</span>}
        </div>
        <TabsContent value="rules" className="mt-2">
          {fieldsQ.isLoading ? <div className="flex flex-col gap-2">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-12 w-full bg-surface-raised" />)}</div>
            : <div className="rounded-sm border border-line bg-surface p-2">
              <TreeEditor def={draft.definition} onChange={definition => setDraft(d => ({ ...d, definition }))} fields={fields} giftValues={giftValues} segments={segments} currentId={id}
                readOnly={readOnly} issues={issues} deltas={deltas} requestDelta={requestDelta} segmentName={segmentName} />
              {issues.filter(i => !/^root\.rules/.test(i.path) && i.path !== 'root').map((i, k) => <p key={k} role="alert" className="mt-2 text-[11px] font-mono text-danger">{i.path}: {i.message}</p>)}
              {issues.filter(i => i.path === 'root').map((i, k) => <p key={k} role="alert" className="mt-2 text-[11px] font-mono text-danger">{i.path}: {i.message}</p>)}
            </div>}
        </TabsContent>
        <TabsContent value="json" className="mt-2 flex flex-col gap-2">
          <JsonPanel definition={draft.definition} fields={fields} mode={isAdmin && !archived ? 'edit' : 'read'} onApply={definition => { setDraft(d => ({ ...d, definition })); toast({ title: 'Definition applied', description: 'The tree now reflects the imported JSON.' }); }} />
          {isAdmin && <ExplainPanel definition={liveDef ?? queried} />}
        </TabsContent>
      </Tabs>
      <div className="lg:sticky lg:top-2">
        <LivePanel idle={{ reason: idleReason, pending }} preview={preview} sample={sample} history={id ? historyQ.data?.items ?? [] : undefined} lastCount={existing?.last_count} segmentId={id} canExport={canEdit} />
      </div>
    </div>
  </div>;
}

function BuilderSkeleton() {
  return <div className="flex flex-col gap-3" data-testid="skeleton-builder">
    <Skeleton className="h-8 w-80 bg-surface-raised" /><Skeleton className="h-10 w-full bg-surface-raised" />
    <div className="grid gap-3 lg:grid-cols-[3fr_2fr]"><div className="flex flex-col gap-2">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-12 bg-surface-raised" />)}</div><Skeleton className="h-72 bg-surface-raised" /></div>
  </div>;
}
