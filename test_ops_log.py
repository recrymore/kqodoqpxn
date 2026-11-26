import tempfile, unittest, os
from pathlib import Path
from unittest.mock import patch
import app, ops_log

class OpsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        p=patch.multiple(app, DB=Path(temp.name)/'db.sqlite3', ADMIN_IDS={99}, ops_events=ops_log.Events()); p.start(); self.addCleanup(p.stop)
        p=patch.dict(os.environ, {'LOG_CHAT_ID':''}); p.start(); self.addCleanup(p.stop)
    def msg(self,uid,cmd,group=True):
        return {'chat':{'id':-123 if group else uid,'type':'supergroup' if group else 'private'},'from':{'id':uid},'text':cmd}
    def test_only_admin_can_bind(self):
        with patch.object(app,'logs_allowed',return_value=True),patch.object(app,'send'):
            app.handle(self.msg(123,'/logs_here')); self.assertIsNone(app.logs_destination())
            app.handle(self.msg(99,'/logs_here')); self.assertEqual(app.logs_destination(),-123)
            app.handle(self.msg(123,'/logs_off',False)); self.assertEqual(app.logs_destination(),-123)
            app.handle(self.msg(99,'/logs_off',False)); self.assertIsNone(app.logs_destination())
    def test_private_owned_group(self):
        def api(method,**kw):
            if method=='getChat':return {'type':'supergroup'}
            if method=='getMe':return {'id':100}
            return {'status':'creator' if kw['user_id']==99 else 'administrator'}
        with patch.object(app,'api',side_effect=api):self.assertTrue(app.logs_allowed(-123))
        with patch.object(app,'api',return_value={'type':'supergroup','username':'public'}):self.assertFalse(app.logs_allowed(-123))
    def test_no_sensitive_fields(self):
        app.set_logs_destination(-123)
        app.log_event('search',requested=5,found=3)
        self.assertIn('Запрошено: 5 · Найдено: 3',app.ops_events.queue.get_nowait())
        app.log_event('search',requested=5,found=3,token='secret')
        self.assertTrue(app.ops_events.queue.empty())
        app.log_event('api_error',status=429);app.log_event('api_error',status=429)
        self.assertEqual(app.ops_events.queue.qsize(),1)
    def test_secret_destination_override(self):
        with patch.dict(os.environ,{'LOG_CHAT_ID':'-123'}):
            self.assertEqual(app.logs_destination(),-123)
            app.set_logs_destination(None);self.assertIsNone(app.logs_destination())
    def test_test_message(self):
        app.set_logs_destination(-123)
        with patch.object(app,'logs_allowed',return_value=True),patch.object(app,'send') as send:
            app.handle(self.msg(99,'/logs_test',False));self.assertEqual(send.call_args_list[0].args[0],-123)
