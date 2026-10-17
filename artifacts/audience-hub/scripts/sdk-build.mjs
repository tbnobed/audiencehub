import { build } from 'vite';
import { readFile, writeFile, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';

await build({ configFile: new URL('../vite.sdk.config.ts', import.meta.url).pathname });
const bundle = new URL('../dist/sdk/ah.js', import.meta.url);
const size = (await stat(bundle)).size;
if (size >= 5000) throw new Error(`SDK must be <5000 bytes, got ${size}`);
const sha256 = createHash('sha256').update(await readFile(bundle)).digest('hex');
const manifest = { file: 'ah.js', sha256, url: `/sdk/ah.js?v=${sha256}` };
await writeFile(new URL('../dist/sdk/manifest.json', import.meta.url), JSON.stringify(manifest) + '\n');
const acceptance = await readFile(new URL('../sdk/test.html', import.meta.url), 'utf8');
await writeFile(new URL('../dist/sdk/test.html', import.meta.url), acceptance.replaceAll('__AH_SDK_SHA256__', sha256));
console.log(`SDK acceptance: ${size} bytes (<5000).`);
