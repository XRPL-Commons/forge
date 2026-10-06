"""Entity mappings for inventory, conversations, requests and delivery receipts.

Import evidence is never used as a runtime read model. Each collection member
gets its own row, and large discussion execution inputs are explicit artifacts.
"""
from ...factory.capacity import memory_bytes
from .normalize import batches, insert, finding
from .snapshot import MigrationError, encode, digest

DISCUSSIONS = 'state/issues/discussions.json'
ACTIONS = 'state/issues/actions.json'
INVENTORY = 'state/issues/inventory.json'


def json_text(value):
    return encode(value).decode() if value is not None else None


def unknown_fields(db, importer, subject, value, known):
    if set(value) - set(known):
        finding(db, importer, subject, 'unmapped_entity_fields')


def normalize(importer, ledger, inventory):
    mapped = set()

    def provider(db, pair):
        account, value = pair
        job = ledger['jobs'].get(value.get('attempt'))
        if not job or not job.get('provider'):
            finding(db, importer, 'provider:'+account, 'provider_identity_missing')
            return
        insert(db, 'provider_cooldowns', account_ref=account, provider=job['provider'],
               reason_code=value['reason'], retry_at=value['until'],
               observed_at=job['created_at'], source_job_id=job['id'])
        unknown_fields(db, importer, 'provider:'+account, value, ('attempt','reason','until'))

    batches(importer, 'normalized:providers', sorted(ledger.get('provider_cooldowns', {}).items()), provider)

    def memory(db, pair):
        key, value = pair
        repo, role = key.split(':', 1)
        insert(db, 'memory_profiles', id='legacy:memory:'+key, repository_id=int(repo), role=role,
               revision=1, memory_bytes=memory_bytes(value['memory']), allocation=value['memory'],
               source_job_id=value.get('attempt'), observed_at=value['at'])
        unknown_fields(db, importer, 'memory:'+key, value, ('at','attempt','memory'))

    batches(importer, 'normalized:memory', sorted(ledger.get('memory_profiles', {}).items()), memory)
    batches(importer, 'normalized:discovery', sorted(ledger.get('discovery_observed', {}).items()), lambda db,pair:
            insert(db, 'repository_observations', id='legacy:discovery:'+pair[0], repository_id=int(pair[0]),
                   kind='discovery', collection_generation=1, fetched_at=pair[1], complete=1))
    if 'provider_cursor' in ledger:
        batches(importer, 'normalized:provider_cursor', [ledger['provider_cursor']], lambda db,value:
                insert(db, 'runtime_controls', name='provider_cursor', cursor=str(value), updated_at=0))

    def repository(db, pair):
        key, value = pair
        observation = 'legacy:inventory:'+key
        collaborators = value.get('collaborators') or {}
        details = {k:value[k] for k in ('scan','error') if k in value}
        details['collaborators_at'] = collaborators.get('at')
        insert(db, 'repository_observations', id=observation, repository_id=int(key), kind='inventory',
               controller_identity=value.get('identity'), collection_generation=1, fetched_at=value.get('at') or 0,
               complete=int(value.get('complete',False)), refreshing=int(value.get('refreshing',False)),
               error_code='legacy_inventory_error' if value.get('error') else None, details_json=json_text(details))
        for pull in value.get('pulls', []):
            insert(db, 'inventory_pull_summaries', repository_id=int(key), github_number=pull['number'],
                   title=pull.get('title',''), body=pull.get('body',''), author_login=pull.get('author',''),
                   branch=pull.get('branch',''), forge_owned=int(pull.get('forge_owned',False)), observation_id=observation)
            unknown_fields(db, importer, observation, pull, ('number','title','body','author','branch','forge_owned'))
        for person in collaborators.get('rows', []):
            insert(db, 'inventory_collaborators', repository_id=int(key), login=person['login'], source='collaborator',
                   observation_id=observation, details_json=json_text(person))
        for login in collaborators.get('authors', []):
            insert(db, 'inventory_collaborators', repository_id=int(key), login=login, source='recent_author',
                   observation_id=observation, details_json='{}')
        unknown_fields(db, importer, observation, collaborators, ('at','rows','authors'))
        unknown_fields(db, importer, observation, value,
                       ('name','issues','at','complete','identity','refreshing','error','scan','collaborators','pulls'))

    batches(importer, 'normalized:inventory_metadata', sorted(inventory.get('repositories', {}).items()), repository)

    def discussion(db, pair):
        key, thread = pair
        identifier = 'legacy:discussion:'+key
        issue = db.execute('SELECT repository_id FROM issues WHERE id=?', (key,)).fetchone()
        if not issue:
            finding(db, importer, key, 'discussion_issue_missing')
            return
        messages = thread.get('messages', [])
        at = min([m['at'] for m in messages] + [thread.get('requested_at') or 0])
        updated = max([m['at'] for m in messages] + [thread.get('requested_at') or 0])
        unit = db.execute('SELECT unit_id FROM unit_issues WHERE issue_id=? ORDER BY joined_at DESC LIMIT 1', (key,)).fetchone()
        insert(db, 'discussions', id=identifier, repository_id=issue['repository_id'], issue_id=key,
               unit_id=unit[0] if unit else None, title='', created_at=at, updated_at=updated,
               status=thread['status'], source_revision=thread.get('revision'), requested_at=thread.get('requested_at'),
               cancel_requested=int(thread.get('cancel_requested',False)), reason=thread.get('reason'),
               partial=thread.get('partial'), origin=json_text(thread.get('origin')), presence_json=json_text(thread.get('presence')))
        for message in messages:
            meta = {k:v for k,v in message.items() if k not in ('id','sequence','role','text','at')}
            insert(db, 'messages', id=message['id'], discussion_id=identifier, sequence=message['sequence'],
                   role={'user':'operator','assistant':'assistant','system':'system'}[message['role']],
                   author_id='legacy-operator' if message['role']=='user' else 'legacy-assistant',
                   content=message['text'], created_at=message['at'], metadata_json=json_text(meta))
        job = thread.get('job')
        if job:
            if not job.get('cleanup'):
                raise MigrationError('uncleaned_discussion')
            # Some historical jobs never recorded created_at. Keep that absence
            # explicit; the operational timestamp is the thread's first record.
            insert(db, 'discussion_executions', id=job['id'], discussion_id=identifier, phase='cleaned',
                   fenced=1, cleanup=1, created_at=job.get('created_at',at),
                   source_created_at=job.get('created_at'), completion_at=job.get('completed'))
            raw = encode(job)
            for sequence, start in enumerate(range(0,len(raw),1048576)):
                part = raw[start:start+1048576]
                insert(db, 'discussion_artifacts', execution_id=job['id'], kind='launch_input', sequence=sequence,
                       media_type='application/json', digest=digest(part), content=part, size=len(part), created_at=at)
        unknown_fields(db, importer, identifier, thread,
                       ('cancel_requested','job','messages','origin','partial','presence','reason','requested_at','revision','status'))

    batches(importer, 'normalized:discussions', sorted(importer.source.json(DISCUSSIONS, {}).items()), discussion)
    if DISCUSSIONS in importer.source.entries:
        mapped.add(DISCUSSIONS)

    def action(db, pair):
        identifier, value = pair
        insert(db, 'issue_action_requests', id=identifier, issue_id=value['key'], action=value['action'],
               expected_revision=value['revision'], status=value['status'], reason=value['reason'], message=value.get('message'),
               plan_json=json_text(value.get('plan')), environment_json=json_text(value.get('environment')), created_at=value['created_at'])
        unknown_fields(db, importer, 'action:'+identifier, value,
                       ('action','created_at','key','message','number','plan','reason','repo','repo_id','revision','status','environment'))
        issue = db.execute('SELECT i.github_number,i.repository_id,r.full_name FROM issues i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?', (value['key'],)).fetchone()
        if (value['number'],value['repo_id'],value['repo']) != tuple(issue):
            raise MigrationError('action_issue_identity_mismatch')

    batches(importer, 'normalized:actions', sorted(importer.source.json(ACTIONS, {}).items()), action)
    if ACTIONS in importer.source.entries:
        mapped.add(ACTIONS)

    def notification(db, name):
        value = importer.source.json(name)
        if name.rsplit('/',1)[-1] != value['id']+'.json':
            raise MigrationError('notification_identity_mismatch')
        for channel, receipt in value['channels'].items():
            status = receipt['status']
            identifier = value['id']+':'+channel
            insert(db, 'notification_outbox', id=identifier, enqueue_key=identifier, source_event_id=value['id'],
                   channel=channel, recipient_ref='environment:'+channel, payload_json=json_text({'message':value['message']}),
                   status={'pending':'pending','retry':'pending','failed':'failed','accepted_by_relay':'delivered','disabled':'delivered'}[status],
                   delivery_state=status, attempt_count=receipt['attempts'], retry_at=receipt['next_at'],
                   last_attempt_at=receipt.get('last_attempt_at'), created_at=value['created_at'])
            # The legacy store retains only the latest receipt, not every attempt.
            if receipt['attempts'] and receipt.get('last_attempt_at') is not None:
                insert(db, 'notification_deliveries', id=identifier+':'+str(receipt['attempts']), notification_id=identifier,
                       attempt_number=receipt['attempts'], status='confirmed' if status in ('accepted_by_relay','disabled') else 'failed',
                       receipt_ref=status, attempted_at=receipt['last_attempt_at'])
            unknown_fields(db, importer, name, receipt, ('status','attempts','next_at','last_attempt_at'))
        unknown_fields(db, importer, name, value, ('id','message','created_at','channels'))

    names = sorted(name for name in importer.source.entries if name.startswith('state/notifications/outbox/') and name.endswith('.json'))
    batches(importer, 'normalized:notifications', names, notification)
    mapped.update(names)
    return mapped
