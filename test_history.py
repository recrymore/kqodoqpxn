import tempfile, unittest, time
from pathlib import Path
from unittest.mock import patch
import app, see_api
from gifts import GiftError
from test_see_api import raw

class HistoryTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        p=patch.multiple(app,DB=Path(t.name)/'db.sqlite',ADMIN_IDS={99});p.start();self.addCleanup(p.stop)
        p=patch.object(see_api,'enabled',return_value=True);p.start();self.addCleanup(p.stop)
        self.c=see_api.Client()
        p=patch.object(app.usage,"settings",return_value={**app.limits.DEFAULT,"cooldown":0});p.start();self.addCleanup(p.stop)
    def test_next_page_and_no_repeat(self):
        pages=[{'items':[raw(i) for i in range(1,5)],'nextCursor':'opaque-next'}, {'items':[raw(i) for i in range(4,9)]}]
        with patch.object(see_api,'client',self.c),patch.object(self.c,'_free'),patch.object(self.c,'_request',side_effect=pages) as req:
            with patch.object(self.c, '_enrich_owner', side_effect=lambda x: x):
                first=app.search_gifts(99,3,filters={'collection':'PlushPepe'})
                second=app.search_gifts(99,3,filters={'collection':'PlushPepe'})
                third=app.search_gifts(99,3,filters={'collection':'PlushPepe'})
                self.assertEqual([len(x) for x in (first,second,third)],[3,3,2])
                slugs=[g['slug'] for g in first+second+third]
                self.assertEqual(len(set(slugs)),8)
                self.assertEqual(req.call_count,2)
                self.assertEqual(req.call_args.args[1]['cursor'],'opaque-next')
                self.assertEqual(app.search_gifts(99,3,filters={'collection':'PlushPepe'}),[])
                self.assertEqual(req.call_count,2)
                self.assertNotIn('example_owner',str(self.c.cache))
    def test_history_persists_and_is_private(self):
        items=[see_api.normalize(raw(1)),see_api.normalize(raw(2))]
        app.remember_gifts(99,items)
        self.assertEqual(app.seen_gifts(99),{'plushpepe-1','plushpepe-2'})
        self.assertEqual(app.seen_gifts(100),set())
        with app.db() as c:
            self.assertEqual({r['name'] for r in c.execute('PRAGMA table_info(seen_gifts)')},{'uid','slug','shown'})
            c.execute('UPDATE seen_gifts SET shown=? WHERE slug=?',(int(time.time())-8*86400,'plushpepe-1'))
        self.assertEqual(app.seen_gifts(99),{'plushpepe-2'})
        app.clear_seen(100);self.assertEqual(app.seen_gifts(99),{'plushpepe-2'})
        app.clear_seen(99);self.assertFalse(app.seen_gifts(99))
    def test_preferences(self):
        self.assertEqual(app.set_count(99,25),25)
        self.assertEqual(app.preferred_count(99),25)
        self.assertEqual(app.preferred_count(100),10)
        with self.assertRaises(ValueError):app.set_count(100,37)
        with self.assertRaises(ValueError):app.set_count(99,51)
    def test_one_page_limit(self):
        data={'items':[raw(1)],'nextCursor':'opaque'}
        with patch.object(self.c,'_free'),patch.object(self.c,'_request',return_value=data) as req:
            with patch.object(self.c, '_enrich_owner', side_effect=lambda x: x):
                self.assertEqual(self.c.search(50,exclude={'plushpepe-1'}),[])
                self.assertEqual(req.call_count,1)
                self.assertEqual(self.c.search(50,exclude={'plushpepe-1'}),[])
                self.assertEqual(req.call_count,2)
                self.assertIsNone(next(iter(self.c.cache.values()))[2])
    def test_quota_partial(self):
        data={'items':[raw(1),raw(2)],'nextCursor':'opaque'}
        with patch.object(self.c,'_free',side_effect=[None,GiftError('quota')]),patch.object(self.c,'_request',return_value=data) as req:
            with patch.object(self.c, '_enrich_owner', side_effect=lambda x: x):
                self.c.search(1,exclude=set())
                result=self.c.search(10,exclude={'plushpepe-1'})
                self.assertEqual([g['slug'] for g in result],['PlushPepe-2'])
                self.assertEqual(req.call_count,1)
    def test_public_selection_excludes_old(self):
        cards=[see_api.normalize(raw(1)),see_api.normalize(raw(2))]
        with patch.object(app,'random_gifts',return_value=cards):
            self.assertEqual(len(app.search_gifts(100,2)),2)
            self.assertEqual(app.search_gifts(100,2),[])
            self.assertEqual(len(app.search_gifts(101,2)),2)

if __name__=='__main__':unittest.main()