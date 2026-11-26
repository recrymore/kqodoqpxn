import unittest, os, json, io
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
import see_api, app
from gifts import GiftError

def raw(num=1):
    return {'slug':f'PlushPepe-{num}','title':'Plush Pepe','model':{'name':'Steel'},'backdrop':'Black','pattern':'Star','estimate':{'ton':12.5,'confidence':'low'},'owner':{'seeType':'public','telegramType':'user','username':'example_owner','telegramId':'1234'},'onSale':True}

class SeeTests(unittest.TestCase):
    def setUp(self): self.c=see_api.Client()
    def test_projection(self):
        item=see_api.normalize(raw());self.assertEqual(item['estimate_ton'],12.5);self.assertEqual(item['model'],'Steel');self.assertNotIn('owner',item);self.assertNotIn('example_owner',str(item))
    def test_details(self):
        item=see_api.normalize({'details':raw(),'estimate':{'ton':15}});self.assertEqual(item['slug'],'PlushPepe-1');self.assertEqual(item['estimate_ton'],15)
    def test_cache_and_batch(self):
        data={'items':[raw(i) for i in range(1,61)]+[raw(1)]}
        with patch.object(self.c,'_free') as quota,patch.object(self.c,'_request',return_value=data) as request:
            self.assertEqual(len(self.c.search(50)),50);self.assertEqual(len(self.c.search(25)),25)
            quota.assert_called_once();request.assert_called_once();self.assertNotIn('example_owner',str(self.c.cache))
    def test_filters(self):
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value={'items':[]}) as request:
            self.c.search(25,{'symbol':'Star'},'mid');params=request.call_args.args[1];self.assertEqual(params['pattern'],'Star');self.assertEqual(params['price_from'],5_000_000_000);self.assertEqual(params['price_to'],19_999_999_999)
        with self.assertRaises(ValueError):self.c.search(25,{'owner':'someone'})
    def test_quota(self):
        with patch.object(self.c,'_request') as request:
            self.c.remaining=0
            with self.assertRaises(GiftError):self.c.search(10)
            request.assert_called_once_with('ping')
    def test_single(self):
        data=raw()
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertEqual(self.c.single('PlushPepe-1')['public_username'],'@example_owner')
            data['owner']['seeType']='hidden';self.assertNotIn('public_username',self.c.single('PlushPepe-1'))
        self.assertFalse(self.c.cache)
    def test_admin_boundary(self):
        with patch.object(app,'ADMIN_IDS',{99}),patch.object(see_api,'enabled',return_value=True),patch.object(see_api.client,'search',return_value=[]) as api,patch.object(app,'random_gifts',return_value=[]) as old:
            app.search_gifts(123,10);api.assert_not_called();old.assert_called_once_with(10)
            app.search_gifts(99,50);api.assert_called_once_with(50,None,None)
    def test_rate_limit(self):
        with patch.dict(os.environ,{'SEE_API_KEY':'test-secret'}),patch.object(self.c.opener,'open',side_effect=HTTPError('https://kartoshka.free/v1/ping',429,'limit',{'Retry-After':'70'},io.BytesIO())) as op:
            with self.assertRaises(GiftError):self.c.quota()
            with self.assertRaises(GiftError):self.c.quota()
            op.assert_called_once()
    def test_chunk_delivery(self):
        with patch.object(app,'membership',return_value={'member':True}),patch.object(app,'destination',return_value={'kind':'private'}),patch.object(app,'send') as send:
            result=app.deliver_gifts(123,[see_api.normalize(raw(i)) for i in range(1,51)],'random');self.assertEqual(result['kind'],'private');self.assertEqual(send.call_count,5)
            for call in send.call_args_list:self.assertLess(len(call.args[1]),4096)
if __name__=='__main__':unittest.main()
