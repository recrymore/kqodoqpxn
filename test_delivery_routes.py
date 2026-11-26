import unittest, tempfile, threading, json
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch
import app

class DeliveryRouteTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        p=patch.object(app,'DB',Path(tmp.name)/'test.sqlite');p.start();self.addCleanup(p.stop)
        p=patch.object(app,'membership',return_value={'member':True});p.start();self.addCleanup(p.stop)
        self.items=[{'slug':'PlushPepe-123','name':'Plush Pepe','number':123,'url':'https://t.me/nft/PlushPepe-123','model':'Steel','backdrop':'Black','symbol':'Star'}]
    def link(self):
        with app.db() as c:c.execute('INSERT INTO delivery VALUES(?,?,?)',(123,-100321,'Test group'))
    def test_app_without_group_never_sends(self):
        with patch.object(app,'send') as send:
            self.assertEqual(app.deliver_gifts(123,self.items,'random',origin='app'),{'kind':'app','fallback':False})
            send.assert_not_called()
    def test_bot_without_group(self):
        with patch.object(app,'send') as send:
            self.assertEqual(app.deliver_gifts(123,self.items,'random')['kind'],'private')
            send.assert_called_once();self.assertEqual(send.call_args.args[0],123)
    def test_both_origins_group_only(self):
        self.link()
        for origin in ('app','bot'):
            with patch.object(app,'group_allowed',return_value=True),patch.object(app,'send') as send:
                self.assertEqual(app.deliver_gifts(123,self.items,'random',origin=origin)['kind'],'group')
                send.assert_called_once();self.assertEqual(send.call_args.args[0],-100321)
    def test_group_permissions_failure_app_no_private(self):
        self.link()
        with patch.object(app,'group_allowed',return_value=False),patch.object(app,'send') as send:
            self.assertEqual(app.deliver_gifts(123,self.items,'random',origin='app'),{'kind':'app','fallback':True})
            send.assert_not_called()
    def test_partial_group_failure_app_no_private(self):
        self.link()
        with patch.object(app,'group_allowed',return_value=True),patch.object(app,'send',side_effect=[None,RuntimeError('failed')]) as send:
            self.assertEqual(app.deliver_gifts(123,self.items*20,'random',origin='app'),{'kind':'app','fallback':True,'group_sent':10})
            self.assertEqual([c.args[0] for c in send.call_args_list],[-100321,-100321])
    def test_http_search_routes(self):
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        self.addCleanup(t.join);self.addCleanup(srv.server_close);self.addCleanup(srv.shutdown)
        def call(path,body):
            req=Request(f'http://127.0.0.1:{srv.server_port}'+path,data=json.dumps({'initData':'test',**body}).encode(),headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=3) as r:return json.load(r)
        with patch.object(app,'verify_init',return_value=123),patch.object(app,'search_gifts',return_value=self.items),patch.object(app,'send') as send:
            for path,body in [('/api/random',{}),('/api/random',{'filters':{'model':'Steel'}}),('/api/prices',{'mode':'low'})]:
                r=call(path,body);self.assertEqual(r['delivery']['kind'],'app');self.assertEqual(r['gifts'],self.items)
            send.assert_not_called()
            self.link()
            with patch.object(app,'group_allowed',return_value=True):
                r=call('/api/random',{});self.assertEqual(r['delivery']['kind'],'group');self.assertEqual(r['gifts'],self.items)
                send.assert_called_once();self.assertEqual(send.call_args.args[0],-100321)
    def test_unsubscribed(self):
        with patch.object(app,'membership',return_value={'member':False}),patch.object(app,'send') as send:
            self.assertEqual(app.deliver_gifts(123,self.items,'random',origin='app')['kind'],'blocked');send.assert_not_called()
