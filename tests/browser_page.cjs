// Use the same server-rendered shell as the dashboard HTTP handler.
const {execFileSync}=require('child_process');
const path=require('path');
const cache=new Map();
module.exports=(template,pathname)=>{
  const key=JSON.stringify([template,pathname]);
  if(!cache.has(key))cache.set(key,execFileSync('python3',['-c',`
import sys
from pathlib import Path
from forge.navigation import render
from forge.setup_help import render as help_page
name, route = sys.argv[1:]
page = help_page(Path('docs/onboarding.md').read_text()) if name == 'help' else (Path('legacy') / name).read_text()
print(render(page, route))
`,template,pathname],{cwd:path.join(__dirname,'..'),encoding:'utf8'}));
  return cache.get(key);
};
