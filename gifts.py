"""Small, rate-limited random selection from public collectible cards.
No owner fields, private Telegram data, account sessions or resale claims.
"""
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import random, re, threading, time

COLLECTIONS={'PlushPepe':('Plush Pepe',2000),'JellyBunny':('Jelly Bunny',10000),'LushBouquet':('Lush Bouquet',1000),'PreciousPeach':('Precious Peach',1000)}
FIELDS={'Model':'model','Backdrop':'backdrop','Symbol':'symbol','Quantity':'quantity'}
class GiftError(Exception): pass
class CardParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.row=None;self.cell=None;self.attrs={}
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(' '.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            if len(self.row)>=2 and self.row[0] in FIELDS:self.attrs[FIELDS[self.row[0]]]=self.row[1][:160]
            self.row=None;self.cell=None

def parse_card(html,slug):
    m=re.fullmatch(r'([A-Za-z]+)-(\d+)',slug)
    if not m or m[1] not in COLLECTIONS:return None
    p=CardParser();p.feed(html)
    if not {'model','backdrop','symbol'}<=p.attrs.keys():return None
    return {'slug':slug,'name':COLLECTIONS[m[1]][0],'number':int(m[2]),'url':'https://t.me/nft/'+slug,**p.attrs}

_lock=threading.Lock();_cache={};_last=0.;_blocked_until=0.
def fetch_card(slug):
    global _last,_blocked_until
    with _lock:
        now=time.monotonic()
        if now<_blocked_until:raise GiftError('Источник временно ограничил запросы. Попробуй позже.')
        if slug in _cache and now-_cache[slug][0]<900:return _cache[slug][1]
        time.sleep(max(0,1.5-(now-_last)));_last=time.monotonic()
        try:
            with urlopen(Request('https://t.me/nft/'+slug,headers={'User-Agent':'GiftRadar/3.0 (public collectible preview)'}),timeout=8) as r:
                if not r.geturl().startswith('https://t.me/nft/'):return None
                html=r.read(600000).decode('utf-8','replace')
        except HTTPError as e:
            if e.code==429:
                try:wait=int(e.headers.get('Retry-After','60'))
                except ValueError:wait=60
                _blocked_until=time.monotonic()+max(60,wait);raise GiftError('Источник ограничил запросы. Попробуй позже.') from None
            if e.code==404:return None
            raise GiftError('Публичный источник недоступен.') from None
        except (URLError,TimeoutError,OSError):raise GiftError('Публичный источник недоступен.') from None
        item=parse_card(html,slug)
        if len(_cache)>=500:_cache.pop(next(iter(_cache)))
        _cache[slug]=(time.monotonic(),item);return item

MAX_BATCH=10
_job=threading.Lock();_last_job=0.
def random_gifts(count, filters=None):
    global _last_job
    if type(count) is not int or not 1<=count<=MAX_BATCH:raise ValueError(f'Выбери от 1 до {MAX_BATCH} подарков.')
    filters=validate_filters(filters)
    if not _job.acquire(blocking=False):raise GiftError('Подборка уже выполняется. Попробуй чуть позже.')
    try:
        if time.monotonic()-_last_job<10 and _last_job:raise GiftError('Подожди несколько секунд перед новой подборкой.')
        _last_job=time.monotonic();results=[];seen=set()
        deadline=time.monotonic()+40
        for _ in range(min(20,count+8) if filters else min(20,count+5)):
            if time.monotonic()>=deadline:break
            collection=filters.get('collection') or random.choice(list(COLLECTIONS));slug=f'{collection}-{random.randint(1,COLLECTIONS[collection][1])}'
            if slug in seen:continue
            seen.add(slug)
            try:item=fetch_card(slug)
            except GiftError:
                if results:break
                raise
            if item and matches(item,filters):results.append(item)
            if len(results)==count:break
        if not results and not filters:raise GiftError('Не удалось получить карточки. Попробуй позже.')
        return results
    finally:_job.release()


def validate_filters(filters):
    if filters is None:return {}
    if not isinstance(filters,dict) or set(filters)-{'collection','model','backdrop','symbol'}:raise ValueError('Неизвестный фильтр.')
    out={}
    for k,v in filters.items():
        if not isinstance(v,str) or len(v)>100:raise ValueError('Некорректный фильтр.')
        if v.strip():out[k]=v.strip()
    if out.get('collection') and out['collection'] not in COLLECTIONS:raise ValueError('Неизвестная коллекция.')
    return out

def matches(item,filters):
    return all((item['slug'].split('-')[0]==v if k=='collection' else v.casefold() in item.get(k,'').casefold()) for k,v in filters.items())
