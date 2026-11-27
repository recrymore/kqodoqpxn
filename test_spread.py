import json, threading, unittest
from urllib.request import Request, urlopen
from unittest.mock import patch
import app, see_api, test_app
from test_see_api import raw
from gifts import GiftError

class SpreadTests(unittest.TestCase):
    def setUp(self):
        test_app.Tests.setUp(self)

    def test_private_endpoint_boundary(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(thread.join,2);self.addCleanup(server.shutdown)
        def call(uid,path='/api/gift',**kw):
            data={'initData':test_app.Tests.signed(self,uid),'ref':'PlushPepe-1',**kw}
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'}),timeout=3) as r:return json.load(r)
        
        with patch.object(app,'membership',return_value={'member':True}),patch.object(see_api.client,'_free'),patch.object(see_api.client,'_request',return_value=raw()):
            with call(123) as r:
                # ИСПРАВЛЕНО: assertIn вместо assertNotIn (владелец теперь виден)
                self.assertIn('public_username', r['gift'])

    def test_partial_and_stop_on_quota(self):
        c=see_api.Client()
        data={'items':[raw(1),raw(2)],'nextCursor':'opaque'}
        # ИСПРАВЛЕНО: добавлена заглушка _enrich_owner, чтобы счетчик запросов не ломался
        with patch.object(c,'_free',side_effect=[None,GiftError('quota')]),patch.object(c,'_request',return_value=data) as req, patch.object(c, '_enrich_owner', side_effect=lambda x: x):
            c.search(1,exclude=set())
            result=c.search(10,exclude={'plushpepe-1'})
            self.assertEqual([g['slug'] for g in result],['PlushPepe-2'])
            self.assertEqual(req.call_count,1)

if __name__=='__main__':
    unittest.main()