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
    def test_nested_card(self):
        data={'estimate':{'ton':15},'gift':{'details':{'slug':'PlushPepe','num':1,'title':'Plush Pepe','model':'Steel','backdrop':'Black','pattern':'Star'}}}
        item=see_api.normalize(data)
        self.assertEqual(item['slug'],'PlushPepe-1')
        self.assertEqual(item['model'],'Steel')
        self.assertEqual(item['estimate_ton'],15)
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertEqual(self.c.single('PlushPepe-1')['number'],1)
            with self.assertRaises(GiftError):self.c.single('PlushPepe-2')
    def test_nested_flat_card(self):
        self.assertEqual(see_api.normalize({'gift':raw()})['slug'],'PlushPepe-1')
        self.assertIsNone(see_api.normalize({'slug':'PlushPepe-2','gift':raw()}))
        self.assertIsNone(see_api.normalize({'estimate':{'ton':15}}))
    def test_nested_diagnostics(self):
        shape=see_api.card_shape({'gift':{'details':{'slug':'PlushPepe','num':1},'owner':{'username':'private_owner'}}})
        self.assertEqual(shape['gift.details.slug_format'],'collection')
        self.assertNotIn('private_owner',str(shape))
    def test_split_slug(self):
        data={'details':{'slug':'PlushPepe','num':1,'title':'Plush Pepe','model':'Steel','backdrop':'Black','pattern':'Star'},'estimate':{'ton':15}}
        item=see_api.normalize(data)
        self.assertEqual(item['slug'],'PlushPepe-1')
        self.assertEqual(item['url'],'https://t.me/nft/PlushPepe-1')
        self.assertEqual(item['estimate_ton'],15)
        self.assertEqual(see_api.normalize({'slug':'JellyBunny','num':'25'})['number'],25)
        self.assertIsNone(see_api.normalize({'slug':'PlushPepe','num':True}))
        self.assertIsNone(see_api.normalize({'slug':'PlushPepe-1','num':2}))
        self.assertIsNone(see_api.normalize({'slug':'PlushPepe'}))
    def test_single_split_slug_and_mismatch(self):
        data={'details':{'slug':'PlushPepe','num':1,'title':'Plush Pepe'}}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertEqual(self.c.single('PlushPepe-1')['slug'],'PlushPepe-1')
            with self.assertRaises(GiftError):self.c.single('PlushPepe-2')
    def test_schema_logs_without_values(self):
        data={'details':{'slug':'private-value','num':'invalid'},'owner':{'username':'private_owner'}}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            with self.assertLogs('see',level='WARNING') as logs:
                with self.assertRaises(GiftError):self.c.single('PlushPepe-1')
            output=str(logs.output)
            self.assertIn('details.slug_format',output)
            self.assertNotIn('private-value',output)
            self.assertNotIn('private_owner',output)
    def test_split_slug_batch(self):
        data={'items':[{'slug':'PlushPepe','num':1},{'slug':'PlushPepe','num':2}]}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertEqual({x['slug'] for x in self.c.search(10)},{'PlushPepe-1','PlushPepe-2'})
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
    def test_nested_public_owner_single_only(self):
        data={'gift':raw(),'estimate':{'ton':15}}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertEqual(self.c.single('PlushPepe-1')['public_username'],'@example_owner')
        self.assertNotIn('public_username',see_api.normalize(data))
        self.assertFalse(self.c.cache)
    def test_nested_hidden_and_conflicting_owner(self):
        data={'gift':raw()}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            data['owner']={'seeType':'hidden'}
            self.assertNotIn('public_username',self.c.single('PlushPepe-1'))
            data.pop('owner');data['gift']['owner']['seeType']='hidden'
            self.assertNotIn('public_username',self.c.single('PlushPepe-1'))
    def test_nested_missing_username(self):
        data={'gift':raw()};data['gift']['owner'].pop('username')
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data):
            self.assertNotIn('public_username',self.c.single('PlushPepe-1'))
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
