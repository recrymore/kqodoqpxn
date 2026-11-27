import json, threading, unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import app,see_api
from gifts import GiftError
from test_see_api import raw
import test_app

class SpreadTests(unittest.TestCase):
    def test_number_bands_and_no_owner_data(self):
        client=see_api.Client()
        def request(path,params):
            self.assertEqual(path,'gifts')
            self.assertEqual(params['sort'],'number')
            self.assertEqual(params['order'],'asc')
            self.assertTrue(1<=params['number_from']<=params['number_to'])
            n={999:184,99999:5728,1000000:926481}[params['number_to']]
            return {'items':[raw(n)]}
        with patch.object(see_api.random,'randint',side_effect=lambda a,b:a),patch.object(client,'_free') as quota,patch.object(client,'_request',side_effect=request) as req:
            result=client.search_spread(3)
            self.assertEqual({g['number'] for g in result},{184,5728,926481})
            self.assertEqual(req.call_count,3);self.assertEqual(quota.call_count,3)
            self.assertNotIn('example_owner',str(result));self.assertNotIn('example_owner',str(client.cache))
            self.assertEqual(client.search_spread(3,exclude={g['slug'] for g in result}),[])
    def test_partial_and_stop_on_quota(self):
        c=see_api.Client()
        with patch.object(see_api.random,'shuffle'),patch.object(see_api.random,'randint',side_effect=lambda a,b:a),patch.object(c,'_free',side_effect=[None,GiftError('quota')]),patch.object(c,'_request',return_value={'items':[raw(184)]}) as req:
            result=c.search_spread(3)
            self.assertEqual([g['number'] for g in result],[184]);req.assert_called_once()
    def test_batch_buttons_are_gift_only(self):
        item=see_api.normalize(raw())
        _,rows=app.gift_pack([item],'random')
        self.assertEqual(rows[0][0][1],item['url'])
        self.assertNotIn('example_owner',str(rows))

class IndividualTests(unittest.TestCase):
    setUp=test_app.Tests.setUp
    signed=test_app.Tests.signed
    def test_private_endpoint_boundary(self):
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        self.addCleanup(srv.server_close);self.addCleanup(t.join,2);self.addCleanup(srv.shutdown)
        def call(uid):return urlopen(Request(f'http://127.0.0.1:{srv.server_port}/api/gift',data=json.dumps({'initData':self.signed(uid),'ref':'PlushPepe-1'}).encode(),headers={'Content-Type':'application/json'}),timeout=3)
        item={**see_api.normalize(raw()),'owner_status':'available','public_username':'@example_owner'}
        with patch.object(see_api,'enabled',return_value=True),patch.object(see_api.client,'single',return_value=item) as single,patch.object(app,'membership',return_value={'member':True}) as membership:
            with patch.object(app,'fetch_card',return_value=see_api.normalize(raw())):
                with call(123) as r:self.assertNotIn('public_username',json.load(r)['gift'])
            single.assert_not_called()
            membership.return_value={'member':False}
            with self.assertRaises(HTTPError) as e:call(99)
            self.assertEqual(e.exception.code,403);single.assert_not_called()
            membership.return_value={'member':True}
            with call(99) as r:self.assertEqual(json.load(r)['gift']['owner_display'],'@example_owner')
            single.assert_called_once_with('PlushPepe-1')
    def test_individual_bot_links_public_only(self):
        def run(item):
            with patch.object(see_api,'enabled',return_value=True),patch.object(see_api.client,'single',return_value=item),patch.object(app,'membership',return_value={'member':True}),patch.object(app,'send') as send:
                app.handle({'chat':{'type':'private','id':99},'from':{'id':99},'text':'/gift PlushPepe-1'})
                return send.call_args.args[2]['inline_keyboard'][0]
        base=see_api.normalize(raw())
        buttons=run({**base,'owner_status':'available','public_username':'@example_owner'})
        self.assertEqual([b['url'] for b in buttons],['https://t.me/nft/PlushPepe-1','https://t.me/example_owner'])
        with patch.object(app.usage,'consume'):
            buttons=run({**base,'owner_status':'hidden'})
        self.assertNotIn('example_owner',str(buttons))
