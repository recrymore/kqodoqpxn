import unittest
from unittest.mock import patch
import app

class CreditTests(unittest.TestCase):
    def test_credit_add_and_keep_description(self):
        with patch.object(app,'api',side_effect=[{'description':'Gift Radar'},True]) as api:
            self.assertTrue(app.ensure_data_credit())
            api.assert_called_with('setMyDescription',description='Gift Radar\n\n'+app.DATA_CREDIT)
    def test_credit_existing(self):
        with patch.object(app,'api',return_value={'description':app.DATA_CREDIT}) as api:
            self.assertTrue(app.ensure_data_credit());api.assert_called_once_with('getMyDescription')
    def test_credit_fail(self):
        with patch.object(app,'api',return_value={'description':'x'*512}) as api:
            self.assertFalse(app.ensure_data_credit());api.assert_called_once()
    def test_single_and_batch(self):
        card={'source':'See.tg','name':'Gift','number':1,'model':'A','backdrop':'B','symbol':'C','url':'https://t.me/nft/Gift-1','estimate_ton':2}
        with patch.object(app,'credit_in_description',True):
            self.assertNotIn('https://see.tg',app.gift_pack([card],'random',single_card=True)[0])
            self.assertIn('Оценка не равна цене продажи',app.gift_pack([card],'random',single_card=True)[0])
            self.assertIn('https://see.tg',app.gift_pack([card],'random')[0])
        with patch.object(app,'credit_in_description',False):
            self.assertIn('https://see.tg',app.gift_pack([card],'random',single_card=True)[0])
