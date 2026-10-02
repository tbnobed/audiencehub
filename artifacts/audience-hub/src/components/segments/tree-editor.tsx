import { useState, type DragEvent, type ReactNode } from 'react';
import { ArrowDown, ArrowUp, Copy, FolderPlus, GripVertical, Plus, Trash2 } from 'lucide-react';
import type { Segment, SegmentDefinition, SegmentField, SegmentGroup, SegmentNode, SegmentRule } from '@/lib/segments';
import { MAX_DEPTH, countRules, MAX_RULES, duplicateNode, fieldFor, insertNode, isGroup, moveNode, pathString, removeNode, ruleFromField, updateNode, describeRule, signed, type Issue, type Path } from './model';
import { FieldPicker, RuleControls } from './rule-editor';

export type DeltaState = { status: 'loading' } | { status: 'ok'; delta: number } | { status: 'error'; message: string };

type Ctx = {
  def: SegmentDefinition; onChange: (d: SegmentDefinition) => void; fields: SegmentField[]; giftValues: Record<string, string[]>;
  segments: Segment[]; currentId?: number; readOnly: boolean; issues: Issue[]; deltas: Record<string, DeltaState>;
  requestDelta: (path: string) => void; drag: Path | null; setDrag: (p: Path | null) => void; segmentName: (id: number) => string | undefined;
};

const iconBtn = 'inline-flex h-6 w-6 items-center justify-center rounded-sm text-ink-muted hover:text-ink hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-signal disabled:opacity-30 disabled:pointer-events-none';

function DropZone({ ctx, group, index }: { ctx: Ctx; group: Path; index: number }) {
  const [over, setOver] = useState(false);
  if (!ctx.drag) return <div className="h-1" />;
  const onDrop = (e: DragEvent) => { e.preventDefault(); setOver(false); if (ctx.drag) ctx.onChange(moveNode(ctx.def, ctx.drag, group, index)); ctx.setDrag(null); };
  return <div onDragOver={e => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}
    data-testid={`drop-${pathString(group)}-${index}`}
    className={`h-2 -my-0.5 rounded-sm transition-colors ${over ? 'bg-signal/60' : 'bg-transparent'}`} />;
}

function DeltaBadge({ state }: { state?: DeltaState }) {
  if (!state) return <span className="text-[10px] text-ink-muted/60 font-mono hidden group-hover/row:inline">hover for impact</span>;
  if (state.status === 'loading') return <span className="text-[10px] text-ink-muted font-mono animate-pulse">measuring…</span>;
  if (state.status === 'error') return <span className="text-[10px] text-warn font-mono" title={state.message}>impact unavailable</span>;
  const d = state.delta;
  return <span className={`text-[10px] font-mono tabular-nums ${d > 0 ? 'text-warn' : d < 0 ? 'text-signal' : 'text-ink-muted'}`} data-testid="text-rule-delta"
    title="Change in partner count if this node were removed">removing {d > 0 ? 'adds' : d < 0 ? 'drops' : 'changes'} {d === 0 ? 'nothing' : signed(d).replace(/^[+−]/, '')}</span>;
}

function NodeChrome({ ctx, path, children, kind }: { ctx: Ctx; path: Path; children: ReactNode; kind: 'rule' | 'group' }) {
  const p = pathString(path);
  const index = path[path.length - 1];
  const parentLen = (() => { let n: SegmentNode = ctx.def.root; for (const i of path.slice(0, -1)) n = (n as SegmentGroup).rules[i]; return (n as SegmentGroup).rules.length; })();
  const full = countRules(ctx.def.root) >= MAX_RULES;
  return <div className="group/row flex items-start gap-1" onMouseEnter={() => ctx.requestDelta(p)} onFocus={() => ctx.requestDelta(p)}>
    {!ctx.readOnly && <button type="button" draggable aria-label="Drag to reorder" title="Drag to reorder" data-testid={`drag-${p}`}
      onDragStart={e => { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', p); ctx.setDrag(path); }}
      onDragEnd={() => ctx.setDrag(null)} className={`${iconBtn} cursor-grab mt-0.5 active:cursor-grabbing`}><GripVertical size={13} /></button>}
    <div className="flex-1 min-w-0">{children}</div>
    <div className="flex items-center gap-0.5 shrink-0 mt-0.5">
      <DeltaBadge state={ctx.deltas[p]} />
      {!ctx.readOnly && <>
        <button type="button" className={iconBtn} aria-label="Move up" title="Move up" disabled={index === 0} data-testid={`button-up-${p}`} onClick={() => ctx.onChange(moveNode(ctx.def, path, path.slice(0, -1), index - 1))}><ArrowUp size={12} /></button>
        <button type="button" className={iconBtn} aria-label="Move down" title="Move down" disabled={index >= parentLen - 1} data-testid={`button-down-${p}`} onClick={() => ctx.onChange(moveNode(ctx.def, path, path.slice(0, -1), index + 2))}><ArrowDown size={12} /></button>
        <button type="button" className={iconBtn} aria-label={`Duplicate ${kind}`} title="Duplicate" disabled={full} data-testid={`button-duplicate-${p}`} onClick={() => ctx.onChange(duplicateNode(ctx.def, path))}><Copy size={12} /></button>
        <button type="button" className={`${iconBtn} hover:text-danger`} aria-label={`Delete ${kind}`} title="Delete" data-testid={`button-delete-${p}`} onClick={() => ctx.onChange(removeNode(ctx.def, path))}><Trash2 size={12} /></button>
      </>}
    </div>
  </div>;
}

function RuleNode({ ctx, rule, path }: { ctx: Ctx; rule: SegmentRule; path: Path }) {
  const p = pathString(path);
  const field = fieldFor(rule, ctx.fields);
  const mine = ctx.issues.filter(i => i.path === p || i.path.startsWith(p + '.') && !i.path.slice(p.length).startsWith('.rules'));
  const issueAt = (suffix: string) => mine.some(i => i.path === p + suffix);
  const testId = `rule-${path.join('-')}`;
  return <div data-testid={testId} data-path={p} className={`rounded-sm border bg-surface px-2 py-1.5 ${mine.length ? 'border-danger/50' : 'border-line'} ${ctx.drag && ctx.drag.join() === path.join() ? 'opacity-40' : ''}`}>
    <NodeChrome ctx={ctx} path={path} kind="rule">
      <div className="flex flex-wrap items-center gap-1">
        <FieldPicker fields={ctx.fields} value={field} disabled={ctx.readOnly} testId={`${testId}-field`} onPick={f => ctx.onChange(updateNode(ctx.def, path, () => ruleFromField(f)))} />
        <RuleControls rule={rule} field={field} fields={ctx.fields} giftValues={ctx.giftValues} segments={ctx.segments} currentId={ctx.currentId}
          onChange={r => ctx.onChange(updateNode(ctx.def, path, () => r))} disabled={ctx.readOnly} issueAt={issueAt} testId={testId} />
      </div>
      <p className="mt-1 text-[11px] text-ink-muted truncate" title={describeRule(rule, ctx.fields, ctx.segmentName)}>{describeRule(rule, ctx.fields, ctx.segmentName)}</p>
      {mine.map((i, k) => <p key={k} className="text-[11px] text-danger font-mono" role="alert" data-testid={`issue-${testId}-${k}`}>{i.path}: {i.message}</p>)}
    </NodeChrome>
  </div>;
}

function GroupNode({ ctx, group, path }: { ctx: Ctx; group: SegmentGroup; path: Path }) {
  const p = pathString(path);
  const isRoot = !path.length;
  const depthHere = path.length + 1;
  const full = countRules(ctx.def.root) >= MAX_RULES;
  const groupIssues = ctx.issues.filter(i => i.path === p || i.path === p + '.op');
  const addRule = () => {
    const first = ctx.fields.find(f => f.type === 'trait') ?? ctx.fields[0];
    if (first) ctx.onChange(insertNode(ctx.def, path, ruleFromField(first)));
  };
  const addGroup = () => {
    const first = ctx.fields.find(f => f.type === 'trait') ?? ctx.fields[0];
    ctx.onChange(insertNode(ctx.def, path, { op: group.op === 'and' ? 'or' : 'and', rules: first ? [ruleFromField(first)] : [] }));
  };
  const accent = group.negate ? 'border-l-warn' : group.op === 'and' ? 'border-l-signal' : 'border-l-chart-4';
  const body = <div className={`border-l-2 ${accent} pl-2`}>
    <div className="flex flex-wrap items-center gap-1.5 py-1">
      <div role="radiogroup" aria-label="Group logic" className="inline-flex rounded-sm border border-line overflow-hidden">
        {(['and', 'or'] as const).map(op => <button key={op} type="button" role="radio" aria-checked={group.op === op} disabled={ctx.readOnly} data-testid={`toggle-${op}-${p}`}
          onClick={() => ctx.onChange(updateNode(ctx.def, path, n => ({ ...(n as SegmentGroup), op })))}
          className={`h-6 px-2 font-mono text-[10px] uppercase tracking-wider ${group.op === op ? (op === 'and' ? 'bg-signal text-primary-foreground' : 'bg-chart-4 text-background') : 'text-ink-muted hover:text-ink'} disabled:cursor-default`}>{op}</button>)}
      </div>
      <label className={`inline-flex items-center gap-1 h-6 px-2 rounded-sm border text-[10px] font-mono uppercase tracking-wider cursor-pointer ${group.negate ? 'border-warn/50 text-warn bg-warn/10' : 'border-line text-ink-muted'}`}>
        <input type="checkbox" className="sr-only" checked={!!group.negate} disabled={ctx.readOnly} data-testid={`toggle-negate-${p}`}
          onChange={e => ctx.onChange(updateNode(ctx.def, path, n => { const g = { ...(n as SegmentGroup) }; if (e.target.checked) g.negate = true; else delete g.negate; return g; }))} />
        not
      </label>
      <span className="text-[11px] text-ink-muted">{group.negate ? 'exclude partners matching ' : 'partners matching '}{group.op === 'and' ? 'all' : 'any'} of {group.rules.length} {group.rules.length === 1 ? 'node' : 'nodes'}</span>
      {groupIssues.map((i, k) => <span key={k} role="alert" className="text-[11px] text-danger font-mono">{i.path}: {i.message}</span>)}
    </div>
    <div className="flex flex-col">
      <DropZone ctx={ctx} group={path} index={0} />
      {group.rules.map((node, i) => <div key={i} className="flex flex-col">
        {isGroup(node) ? <GroupNode ctx={ctx} group={node} path={[...path, i]} /> : <RuleNode ctx={ctx} rule={node as SegmentRule} path={[...path, i]} />}
        <DropZone ctx={ctx} group={path} index={i + 1} />
      </div>)}
    </div>
    {!ctx.readOnly && <div className="flex items-center gap-1 pt-1">
      <button type="button" onClick={addRule} disabled={full || !ctx.fields.length} data-testid={`button-add-rule-${p}`} className="inline-flex items-center gap-1 h-6 px-2 rounded-sm text-[11px] text-signal hover:bg-signal-soft disabled:opacity-40"><Plus size={12} /> Rule</button>
      <button type="button" onClick={addGroup} disabled={full || depthHere >= MAX_DEPTH || !ctx.fields.length} title={depthHere >= MAX_DEPTH ? `Max depth ${MAX_DEPTH}` : undefined} data-testid={`button-add-group-${p}`} className="inline-flex items-center gap-1 h-6 px-2 rounded-sm text-[11px] text-ink-muted hover:text-ink hover:bg-surface-raised disabled:opacity-40"><FolderPlus size={12} /> Group</button>
    </div>}
  </div>;
  if (isRoot) return body;
  return <div data-testid={`group-${path.join('-')}`} className={`rounded-sm border border-line bg-background/40 px-1.5 py-1 ${ctx.drag && ctx.drag.join() === path.join() ? 'opacity-40' : ''}`}>
    <NodeChrome ctx={ctx} path={path} kind="group">{body}</NodeChrome>
  </div>;
}

export function TreeEditor(props: Omit<Ctx, 'drag' | 'setDrag'>) {
  const [drag, setDrag] = useState<Path | null>(null);
  const ctx: Ctx = { ...props, drag, setDrag };
  const empty = !props.def.root.rules.length;
  return <div data-testid="tree-editor" className="flex flex-col gap-2">
    <GroupNode ctx={ctx} group={props.def.root} path={[]} />
    {empty && <div className="rounded-sm border border-dashed border-line p-4 text-xs text-ink-muted" data-testid="empty-tree">
      {props.readOnly ? 'This audience has no rules, so it matches every active partner.' : 'No rules yet. Add a rule to start narrowing partners — try Giving · Partner status, or a gift count in the last 365 days.'}
    </div>}
  </div>;
}
