// Build-time only. Manifests and executable code are reviewed with the image.
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
for (const dir of fs.readdirSync(__dirname).sort()) {
  const file = path.join(__dirname, dir, 'manifest.json');
  if (!fs.existsSync(file)) continue;
  const m = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (m.contract_version !== 1 || !/^\d+\.\d+\.\d+$/.test(m.version)) throw Error('Invalid harness manifest');
  if (process.argv.includes('--links-only')) {
    fs.symlinkSync(path.join('/usr/local/lib/node_modules', m.package, m.binary), '/usr/local/bin/' + m.command);
    for (const [file, target] of Object.entries(m.managed_files || {})) {
      if (path.basename(file) !== file || !target.startsWith('/etc/')) throw Error('Invalid managed policy destination');
      fs.mkdirSync(path.dirname(target), {recursive: true});
      fs.copyFileSync(path.join(__dirname, dir, file), target);
      fs.chmodSync(target, 0o644);
    }
  } else {
    execFileSync('npm', ['install', '--global', m.package + '@' + m.version], {stdio: 'inherit'});
  }
}
