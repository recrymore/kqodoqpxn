import json, hmac, hashlib, time, tempfile, threading, unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import app, partners

class AdminAppTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.multiple(app,TOKEN='test-token',ADMIN_IDS={99},URL='https://example.test',CONFIG=Path(tmp.name)/'config.json',DB=Path(tmp.name)/'app.sqlite3');p.start();self.addCleanup(p.stop)
        p=patch.object(partners,'PATH',Path(tmp.name)/'partners.sqlite3');p.start();self.addCleanup(p.stop)
        p=patch.dict(app.os.environ,{'LOG_CHAT_ID':''});p.start();self.addCleanup(p.stop)
    def signed(self,uid):
        data={'auth_date':str(int(time.time())),'user':json.dumps({'id':uid})}
        secret=hmac.new(b'WebAppData',app.TOKEN.encode(),hashlib.sha256).digest()
        data['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(),hashlib.sha256).hexdigest()
        return urlencode(data)
    def test_http_permissions(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def call(raw,action='overview',**kwargs):
            return urlopen(Request(base+'/api/admin',data=json.dumps({'initData':raw,'action':action,'admin':True,'uid':99,**kwargs}).encode(),headers={'Content-Type':'application/json'}),timeout=3)
        try:
            for raw,code in [(self.signed(123),403),('',401),(self.signed(123).replace('123','99'),401)]:
                with self.assertRaises(HTTPError) as cm:call(raw,'save',config={'title':'Unauthorized'})
                self.assertEqual(cm.exception.code,code)
            with call(self.signed(99)) as r: self.assertIn('counts',json.load(r))
            with urlopen(base+'/admin') as r:self.assertIn(b'GIFT RADAR / CONTROL',r.read())
            self.assertEqual(app.read_config()['title'],app.DEFAULT['title'])
        finally:server.shutdown();server.server_close();t.join()
    def test_direct_guard(self):
        with self.assertRaises(PermissionError):app.admin_action(123,{'action':'overview'})
    def test_settings_validation(self):
        before=app.read_config()
        with self.assertRaises(ValueError):app.admin_action(99,{'action':'save','config':{'title':'New','banner_enabled':'false'}})
        self.assertEqual(app.read_config(),before)
        self.assertEqual(app.admin_action(99,{'action':'save','config':{'title':'New','banner_enabled':False}})['config']['title'],'New')
        with self.assertRaises(ValueError):app.admin_action(99,{'action':'save','config':{'admin_id':123}})
    def test_partners(self):
        row=partners.submit(123,'ExampleGiftBot')
        r=app.admin_action(99,{'action':'partner_review','id':row['id'],'status':'approved'})
        self.assertEqual(r['request']['status'],'approved')
        self.assertIn('вручную',r['note'])
    def test_support(self):
        with patch.object(app,'send'):
            tid=app.new_ticket(123,'question','Help')
            rows=app.admin_action(99,{'action':'tickets'})['tickets']
            self.assertEqual(rows[0]['id'],tid);self.assertNotIn('uid',rows[0])
            self.assertTrue(app.admin_action(99,{'action':'reply','id':tid,'reply':'Answer'})['delivered'])
    def test_logs(self):
        with patch.object(app,'logs_check',return_value='Denied'):
            with self.assertRaises(ValueError):app.admin_action(99,{'action':'logs_bind','chat_id':'-123'})
        self.assertIsNone(app.logs_destination())
        with patch.object(app,'logs_check',return_value=None),patch.object(app,'send') as send:
            app.admin_action(99,{'action':'logs_bind','chat_id':'-123'});app.admin_action(99,{'action':'logs_test'})
            self.assertEqual(send.call_args.args[0],-123)
        app.admin_action(99,{'action':'logs_off'});self.assertIsNone(app.logs_destination())
    def test_admin_button(self):
        with patch.object(app,'send') as send:
            for uid in [123,99]:app.handle({'chat':{'type':'private','id':uid},'from':{'id':uid},'text':'/admin'})
            self.assertEqual(send.call_count,2)
            self.assertTrue(send.call_args_list[0].args[2]['remove_keyboard'])
            self.assertEqual(send.call_args.args[2]['inline_keyboard'][0][0]['web_app']['url'],'https://example.test/admin')
