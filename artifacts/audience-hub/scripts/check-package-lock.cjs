const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

// The standalone Docker image must not depend on workspace registry access.
const file = process.argv[2] || path.join(__dirname, '..', 'package-lock.json');
const lock = JSON.parse(fs.readFileSync(file, 'utf8'));
assert.ok(lock.packages, 'Expected a modern standalone npm lockfile');
for (const [name, pkg] of Object.entries(lock.packages)) {
  assert.ok(!pkg.link, `${name}: local package links are not portable`);
  if (!pkg.resolved) continue;
  const url = new URL(pkg.resolved);
  assert.ok(url.protocol === 'https:' && url.hostname === 'registry.npmjs.org' &&
    !url.username && !url.password && !url.port,
    `${name}: standalone packages must resolve through https://registry.npmjs.org`);
}
console.log('Standalone lockfile: public registry URLs, no workspace links.');