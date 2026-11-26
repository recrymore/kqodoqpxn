import hashlib,hmac,json,tempfile,time,unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import app
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for k,v in dict(TOKEN='test-token',DB=Path(self.tmp.name)/'test.db',CONFIG=Path(self.tmp.name)/'config.json',ADMIN_IDS={99},URL='https://example.test').items():
            p=patch.object(app,k,v);p.start();self.addCleanup(p.stop)
        p=patch.object(app,'send');self.send=p.start();self.addCleanup(p.stop)
    def signed(self,uid=123,age=0):
        f={'auth_date':str(int(time.time())-age),'user':json.dumps({'id':uid}),'query_id':'q'}
        secret=hmac.new(b'WebAppData',app.TOKEN.encode(),hashlib.sha256).digest()
        f['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(f.items())).encode(),hashlib.sha256).hexdigest()
        return urlencode(f)
    def test_auth(self):
        self.assertEqual(app.verify_init(self.signed()),123)
        for invalid in (self.signed().replace('123','124'),self.signed(age=90000),''):
            with self.assertRaises(ValueError):app.verify_init(invalid)
    def test_tickets(self):
        tid=app.new_ticket(123,'question','Привет')
        self.assertEqual(app.tickets(456),[])
        self.assertTrue(app.answer_ticket(tid,'Ответ'))
        self.assertEqual(app.tickets(123)[0]['reply'],'Ответ')
        with self.assertRaises(ValueError):app.answer_ticket(tid,'Повтор')
    def test_config(self):
        app.change('welcome','Новое приветствие')
        self.assertEqual(app.read_config()['welcome'],'Новое приветствие')
        self.assertEqual(app.parse_tariffs('1 10\n3 25.5')[1]['price'],'25.5')
    def test_admin(self):
        def msg(uid,text):app.handle({'chat':{'type':'private','id':uid},'from':{'id':uid},'text':text})
        msg(123,'👋 Приветствие');msg(123,'Чужой текст')
        self.assertNotEqual(app.read_config()['welcome'],'Чужой текст')
        msg(99,'👋 Приветствие');msg(99,'Новый текст')
        self.assertEqual(app.read_config()['welcome'],'Новый текст')
        msg(123,'/start');self.assertIn('Новый текст',self.send.call_args.args[1])
if __name__=='__main__':unittest.main()
