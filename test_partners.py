import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import app, partners

class PartnersTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(partners,'PATH',Path(self.tmp.name)/'p.sqlite3');p.start();self.addCleanup(p.stop)
    def test_submit_is_idempotent_and_private(self):
        a=partners.submit(12,'@SampleGiftBot')
        self.assertEqual(a['id'],partners.submit(12,'samplegiftbot')['id'])
        self.assertEqual(partners.mine(13),[])
        with self.assertRaises(ValueError):partners.submit(13,'SampleGiftBot')
    def test_secrets_rejected(self):
        for value in ('12345:secret','https://t.me/SomeBot','ordinary','x'*200,None):
            with self.assertRaises(ValueError):partners.submit(12,value)
    def test_quota_and_status(self):
        a=partners.submit(12,'FirstGiftBot');partners.submit(12,'SecondGiftBot');partners.submit(12,'ThirdGiftBot')
        with self.assertRaises(ValueError):partners.submit(12,'FourthGiftBot')
        with self.assertRaises(ValueError):partners.review(a['id'],'active')
        self.assertEqual(partners.review(a['id'],'approved')['status'],'approved')
        self.assertEqual(partners.review(a['id'],'active')['status'],'active')
        self.assertEqual(partners.review(a['id'],'disabled')['status'],'disabled')
    def test_admin_approval_only(self):
        a=partners.submit(12,'FirstGiftBot')
        def msg(uid):return {'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':'/partnerapprove '+str(a['id'])}
        with patch.object(app,'send'),patch.object(app,'ADMIN_IDS',{8677315356}):
            app.handle(msg(12));self.assertEqual(partners.mine(12)[0]['status'],'pending')
            app.handle(msg(8677315356));self.assertEqual(partners.mine(12)[0]['status'],'approved')
    def test_membership_gate(self):
        m={'chat':{'id':12,'type':'private'},'from':{'id':12},'text':'/partner @FirstGiftBot'}
        with patch.object(app,'gate',return_value=False),patch.object(app,'send'):app.handle(m)
        self.assertEqual(partners.mine(12),[])
