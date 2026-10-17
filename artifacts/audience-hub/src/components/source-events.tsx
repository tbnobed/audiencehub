import { useState } from 'react';
import { useAuth } from '@/hooks/use-auth';
import { Source, useSourceEvents, useSDKManifest } from '@/hooks/use-sources';
import { fetchApi } from '@/lib/api';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';

function snippet(host: string, key: string, sha256: string) {
  // Escape HTML parser-sensitive characters even inside JavaScript strings.
  const js = (value: string) => JSON.stringify(value).replace(/</g, '\\u003c');
  return `<script>
window.ahConfig = {writeKey: ${js(key)}, host: ${js(host)}};
window.ah = window.ah || {q: []};
["page","track","identify","flush","optOut"].forEach(function(m) {
  if (!window.ah[m]) window.ah[m] = function() {
    if (m === "optOut") window.ah.q = [["optOut"]];
    else if (window.ah.q.length < 100) window.ah.q.push([m].concat(Array.prototype.slice.call(arguments)));
  };
});
ah.page();
var s = document.createElement("script");
s.async = true;
s.src = ${js(host + '/sdk/ah.js?v=' + sha256)};
document.head.appendChild(s);
</script>`;
}

export function SourceEvents({ source }: { source: Source }) {
  const { user } = useAuth();
  const admin = user?.role === 'admin';
  const analyst = admin || user?.role === 'analyst';
  const [live, setLive] = useState(true);
  const [writeKey, setWriteKey] = useState('');
  const [host, setHost] = useState(user?.app_env === 'production' ? '' : window.location.origin);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [copyStatus, setCopyStatus] = useState('');
  const queryClient = useQueryClient();
  const events = useSourceEvents(source.id, !!analyst && live);
  const manifest = useSDKManifest(admin);
  let normalizedHost = '';
  try {
    const url = new URL(host);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.search || url.hash) throw Error();
    normalizedHost = url.href.replace(/\/$/, '');
  } catch { /* Show explicit field feedback below. */ }
  const code = writeKey && normalizedHost && manifest.data ? snippet(normalizedHost, writeKey, manifest.data.sha256) : '';
  const generate = async () => {
    if (source.has_write_key && !window.confirm('Rotate this source write key? Existing installations will stop sending until updated.')) return;
    setSaving(true);
    setError('');
    setWriteKey('');
    setCopyStatus('');
    try {
      const result = await fetchApi(`/api/sources/${source.id}/write-key`, { method: 'POST', timeoutMs: 15000 });
      if (typeof result.write_key !== 'string' || !result.write_key) throw new Error('The server did not return a write key.');
      setWriteKey(result.write_key);
      void queryClient.invalidateQueries({ queryKey: ['sources'] });
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to generate write key.'); }
    finally { setSaving(false); }
  };
  return (
    <section className="space-y-6" aria-label={`${source.name} event installation`}>
      {admin && <div className="space-y-3 rounded-md border p-4">
        <div className="flex items-center justify-between gap-3">
          <h3 className="font-semibold">JavaScript installation</h3>
          <Badge variant="outline">{source.has_write_key ? 'Key configured' : 'No write key'}</Badge>
        </div>
        <p className="text-sm text-muted-foreground">Generate a browser write key. The full key is shown only in this dialog, once. Rotation invalidates existing installations.</p>
        {user?.app_env !== 'production' && <p className="text-sm"><a className="text-primary underline" href={`${import.meta.env.BASE_URL}sdk/test.html`} target="_blank" rel="noreferrer">Open development SDK acceptance page</a></p>}
        <Button onClick={generate} disabled={saving || !manifest.data}>{saving ? 'Generating…' : source.has_write_key ? 'Rotate write key' : 'Generate write key'}</Button>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {manifest.isLoading && <p role="status" className="text-sm">Loading SDK content hash…</p>}
        {manifest.isError && <p role="alert" className="text-sm text-destructive">
          {manifest.error.message} Snippet cannot be generated without verified SDK metadata.
          <Button variant="ghost" size="sm" onClick={() => void manifest.refetch()}>Retry metadata</Button>
        </p>}
        {writeKey && <>
          <Label htmlFor="sdk-host">Audience Hub public host (editable)</Label>
          <Input id="sdk-host" value={host} onChange={e => { setHost(e.target.value); setCopyStatus(''); }} placeholder="Enter your deployed HTTPS host" />
          {!normalizedHost && <p role="alert" className="text-sm text-destructive">Enter a valid HTTP(S) host without credentials, query, or fragment.</p>}
          <p className="text-xs text-muted-foreground">Use the actual public ingestion host in production. Allow the website origin in INGEST_CORS_ORIGINS. SDK honors browser GPC; opt out with ah.optOut().</p>
          {code && <>
            <Textarea aria-label="Once-only SDK snippet" readOnly value={code} className="h-60 font-mono text-xs" />
            <div className="flex gap-2">
              <Button variant="outline" onClick={async () => {
                try { await navigator.clipboard.writeText(code); setCopyStatus('Snippet copied.'); }
                catch { setCopyStatus('Clipboard unavailable. Select and copy the snippet manually.'); }
              }}>Copy snippet</Button>
              <Button variant="ghost" onClick={() => setWriteKey('')}>Dismiss key</Button>
            </div>
            <p role="status" className="text-sm">{copyStatus}</p>
          </>}
        </>}
      </div>}
      {analyst && <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Live event tail</h3>
          <Button size="sm" variant="outline" onClick={() => setLive(!live)}>{live ? 'Pause' : 'Resume'}</Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Latest 50 events · {live ? 'polling every 3 seconds' : 'paused; events may be stale'}
          {events.dataUpdatedAt > 0 && ` · last received ${new Date(events.dataUpdatedAt).toLocaleTimeString()}`}
        </p>
        <p className="text-xs text-muted-foreground">Metadata only. Event payloads and contact identifiers are not exposed here.</p>
        {events.isError && <div role="alert" className="text-sm text-destructive">
          Event tail unavailable: {events.error.message}. {events.data && 'Showing stale events.'}
          <Button size="sm" variant="ghost" onClick={() => void events.refetch()}>Retry</Button>
        </div>}
        {events.isLoading && <p role="status">Loading events…</p>}
        {events.data?.items.length === 0 && <p className="text-sm text-muted-foreground">No events received yet. Install the snippet or use the development SDK acceptance page.</p>}
        <div className="max-h-80 overflow-auto space-y-2">
          {events.data?.items.map(event => <article key={event.id} className="rounded-md border p-3 text-sm">
            <div className="flex justify-between gap-3">
              <strong>{event.name || event.type || 'Event'}</strong>
              <span className="text-xs text-muted-foreground">{event.occurred_at || event.received_at || ''}</span>
            </div>
            <p className="font-mono text-xs text-muted-foreground">{event.profile_id ? `Profile #${event.profile_id}` : 'Anonymous / awaiting resolution'}</p>
            {event.timestamp_clamped && <Badge variant="outline">Timestamp clamped</Badge>}
          </article>)}
        </div>
      </div>}
    </section>
  );
}
