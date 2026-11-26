import re, sqlite3, time
from decimal import Decimal
from urllib.parse import urlsplit

SLUG = re.compile(r'[A-Za-z][A-Za-z0-9]{0,79}-[1-9][0-9]{0,11}')
USER = re.compile(r'[A-Za-z0-9_]{1,32}')

def slug_of(s):
    s = s.strip()
    if s.startswith('https://t.me/nft/'):
        u = urlsplit(s)
        s = u.path.removeprefix('/nft/')
    if not SLUG.fullmatch(s):
        raise ValueError('Нужна ссылка https://t.me/nft/HexPot-44075 или slug.')
    return s

def parse_gift(g):
    slug = slug_of(g.slug)
    attrs = {type(a).__name__: a for a in g.attributes}
    def name(k): return getattr(attrs.get('StarGiftAttribute'+k), 'name', '')
    model = attrs.get('StarGiftAttributeModel')
    rarity = getattr(model, 'rarity_permille', None)
    if rarity is None: rarity = getattr(getattr(model, 'rarity', None), 'permille', None)
    stars = ton = None
    for a in getattr(g, 'resell_amount', None) or []:
        typ = type(a).__name__
        if typ == 'StarsAmount': stars = int(a.amount) + getattr(a, 'nanos', 0)/1e9
        elif typ in ('StarsTonAmount', 'StarsGramAmount'): ton = int(a.amount)/1e9
    return dict(slug=slug, title=g.title, num=g.num, model=name('Model'), backdrop=name('Backdrop'), pattern=name('Pattern'), rarity=rarity, stars=stars, ton=ton, updated=int(time.time()))

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS gifts(slug TEXT PRIMARY KEY, title TEXT, num INTEGER, model TEXT, backdrop TEXT, pattern TEXT, rarity INTEGER, stars REAL, ton REAL, updated INTEGER, market_at INTEGER, market_collection TEXT);
        CREATE TABLE IF NOT EXISTS consent(slug TEXT PRIMARY KEY, uid INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS gift_title ON gifts(title);''')
    def save(self, g, market_collection=None, market_at=None):
        keys = ['slug','title','num','model','backdrop','pattern','rarity','stars','ton','updated']
        data = [g[k] for k in keys]
        self.db.execute('INSERT INTO gifts('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+') ON CONFLICT(slug) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in keys[1:]), data)
        if market_collection is not None:
            self.db.execute('UPDATE gifts SET market_collection=?,market_at=? WHERE slug=?', (str(market_collection),market_at,g['slug']))
        self.db.commit()
    def complete(self, collection, stamp):
        self.db.execute('UPDATE gifts SET market_at=NULL,stars=NULL,ton=NULL WHERE market_collection=? AND market_at<?', (str(collection),stamp)); self.db.commit()
    def grant(self, slug, uid):
        self.db.execute('INSERT INTO consent VALUES(?,?,?) ON CONFLICT(slug) DO UPDATE SET uid=excluded.uid,expires=excluded.expires', (slug,uid,int(time.time())+86400)); self.db.commit()
    def revoke(self, uid):
        self.db.execute('DELETE FROM consent WHERE uid=?',(uid,)); self.db.commit()
    def consent(self, slug):
        r=self.db.execute('SELECT uid FROM consent WHERE slug=? AND expires>?',(slug,int(time.time()))).fetchone()
        return r[0] if r else None
    def query(self, filters, market=False, page=0, ttl=3600):
        where, args = [], []
        for k in ('title','model','backdrop','pattern'):
            if filters.get(k):
                where.append(k+' = ? COLLATE NOCASE'); args.append(filters[k])
        if filters.get('num') is not None: where.append('num=?'); args.append(filters['num'])
        if filters.get('rare'): where.append('rarity IS NOT NULL AND rarity<=10')
        currency = filters.get('currency','stars')
        if currency not in ('stars','ton'): raise ValueError('Валюта')
        if market:
            where.append('market_at>=? AND (stars>0 OR ton>0)'); args.append(int(time.time())-ttl)
        for k,op in [('min','>='),('max','<=')]:
            if filters.get(k) is not None:
                where.append(f'{currency} {op} ?'); args.append(filters[k])
        clause=' WHERE '+' AND '.join(where) if where else ''
        total=self.db.execute('SELECT count(*) FROM gifts'+clause,args).fetchone()[0]
        order = f'{currency} IS NULL, {currency}, slug' if filters.get('price_sort') else 'updated DESC, slug'
        if filters.get('number_sort'): order='num, slug'
        page=max(0,min(page,max(0,(total-1)//10)))
        rows=self.db.execute('SELECT * FROM gifts'+clause+' ORDER BY '+order+' LIMIT 10 OFFSET ?',args+[page*10]).fetchall()
        return rows,total,page
    def options(self, field, title=None):
        if field not in ('title','model','backdrop','pattern'): raise ValueError('field')
        q=f'SELECT DISTINCT {field} FROM gifts WHERE {field} IS NOT NULL AND {field} != ?'; args=['']
        if title and field!='title': q+=' AND title=? COLLATE NOCASE'; args.append(title)
        return [r[0] for r in self.db.execute(q+f' ORDER BY {field} COLLATE NOCASE',args)]
