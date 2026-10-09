"""Browser and explicitly delegated CLI decisions for approved deliveries."""
from .client_connections import ConnectionError, fields, need
from .factory.store_operator import DELIVERY_ACTIONS, selected_service
from .store.queries import Queries
from .store.models import TransitionError, VersionConflict, IdempotencyConflict


def request(service,value):
    need(isinstance(value,dict))
    if value.get('action')=='inspect':
        fields(value,{'key','action'})
        return service.membership(value['key'])
    fields(value,{'key','action','revision','reason','membership','request_id'})
    need(value['action'] in DELIVERY_ACTIONS)
    identifier=service.request(value['key'],value['action'],value['revision'],value['reason'],
                               request_id=value['request_id'],membership=value['membership'])
    return {'request':identifier,'status':'pending','message':'Waiting for verified batch cleanup before applying the decision.'}


class ClientDelivery:
    def __init__(self,root,clients):
        self.root,self.clients=root,clients

    def request(self,token,value):
        need(isinstance(value,dict))
        capability='repositories:read' if value.get('action')=='inspect' else 'repositories:triage'
        common={'repository','instance','generation'}
        fields(value,common|({'key','action'} if value.get('action')=='inspect' else {'key','action','revision','reason','membership','request_id'}))
        grant=self.clients.authorize(token,value['repository'],capability,value['instance'],value['generation'])
        service=selected_service(self.root,actor_id=f"github:{grant['operator']['id']}:client:{grant['id']}")
        need(service is not None,'setup_required','Select the SQLite runtime before changing deliveries.',409)
        with self.clients.effect_guard(token,value['instance'],value['generation']):
            with service.store.read_snapshot() as db:
                issue=Queries._issue(db,value['key'])
                need(issue['repository_id']==value['repository']['id'] and issue['repository'].lower()==value['repository']['name'].lower(),
                     'scope_changed','Issue does not belong to the authorized repository.',403)
            from .mutation_audit import current
            if current():current().bind(grant=grant)
            try:
                return request(service,{key:item for key,item in value.items() if key not in common})
            except TransitionError as error:
                code='revision_changed' if isinstance(error,VersionConflict) else 'operation_conflict' if isinstance(error,IdempotencyConflict) else 'decision_unavailable'
                raise ConnectionError(code,str(error),409) from error
