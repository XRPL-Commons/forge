"""Agent delivery reads keep the existing repository authorization boundary."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.client_connections import ConnectionError
from forge.client_repositories import ClientRepositories
from forge_client.cli import parser, repository_commands
from forge.store.models import DeliveryPlanChanged


class DeliveryReadTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(ClientRepositories)
        self.service.settings = SimpleNamespace(root='/synthetic')
        self.service.clients = Mock()
        self.value = dict(repository={'id':1,'name':'example/repo'}, instance='instance', generation=1,
                          section='delivery', number=12, page=1, after='cursor')

    def test_scoped_read_rechecks_authorization_and_uses_issue_identity(self):
        projection = Mock(); projection.delivery.return_value = {'repo_id':1,'unit_id':'batch','jobs':[]}
        with patch('forge.client_repositories.describe',return_value={}), patch('forge.factory.control.public_views',return_value=projection):
            self.assertEqual(self.service.read('private-token',self.value)['unit_id'],'batch')
        projection.delivery.assert_called_once_with('1:12',after='cursor',task_after='')
        self.assertEqual(self.service.clients.authorize.call_count,2)
        self.assertTrue(all(call.args[2]=='repositories:read' for call in self.service.clients.authorize.call_args_list))

    def test_denied_repository_is_never_read(self):
        self.service.clients.authorize.side_effect = ConnectionError('forbidden','Denied',403)
        with patch('forge.factory.control.public_views') as projection:
            with self.assertRaises(ConnectionError): self.service.read('private-token',self.value)
            projection.assert_not_called()

    def test_cli_delivery_uses_existing_repository_read_namespace(self):
        args = parser().parse_args(['repo','delivery','example/repo','12','--server','https://forge.example','--after','job-cursor','--task-after','task-cursor'])
        self.assertEqual((args.repo_command,args.number,args.after,args.task_after),('delivery',12,'job-cursor','task-cursor'))
        self.assertEqual(repository_commands()['delivery']['response_version'],2)
        self.assertEqual(repository_commands()['delivery']['capability'],'repositories:read')

    def test_task_page_cursor_is_forwarded_without_changing_job_cursor(self):
        self.value['task_after'] = 'task-cursor'
        projection = Mock(); projection.delivery.return_value = {'repo_id': 1}
        with patch('forge.client_repositories.describe', return_value={}), patch('forge.factory.control.public_views', return_value=projection):
            self.service.read('private-token', self.value)
        projection.delivery.assert_called_once_with('1:12', after='cursor', task_after='task-cursor')
        self.assertEqual(self.service.clients.authorize.call_count, 2)

    def test_replaced_plan_returns_a_restartable_conflict(self):
        self.value['task_after'] = 'old-plan-cursor'
        projection = Mock(); projection.delivery.side_effect = DeliveryPlanChanged('delivery plan changed')
        with patch('forge.client_repositories.describe', return_value={}), patch('forge.factory.control.public_views', return_value=projection):
            with self.assertRaises(ConnectionError) as raised:
                self.service.read('private-token', self.value)
        self.assertEqual(raised.exception.code, 'delivery_plan_changed')
        self.assertEqual(raised.exception.status, 409)
