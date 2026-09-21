// Build-time only. Manifests and executable code are reviewed with the image.
// Two pinned distributions: an npm package at an exact version, or a single
// binary at an exact https URL verified against its manifest sha256.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const BINARIES = '/usr/local/lib/forge-harnesses';

function validate(m) {
  if (m.contract_version !== 1 || !/^\d+\.\d+\.\d+$/.test(m.version) || !/^[a-z][a-z0-9_-]*$/.test(m.id)) throw Error('Invalid harness manifest');
  if (!/^[a-z][a-z0-9_-]*$/.test(m.command)) throw Error('Invalid harness command name');
  if (m.distribution === 'binary') {
    if (!/^https:\/\/x\.ai\/cli\/[A-Za-z0-9._-]+$/.test(m.url) || !/^[a-f0-9]{64}$/.test(m.sha256)) throw Error('Binary harness needs a pinned https://x.ai/cli/ URL and sha256');
  } else if (typeof m.package !== 'string' || typeof m.binary !== 'string') {
    throw Error('npm harness needs package and binary');
  }
}

function target(m) {
  return m.distribution === 'binary' ? path.join(BINARIES, m.id, m.version, m.command)
                                     : path.join('/usr/local/lib/node_modules', m.package, m.binary);
}

async function fetchBinary(m) {
  const response = await fetch(m.url, {redirect: 'follow'});
  if (!response.ok) throw Error('Download failed for ' + m.id + ': ' + response.status);
  const data = Buffer.from(await response.arrayBuffer());
  const digest = crypto.createHash('sha256').update(data).digest('hex');
  if (digest !== m.sha256) throw Error('Checksum mismatch for ' + m.id + ' (' + digest + ')');
  const file = target(m);
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, data, {mode: 0o755});
}

async function main() {
  fs.mkdirSync(BINARIES, {recursive: true});
  for (const dir of fs.readdirSync(__dirname).sort()) {
    const file = path.join(__dirname, dir, 'manifest.json');
    if (!fs.existsSync(file)) continue;
    const m = JSON.parse(fs.readFileSync(file, 'utf8'));
    validate(m);
    if (process.argv.includes('--links-only')) {
      fs.symlinkSync(target(m), '/usr/local/bin/' + m.command);
      for (const [name, destination] of Object.entries(m.managed_files || {})) {
        if (path.basename(name) !== name || !destination.startsWith('/etc/')) throw Error('Invalid managed policy destination');
        fs.mkdirSync(path.dirname(destination), {recursive: true});
        fs.copyFileSync(path.join(__dirname, dir, name), destination);
        fs.chmodSync(destination, 0o644);
      }
    } else if (m.distribution === 'binary') {
      await fetchBinary(m);
    } else {
      execFileSync('npm', ['install', '--global', m.package + '@' + m.version], {stdio: 'inherit'});
    }
  }
}

main().catch(error => { console.error(error.message); process.exit(1); });
