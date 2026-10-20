type Config = { writeKey: string; host?: string; ignoreGPC?: boolean };
type Json = Record<string, unknown>;
type Entry = { data: Json; attempts: number };
type SDK = {
  page(name?: string, properties?: Json): void;
  track(event: string, properties?: Json): void;
  identify(userId: string, traits?: Json): void;
  flush(): Promise<void>;
  optOut(): void;
};

(() => {
  const w = window as unknown as { ahConfig?: Config; ah?: SDK & { q?: unknown[][] } };
  const config = w.ahConfig;
  const pending = w.ah?.q || [];
  if (w.ah && !w.ah.q) return;
  const read = (key: string) => {
    try {
      const cookie = document.cookie.split('; ').find(c => c.startsWith(key + '='));
      if (cookie) return decodeURIComponent(cookie.slice(key.length + 1));
    } catch { /* Storage may be disabled. */ }
    try { return localStorage.getItem(key); } catch { return null; }
  };
  const store = (key: string, value: string) => {
    try {
      document.cookie = `${key}=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
    } catch { /* Try the first-party storage fallback. */ }
    try { localStorage.setItem(key, value); } catch { /* In-memory session only. */ }
  };
  let stopped = read('ah_optout') === '1' ||
    ((navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl === true && config?.ignoreGPC !== true);
  let endpoint = '';
  try {
    const host = new URL(config?.host || location.origin);
    if (!/^https?:$/.test(host.protocol) || host.username || host.password || host.search || host.hash) throw Error();
    endpoint = host.href.replace(/\/$/, '') + '/v1/batch';
  } catch { console.warn('Audience Hub: invalid HTTP(S) host.'); }
  const valid = !!endpoint && typeof config?.writeKey === 'string' && config.writeKey.length > 0 && config.writeKey.length <= 256;
  if (!valid) console.warn('Audience Hub: configure a browser write key and HTTP(S) host before loading the SDK.');
  let anonymousId = '';
  let userId: string | undefined;
  let queue: Entry[] = [];
  let active: AbortController | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  const uuid = () => crypto.randomUUID();
  const blocked = () => stopped || read('ah_optout') === '1' ||
    ((navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl === true && config?.ignoreGPC !== true);
  const optOut = () => {
    stopped = true;
    store('ah_optout', '1');
    queue = [];
    active?.abort();
    clearInterval(timer);
    try { sessionStorage.removeItem('ah_fbclid'); sessionStorage.removeItem('ah_fbc'); } catch { /* Storage disabled. */ }
  };
  const warn = () => console.warn('Audience Hub: invalid event or queue limit reached.');
  const depthOK = (value: unknown, depth = 1): boolean => {
    if (!value || typeof value !== 'object') return true;
    return depth <= 5 && Object.values(value).every(v => depthOK(v, depth + 1));
  };
  const add = (type: string, fields: Json) => {
    if (blocked() || !valid) return;
    if (queue.length >= 100) { warn(); return; }
    try {
      if (!depthOK(fields.properties) || !depthOK(fields.traits)) throw Error();
      if (!anonymousId) {
        anonymousId = read('ah_aid') || uuid();
        store('ah_aid', anonymousId);
      }
      // First-party attribution only. Never identify or merge on these IDs.
      // Only collect after opt-out/GPC checks; do not load Meta's Pixel.
      const click = new URLSearchParams(location.search).get('fbclid');
      if (click && /^[A-Za-z0-9._~-]{1,512}$/.test(click)) {
        try {
          if (sessionStorage.getItem('ah_fbclid') !== click) {
            sessionStorage.setItem('ah_fbc', `fb.1.${Date.now()}.${click}`);
          }
          sessionStorage.setItem('ah_fbclid', click);
        } catch { /* Session-only memory unavailable. */ }
      }
      let fbclid = click;
      try { fbclid ||= sessionStorage.getItem('ah_fbclid'); } catch { /* Storage disabled. */ }
      const attribution: Json = {};
      for (const [key, value] of [['fbclid', fbclid], ['_fbp', read('_fbp')], ['_fbc', read('_fbc')]]) {
        if (typeof value === 'string' && value.length <= (key === '_fbc' ? 600 : 512) && /^[A-Za-z0-9._~-]+$/.test(value)) attribution[key!] = value;
      }
      if (!attribution._fbc && typeof attribution.fbclid === 'string') {
        try { attribution._fbc = sessionStorage.getItem('ah_fbc'); } catch { /* Storage disabled. */ }
        attribution._fbc ||= `fb.1.${Date.now()}.${attribution.fbclid}`;
      }
      const data = JSON.parse(JSON.stringify({
        ...fields, ...attribution, type, anonymousId, userId, messageId: uuid(), timestamp: new Date().toISOString(),
      }));
      // A batch stays below the browser keepalive / beacon 64 KiB budget.
      if (new TextEncoder().encode(JSON.stringify(data)).length > 30000) throw Error();
      queue.push({ data, attempts: 0 });
      if (queue.length >= 20) void flush();
    } catch { warn(); }
  };
  const take = (limit = 60000) => {
    const batch: Entry[] = [];
    let size = 12;
    while (queue.length && batch.length < 20) {
      const next = new TextEncoder().encode(JSON.stringify(queue[0].data)).length + 1;
      if (size + next > limit) break;
      size += next;
      batch.push(queue.shift()!);
    }
    return batch;
  };
  const body = (batch: Entry[]) => JSON.stringify({ batch: batch.map(e => e.data) });
  const retry = (batch: Entry[]) => {
    if (!blocked()) {
      const remaining = batch.filter(e => ++e.attempts < 3);
      if (remaining.length !== batch.length) console.warn('Audience Hub: retry limit reached; batch dropped.');
      if (remaining.length + queue.length > 100) warn();
      queue = remaining.concat(queue).slice(0, 100);
    }
  };
  async function flush() {
    if (blocked() || !valid || active || !queue.length) return;
    const batch = take();
    const controller = new AbortController();
    active = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Write-Key': config!.writeKey },
        body: body(batch), keepalive: true, credentials: 'omit', signal: controller.signal,
      });
      if (response.status === 429 || response.status >= 500) retry(batch);
      else if (!response.ok) console.warn('Audience Hub: ingestion rejected the batch.');
    } catch { retry(batch); }
    finally { clearTimeout(timeout); active = undefined; }
  }
  const sdk: SDK = {
    page(name, properties = {}) {
      if ((name !== undefined && (typeof name !== 'string' || name.length > 255)) || !properties || typeof properties !== 'object' || Array.isArray(properties)) { warn(); return; }
      add('page', { name, properties: { url: location.href, path: location.pathname, title: document.title, referrer: document.referrer, ...properties } });
    },
    track(event, properties = {}) {
      if (typeof event !== 'string' || !event.trim() || event.length > 255 || !properties || typeof properties !== 'object' || Array.isArray(properties)) { warn(); return; }
      add('track', { event, properties });
    },
    identify(id, traits = {}) {
      if (typeof id !== 'string' || !id.trim() || id.length > 255 || !traits || typeof traits !== 'object' || Array.isArray(traits)) { warn(); return; }
      userId = id;
      add('identify', { traits });
    },
    flush,
    optOut,
  };
  w.ah = sdk;
  if (pending.some(call => call[0] === 'optOut')) optOut();
  if (!blocked() && valid) {
    timer = setInterval(() => { void flush(); }, 2000);
    window.addEventListener('pagehide', () => {
      if (blocked() || !queue.length) return;
      // Never resend the batch currently owned by fetch.
      let budget = 60000;
      while (queue.length && budget > 0) {
        const batch = take(budget - 512);
        if (!batch.length) break;
        const payload = new Blob([JSON.stringify({ write_key: config!.writeKey, batch: batch.map(e => e.data) })], { type: 'application/json' });
        budget -= payload.size;
        try {
          if (!navigator.sendBeacon(endpoint, payload)) { retry(batch); break; }
        } catch { retry(batch); break; }
      }
    });
    for (const [method, ...args] of pending.slice(0, 100)) {
      if (method === 'page') sdk.page(args[0] as string, args[1] as Json);
      if (method === 'track') sdk.track(args[0] as string, args[1] as Json);
      if (method === 'identify') sdk.identify(args[0] as string, args[1] as Json);
      if (method === 'flush') void sdk.flush();
    }
  }
})();
