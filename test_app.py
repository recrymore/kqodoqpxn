import unittest, tempfile, time, json, hmac, hashlib, threading
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import app,gifts

class Tests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        p=patch.multiple(app,TOKEN='test',GROUP_ID='-100123',ADMIN_IDS={99},CONFIG=Path(t.name)/'config.json',DB=Path(t.name)/'db.sqlite',URL='https://example.test');p.start();self.addCleanup(p.stop)
    def signed(self,uid=123):
        f={'auth_date':str(int(time.time())),'user':json.dumps({'id':uid})}
        secret=hmac.new(b'WebAppData',app.TOKEN.encode(),hashlib.sha256).digest();f['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(f.items())).encode(),hashlib.sha256).hexdigest();return urlencode(f)
    def test_auth(self):
        self.assertEqual(app.verify_init(self.signed()),123)
        with self.assertRaises(ValueError):app.verify_init(self.signed().replace('123','124'))
    def test_membership(self):
        for status,flag,expected in [('member',False,True),('administrator',False,True),('creator',False,True),('left',False,False),('kicked',False,False),('restricted',True,True),('restricted',False,False)]:
            with patch.object(app,'api',return_value={'status':status,'is_member':flag}):self.assertEqual(app.membership(123)['member'],expected)
        with patch.object(app,'api',side_effect=RuntimeError):self.assertFalse(app.membership(123)['member'])
        with patch.object(app,'GROUP_ID',''):self.assertFalse(app.membership(123)['member'])
    def test_gate(self):
        srv=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);t=threading.Thread(target=srv.serve_forever,daemon=True);t.start()
        try:
            def call():return urlopen(Request(f'http://127.0.0.1:{srv.server_port}/api/random',data=json.dumps({'initData':self.signed(),'count':1}).encode(),headers={'Content-Type':'application/json'}),timeout=3)
            with patch.object(app,'api',return_value={'status':'left'}),patch.object(app,'random_gifts') as f:
                with self.assertRaises(HTTPError) as e:call()
                self.assertEqual(e.exception.code,403);f.assert_not_called()
            with patch.object(app,'api',return_value={'status':'member'}),patch.object(app,'random_gifts',return_value=[{'slug':'PlushPepe-123'}]):
                with call() as r:self.assertEqual(json.load(r)['gifts'][0]['slug'],'PlushPepe-123')
        finally:srv.shutdown();srv.server_close();t.join()
    def test_public_fields(self):
        html='<table><tr><th>Owner</th><td>Private example</td></tr><tr><th>Model</th><td>Steel <mark>1%</mark></td></tr><tr><th>Backdrop</th><td>Green</td></tr><tr><th>Symbol</th><td>Potion</td></tr></table>'
        result=gifts.parse_card(html,'PlushPepe-123');self.assertEqual(result['model'],'Steel 1%');self.assertNotIn('Private example',str(result));self.assertIsNone(gifts.parse_card('<html>not found</html>','PlushPepe-123'))
        with self.assertRaises(ValueError):gifts.random_gifts(999)
    def test_admin(self):
        with patch.object(app,'send') as send:
            def msg(uid,text):app.handle({'chat':{'type':'private','id':uid},'from':{'id':uid},'text':text})
            msg(123,'✏️ Верхний баннер');msg(123,'Чужой');self.assertNotEqual(app.read_config()['banner'],'Чужой')
            msg(99,'✏️ Верхний баннер');msg(99,'Новый');self.assertEqual(app.read_config()['banner'],'Новый');self.assertNotIn('tariffs',app.read_config())
            app.handle({'chat':{'type':'supergroup','id':-100123},'from':{'id':99},'text':'/groupid'});self.assertIn('-100123',send.call_args.args[1])
    def test_support(self):
        with patch.object(app,'send'):
            tid=app.new_ticket(123,'question','Привет');self.assertTrue(app.answer_ticket(tid,'Ответ'));self.assertEqual(app.tickets(123)[0]['reply'],'Ответ')
    def test_filters(self):
        item={'slug':'PlushPepe-123','model':'Steel 1%','backdrop':'Black','symbol':'Star'}
        self.assertTrue(gifts.matches(item,gifts.validate_filters({'model':'steel','collection':'PlushPepe'})))
        self.assertFalse(gifts.matches(item,{'backdrop':'pink'}))
        with self.assertRaises(ValueError):gifts.validate_filters({'owner_id':'123'})
        with self.assertRaises(ValueError):gifts.validate_filters({'collection':'Unknown'})
    def test_price_catalog(self):
        card={'slug':'PlushPepe-123','name':'Plush Pepe','number':123,'url':'https://t.me/nft/PlushPepe-123','model':'Steel','backdrop':'Black','symbol':'Star'}
        with patch.object(app,'fetch_card',return_value=card):
            app.set_lot('PlushPepe-123','5')
            self.assertEqual(app.price_gifts('low'),[])
            self.assertEqual(app.price_gifts('mid')[0]['price_ton'],5)
            with self.assertRaises(ValueError):app.set_lot('PlushPepe-123','NaN')
        with app.db() as c:c.execute('UPDATE lots SET updated=0')
        self.assertEqual(app.price_gifts('all'),[])
    def test_chat_gate(self):
        q={'id':'1','from':{'id':123},'message':{'chat':{'type':'private','id':123}},'data':'random'}
        with patch.object(app,'api'),patch.object(app,'membership',return_value={'member':False}),patch.object(app,'send'),patch.object(app,'random_gifts') as f:
            app.callback(q);f.assert_not_called()
        with patch.object(app,'api'),patch.object(app,'membership',return_value={'member':True}),patch.object(app,'send'),patch.object(app,'random_gifts',return_value=[]) as f:
            app.callback(q);f.assert_called_once_with(3)
if __name__=='__main__':unittest.main()
