import time, unittest
from types import SimpleNamespace
from core import Store, slug_of, parse_gift
class Tests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:')
        self.g=dict(slug='HexPot-1',title='Hex Pot',num=1,model='Witch',backdrop='Black',pattern='Star',rarity=5,stars=100,ton=None,updated=int(time.time()))
    def test_slug(self):
        self.assertEqual(slug_of('https://t.me/nft/HexPot-1'),'HexPot-1')
        with self.assertRaises(ValueError): slug_of('https://example.com/nft/HexPot-1')
    def test_query(self):
        self.s.save(self.g)
        self.assertEqual(self.s.query({'backdrop':'black'})[1],1)
        self.assertEqual(self.s.query({},True)[1],0)
        self.s.save(self.g,123,int(time.time()))
        self.assertEqual(self.s.query({'rare':True,'min':99},True)[1],1)
        self.assertEqual(self.s.query({'min':101},True)[1],0)
        self.s.complete(123,int(time.time())+1)
        self.assertEqual(self.s.query({},True)[1],0)
    def test_consent(self):
        self.assertIsNone(self.s.consent('HexPot-1'))
        self.s.grant('HexPot-1',42); self.assertEqual(self.s.consent('HexPot-1'),42)
        self.s.revoke(42); self.assertIsNone(self.s.consent('HexPot-1'))
    def test_page(self):
        for i in range(1,24): self.s.save(dict(self.g,slug=f'HexPot-{i}',num=i))
        rows,n,p=self.s.query({'number_sort':True},page=9)
        self.assertEqual((len(rows),n,p),(3,23,2)); self.assertEqual(rows[0]['num'],21)
    def test_parse(self):
        model=type('StarGiftAttributeModel',(),{})(); model.name='Witch'; model.rarity=SimpleNamespace(permille=5)
        amount=type('StarsAmount',(),{})(); amount.amount=100
        g=SimpleNamespace(slug='HexPot-1',title='Hex Pot',num=1,attributes=[model],resell_amount=[amount])
        self.assertEqual(parse_gift(g)['rarity'],5); self.assertEqual(parse_gift(g)['stars'],100)
if __name__=='__main__': unittest.main()
