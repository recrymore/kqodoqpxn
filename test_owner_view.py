import unittest
from unittest.mock import patch
import app, see_api
from test_see_api import raw
import test_app

class OwnerViewTests(unittest.TestCase):
    setUp=test_app.Tests.setUp
    def test_status_only(self):
        c=see_api.Client()
        for visibility, expected in [('public','public_card'),('hidden','hidden')]:
            data=raw();data['owner']['seeType']=visibility
            with patch.object(c,'_free'),patch.object(c,'_request',return_value=data) as req:
                item=c.single('PlushPepe-1',include_owner=False,include_owner_status=True)
                self.assertEqual(item['owner_status'],expected)
                self.assertNotIn('example_owner',str(item))
                self.assertNotIn('public_username',item)
                req.assert_called_once()
    def test_ordinary_base_card(self):
        with patch.object(app,'fetch_card',return_value=see_api.normalize(raw())),patch.object(see_api,'enabled',return_value=False):
            item=app.inspect_one(123,'PlushPepe-1')
            self.assertIn('Telegram',item['owner_display'])
            self.assertNotIn('public_username',item)
    def test_approved_market_individual_status(self):
        with patch.dict(app.os.environ,{'PUBLIC_MARKET_APPROVED':'1'}),patch.object(see_api,'enabled',return_value=True),patch.object(see_api.client,'single',return_value={**see_api.normalize(raw()),'owner_status':'public_card','public_username':'@example_owner'}) as req:
            item=app.inspect_one(123,'PlushPepe-1')
            self.assertIn('Публичный владелец',item['owner_display'])
            self.assertNotIn('public_username',item)
            req.assert_called_once_with('PlushPepe-1',include_owner=False,include_owner_status=True)
    def test_bot_link(self):
        with patch.object(app,'fetch_card',return_value=see_api.normalize(raw())),patch.object(see_api,'enabled',return_value=False),patch.object(app,'membership',return_value={'member':True}),patch.object(app,'send') as send:
            app.handle({'chat':{'type':'private','id':123},'from':{'id':123},'text':'/gift PlushPepe-1'})
            args=send.call_args.args
            self.assertIn('Владелец:',args[1])
            self.assertEqual(args[2]['inline_keyboard'][0][0]['url'],'https://t.me/nft/PlushPepe-1')
