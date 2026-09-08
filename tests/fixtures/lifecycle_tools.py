#!/usr/bin/python3
"""Strict external boundary simulator. Unknown commands fail; none reach the host."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

root = Path(os.environ['FIXTURE_ROOT'])
path = root / 'world.json'
world = json.loads(path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as out:
    out.write(json.dumps([name, *args]) + '\n')

def save():
    path.write_text(json.dumps(world))

def fail():
    with (root / 'unexpected.jsonl').open('a') as out:
        out.write(json.dumps([name, *args]) + '\n')
    sys.exit(97)

def finish(value=None):
    if value is not None:
        print(value)
    sys.exit(0)

if name == 'sleep':
    finish()
if name == 'claude':
    # A real subprocess is executed by the fake terminal, but no real provider runs.
    finish('FIXTURE_PROVIDER_READY')
if name == 'codex':
    if not args or args[0] != 'exec' or '--json' not in args or '--approve-for-me' not in args or '--sandbox' in args:
        fail()
    if not sys.stdin.read().strip():
        fail()
    print(json.dumps({'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'}))
    print(json.dumps({'type':'turn.started'}))
    finish(json.dumps({'type':'turn.completed'}))
if name == 'ps':
    if args != ['-eo', 'pid=,ppid=,comm=']:
        fail()
    finish('\n'.join(f"{s['pid']} 1 bash" + (f"\n{s['pid']+1} {s['pid']} claude" if s['command']=='claude' else '') for s in world['sessions'].values()))
if name == 'git':
    if args[:1] != ['-C'] or not Path(args[1]).is_relative_to(root):
        fail()
    operation = args[2:]
    if operation == ['remote', 'get-url', 'origin']:
        finish('git@github.com:o/fixture.git')
    if operation == ['rev-parse', '--show-toplevel']:
        finish(args[1])
    if operation == ['status', '--porcelain=v1', '--untracked-files=all']:
        finish(' dirty' if world.get('dirty') else '')
    if operation in (['rev-parse', 'HEAD'], ['rev-parse', 'origin/main']):
        finish('a' * 40)
    if operation == ['symbolic-ref', '--short', 'HEAD']:
        finish('main')
    fail()
if name == 'gh':
    issue = dict(number=1, title='Fixture work', body='', labels=[{'name':'ready' if world['phase']=='ready' else 'wip'}, {'name':'p2'}], state='open')
    items = [issue]
    if world['phase'] in ('published','replacement'):
        items.append(dict(number=2, title='Published change', body='Closes #1', labels=[{'name':'ready-for-review'}], pull_request={}, user={'login':'fixture-bot'}, state='open'))
    if world['phase'] == 'replacement':
        items.append(dict(number=3, title='Next work', body='', labels=[{'name':'ready'}, {'name':'p2'}], state='open'))
    if args[:3] == ['api', '--paginate', '--slurp']:
        endpoint = args[3]
        if endpoint == 'repos/o/fixture/labels?per_page=100':
            finish(json.dumps([[{'name':'auto:owned-by-fixture'}]]))
        if endpoint == 'repos/o/fixture/issues?state=open&per_page=100':
            finish(json.dumps([items]))
        if endpoint in [f'repos/o/fixture/issues/{n}/dependencies/blocked_by?per_page=100' for n in (1,3)]:
            finish('[[]]')
    if args[:3] == ['pr', 'view', '2']:
        finish(json.dumps(dict(state='OPEN', isDraft=False, headRefOid='a'*40, baseRefOid='b'*40,
            author={'login':'fixture-bot'}, labels=[{'name':'ready-for-review'}],
            closingIssuesReferences=[{'number':1,'repository':{'nameWithOwner':'o/fixture'}}])))
    fail()
if name == 'tmux':
    action = args[0]
    sessions = world['sessions']
    def target():
        value = args[args.index('-t')+1].lstrip('=')
        for key, session in sessions.items():
            if value.split(':')[0] == key or value in (session['id'],session['pane']):
                return key, session
        sys.exit(1)
    def formatted(session, key, fmt):
        values = dict(session_name=key, session_id=session['id'], session_created=session['created'],
            pane_id=session['pane'], pane_pid=session['pid'], pane_current_command=session['command'],
            pane_dead=0, pane_current_path=session['cwd'])
        for field, value in values.items():
            fmt = fmt.replace('#{' + field + '}', str(value))
        if '#{' in fmt:
            fail()
        return fmt
    if action in ('ls','list-sessions'):
        if not sessions:
            print('no sessions',file=sys.stderr)
            sys.exit(1)
        finish('\n'.join(formatted(s,key,args[-1]) for key,s in sessions.items()))
    if action == 'has-session':
        target(); finish()
    if action == 'new-session':
        key=args[args.index('-s')+1]
        if key in sessions:
            sys.exit(1)
        world['next_id'] += 1
        number=world['next_id']
        sessions[key]=dict(id=f'${number}',pane=f'%{number}',pid=int(os.environ['FIXTURE_PANE_PID']),created=int(time.time()),
            command='bash', state='shell',cwd=args[args.index('-c')+1],input='')
        save(); finish()
    key, session = target()
    if action in ('display-message','list-panes'):
        finish(formatted(session,key,args[-1]))
    if action == 'capture-pane':
        finish('Working… esc to interrupt' if session['state']=='busy' else 'fixture@host:~$' if session['command']=='bash' else '❯\n ⏵⏵ bypass permissions on')
    if action == 'send-keys':
        if '-l' in args:
            session['input']=args[-1]
        elif args[-1]=='Enter':
            if session['command']=='bash':
                command=shlex.split(session['input'])
                if command[:1]==['env']:
                    if len(command)<6 or not command[1].startswith('PYTHONPATH='+str(root)) or command[3:6]!=['-m','forge.codex_worker','run']:
                        fail()
                    # The actual runner validates ancestry through this fixture process
                    # to the test-owned parent recorded as the virtual pane PID.
                    result=subprocess.run(command,capture_output=True,text=True,timeout=10)
                    if result.returncode:
                        print(result.stderr+result.stdout,file=sys.stderr);sys.exit(result.returncode)
                    session.update(state='idle',input='')
                    world['dispatches'].append(dict(session=key,generation=session['id'],provider='codex'))
                    save();finish()
                executable=command[0]
                if executable != str(root/'mockbin/claude'):
                    fail()
                result=subprocess.run([executable,'--dangerously-skip-permissions'],capture_output=True,text=True,check=True)
                if result.stdout.strip()!='FIXTURE_PROVIDER_READY':
                    fail()
                session.update(command='claude',state='idle',input='')
            else:
                if not session['input']:
                    fail()
                session.update(state='busy',input='')
                world['dispatches'].append(dict(session=key,generation=session['id']))
        else:
            fail()
        save(); finish()
    if action == 'kill-session':
        if args[-1] != session['id']:
            fail()
        world['released'].append(session['id'])
        del sessions[key]
        save(); finish()
fail()
