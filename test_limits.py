import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import app, limits, see_api
from gifts import GiftError
from test_see_api import raw

class LimitTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.multiple(app,CONFIG=Path(tmp.name)/'config.json',DB=Path(tmp.name)/'db.sqlite',ADMIN_IDS={99},MIRROR_MODE=False);p.start();self.addCleanup(p.stop)
        p=patch.dict(app.os.environ,{'PUBLIC_MARKET_APPROVED':'0'});p.start();self.addCleanup(p.stop)
    def test_default_and_admin_guard(self):
        self.assertEqual(app.max_count(123),25);self.assertEqual(app.max_count(99),25)
        self.assertEqual(app.usage.remaining(123),{'search':19,'detail':10})
        self.assertNotIn('limits',app.public_config())
        with self.assertRaises(PermissionError):app.admin_action(123,{'action':'save_limits','limits':limits.DEFAULT})
        with self.assertRaises(ValueError):app.admin_action(99,{'action':'save_limits','limits':{**limits.DEFAULT,'budget_nano':-1}})
        app.admin_action(99,{'action':'save_limits','limits':{**limits.DEFAULT,'batch_max':12}})
        self.assertEqual(app.max_count(123),12)
    def test_daily_and_cooldown(self):
        app.change('limits',{**limits.DEFAULT,'search_daily':2})
        with patch('limits.time.time',return_value=1000):app.usage.consume(123,'search')
        with patch('limits.time.time',return_value=1001):
            with self.assertRaises(GiftError):app.usage.consume(123,'search')
            app.usage.consume(124,'search');app.usage.consume(123,'detail')
        with patch('limits.time.time',return_value=1011):app.usage.consume(123,'search')
        with self.assertRaises(GiftError):app.usage.consume(123,'search')
        with patch('limits.day',return_value='2099-01-02'):self.assertEqual(app.usage.remaining(123)['search'],2)
    def test_finance(self):
        app.change('limits',{**limits.DEFAULT,'budget_nano':4_000_000})
        d=app.usage.reserve();app.usage.settle(d,'0')
        self.assertEqual(app.usage.finance()['cost'],0)
        for _ in range(2):
            d=app.usage.reserve();app.usage.settle(d,'0.002')
        self.assertEqual(app.usage.finance()['cost'],4_000_000)
        with self.assertRaises(GiftError):app.usage.reserve()
    def test_unknown_cost_stops_and_persists(self):
        d=app.usage.reserve();app.usage.settle(d,None)
        other=limits.Limits(app.db,app.read_config)
        self.assertEqual(other.finance()['uncertain'],1)
        with self.assertRaises(GiftError):other.reserve()
    def test_higher_charge_stops(self):
        d=app.usage.reserve();app.usage.settle(d,'0.003')
        self.assertEqual(app.usage.finance()['cost'],3_000_000)
        with self.assertRaises(GiftError):app.usage.reserve()
    def test_atomic_reservation(self):
        def attempt(_):
            try:app.usage.reserve();return True
            except GiftError:return False
        with ThreadPoolExecutor(max_workers=4) as ex:self.assertEqual(sum(ex.map(attempt,range(4))),1)
    def test_approval_separate_and_no_contacts_in_public_data(self):
        with patch.object(see_api,'enabled',return_value=True):
            self.assertFalse(app.see_enabled(123));self.assertTrue(app.see_enabled(99))
            with patch.dict(app.os.environ,{'PUBLIC_MARKET_APPROVED':'1'}):
                self.assertTrue(app.see_enabled(123));self.assertFalse(app.owner_inspection(123))
                c=see_api.Client()
                with patch.object(c,'_free'),patch.object(c,'_request',return_value=raw()):
                    self.assertNotIn('public_username',c.single('PlushPepe-1',include_owner=False))
    def test_paid_toggle(self):
        c=see_api.Client();c.billing=app.usage;c.remaining=0
        with patch.object(c,'_request'):
            with self.assertRaises(GiftError):c._free()
            app.change('limits',{**limits.DEFAULT,'paid':True});c._free()
    def test_global_pause(self):
        app.change('limits',{**limits.DEFAULT,'search_open':False})
        for uid in (99,123):
            for kind in ('search','detail'):
                with self.assertRaises(GiftError):app.usage.consume(uid,kind)
