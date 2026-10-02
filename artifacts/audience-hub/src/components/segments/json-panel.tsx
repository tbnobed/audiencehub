import { useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, Copy, FileUp, Lock, Play } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { useSegmentExplain } from '@/hooks/use-segments';
import type { SegmentDefinition, SegmentField } from '@/lib/segments';
import { parseDefinition, validate, type Issue } from './model';

export function JsonPanel({ definition, fields, mode, onApply }: { definition: SegmentDefinition; fields: SegmentField[]; mode: 'edit' | 'read'; onApply: (d: SegmentDefinition) => void }) {
  const pretty = JSON.stringify(definition, null, 2);
  const [text, setText] = useState(pretty);
  const [touched, setTouched] = useState(false);
  const [issues, setIssues] = useState<Issue[] | null>(null);
  const [copied, setCopied] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (!touched) setText(pretty); }, [pretty, touched]);
  const check = (t = text) => {
    const parsed = parseDefinition(t);
    const found = parsed.definition ? validate(parsed.definition, fields) : parsed.issues;
    setIssues(found);
    return found.length ? null : parsed.definition!;
  };
  const apply = () => { const d = check(); if (d) { onApply(d); setTouched(false); } };
  const copy = async () => { try { await navigator.clipboard.writeText(pretty); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { setIssues([{ path: 'clipboard', message: 'Clipboard unavailable — select the text and copy manually.' }]); } };
  const editable = mode === 'edit';
  return <div className="flex flex-col gap-2" data-testid="panel-json">
    <div className="flex flex-wrap items-center gap-1.5">
      {!editable && <span className="inline-flex items-center gap-1 text-[11px] text-ink-muted mr-auto"><Lock size={11} /> Read-only. Admins can edit and import JSON.</span>}
      {editable && <span className="text-[11px] text-ink-muted mr-auto">Paste a definition from another environment, validate, then apply to the tree.</span>}
      <Button type="button" variant="outline" size="sm" className="h-7 text-xs" onClick={copy} data-testid="button-copy-json">{copied ? <Check size={12} className="mr-1" /> : <Copy size={12} className="mr-1" />}{copied ? 'Copied' : 'Copy'}</Button>
      {editable && <>
        <input ref={fileRef} type="file" accept="application/json,.json" className="hidden" data-testid="input-import-json"
          onChange={async e => { const f = e.target.files?.[0]; if (!f) return; const t = await f.text(); setText(t); setTouched(true); check(t); e.target.value = ''; }} />
        <Button type="button" variant="outline" size="sm" className="h-7 text-xs" onClick={() => fileRef.current?.click()} data-testid="button-import-json"><FileUp size={12} className="mr-1" /> Import file</Button>
        <Button type="button" variant="outline" size="sm" className="h-7 text-xs" onClick={() => check()} data-testid="button-validate-json">Validate</Button>
        <Button type="button" size="sm" className="h-7 text-xs" onClick={apply} disabled={!touched} data-testid="button-apply-json">Apply to tree</Button>
      </>}
    </div>
    <textarea aria-label="Definition JSON" data-testid="textarea-json" readOnly={!editable} spellCheck={false} value={editable ? text : pretty}
      onChange={e => { setText(e.target.value); setTouched(true); setIssues(null); }}
      className="min-h-[420px] w-full resize-y rounded-sm border border-line bg-background p-3 font-mono text-[12px] leading-5 text-ink outline-none focus-visible:border-signal" />
    {issues && (issues.length
      ? <ul className="rounded-sm border border-danger/40 bg-danger/10 p-2 text-[11px] font-mono text-danger" role="alert" data-testid="list-json-issues">{issues.map((i, k) => <li key={k}>{i.path}: {i.message}</li>)}</ul>
      : <p className="text-[11px] text-ok" data-testid="text-json-valid">Definition is valid against the field registry{touched ? ' — apply to load it into the tree.' : '.'}</p>)}
  </div>;
}

export function ExplainPanel({ definition }: { definition: SegmentDefinition | null }) {
  const explain = useSegmentExplain();
  const [open, setOpen] = useState(false);
  return <Collapsible open={open} onOpenChange={setOpen} className="rounded-sm border border-line bg-surface">
    <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-3 h-8 text-[11px] uppercase tracking-wider text-ink-muted hover:text-ink" data-testid="button-toggle-explain">
      <ChevronRight size={12} className={`transition-transform ${open ? 'rotate-90' : ''}`} /> Query plan (EXPLAIN) <span className="ml-auto normal-case tracking-normal text-[10px]">admin</span>
    </CollapsibleTrigger>
    <CollapsibleContent className="px-3 pb-3">
      <Button type="button" size="sm" variant="outline" className="h-7 text-xs" disabled={!definition || explain.isPending} onClick={() => definition && explain.mutate(definition)} data-testid="button-run-explain"><Play size={11} className="mr-1" />{explain.isPending ? 'Planning…' : 'Run EXPLAIN'}</Button>
      {!definition && <p className="mt-2 text-[11px] text-ink-muted">Needs a valid, nonempty definition.</p>}
      {explain.isError && <p className="mt-2 text-[11px] text-danger" role="alert">{explain.error instanceof Error ? explain.error.message : 'EXPLAIN failed.'}</p>}
      {explain.data && <pre className="mt-2 max-h-80 overflow-auto rounded-sm bg-background p-2 font-mono text-[11px] text-ink-muted" data-testid="text-explain-plan">{JSON.stringify(explain.data.plan, null, 2)}</pre>}
    </CollapsibleContent>
  </Collapsible>;
}
