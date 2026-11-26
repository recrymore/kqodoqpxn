"""See.tg public API adapter, admin test only in this release.
Batch results retain gift attributes, never profile/contact data.
Only standard gift endpoints; no hidden-owner inference endpoints.
"""
import json, logging, math, os, random, re, threading, time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from gifts import GiftError

BASE = 'https://kartoshka.free/v1/'
MAX_BATCH = 50
SLUG = re.compile(r'[A-Za-z][A-Za-z0-9]{0,80}-[1-9][0-9]{0,9}')
log = logging.getLogger('see')
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def enabled():
    return bool(os.environ.get('SEE_API_KEY','').strip())

def clean(v):
    if isinstance(v, dict): v = v.get('name') or v.get('title') or v.get('value')
    if not isinstance(v,str): return '—'
    return ' '.join(v.split())[:100] or '—'

def numeric(v):
    try:
        if isinstance(v,bool): return None
        n=float(v)
        return n if math.isfinite(n) and 0 <= n <= 1e12 else None
    except (ValueError,TypeError,OverflowError): return None

def normalize(raw):
    if not isinstance(raw,dict): return None
    details=raw.get('details') if isinstance(raw.get('details'),dict) else {}
    # Only whitelisted gift fields cross the adapter boundary.
    def field(k): return raw.get(k) if raw.get(k) is not None else details.get(k)
    slug=field('slug')
    if not isinstance(slug,str) or not SLUG.fullmatch(slug): return None
    est=raw.get('estimate') if isinstance(raw.get('estimate'),dict) else {}
    item={'slug':slug,'name':clean(field('title')),'number':int(slug.rsplit('-',1)[1]),'url':'https://t.me/nft/'+slug,'model':clean(field('model')),'backdrop':clean(field('backdrop')),'symbol':clean(field('pattern')),'source':'See.tg','on_sale':field('onSale') is True}
    for k,target in [('ton','estimate_ton'),('low','estimate_low'),('high','estimate_high')]:
        v=numeric(est.get(k))
        if v is not None:item[target]=v
    if isinstance(est.get('confidence'),str):item['confidence']=clean(est['confidence'])
    return item

class Client:
    def __init__(self):
        self.lock=threading.RLock(); self.last=0.; self.blocked=0.; self.cache={}; self.remaining=None
        self.opener=build_opener(NoRedirect())
    def _request(self,path,params=None):
        token=os.environ.get('SEE_API_KEY','').strip()
        if not token:raise GiftError('Подключение к источнику данных не настроено.')
        now=time.monotonic()
        if now<self.blocked:raise GiftError('Источник данных временно ограничил запросы. Попробуй позже.')
        if self.last:time.sleep(max(0,1.2-(now-self.last)))
        self.last=time.monotonic()
        req=Request(BASE+path+('?' + urlencode(params) if params else ''),headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'GiftRadar/1.0'})
        try:
            with self.opener.open(req,timeout=20) as resp:
                payload=json.loads(resp.read(4_000_000))
                try:self.remaining=int(resp.headers.get('X-Free-Remaining',''))
                except (ValueError,TypeError):self.remaining=None
                log.info('see endpoint=%s HTTP=%s free=%s',path.split('/')[0],resp.status,self.remaining)
                if not isinstance(payload,dict) or payload.get('ok') is not True:raise GiftError('Источник данных вернул неожиданный ответ.')
                return payload.get('result')
        except HTTPError as e:
            log.warning('see endpoint=%s HTTP=%s',path.split('/')[0],e.code)
            if e.code==429:
                try:wait=max(60,int(e.headers.get('Retry-After','60')))
                except (ValueError,TypeError):wait=60
                self.blocked=time.monotonic()+wait
            if e.code in (403,525):self.blocked=time.monotonic()+60
            raise GiftError({401:'Источник данных: проверь токен.',402:'Источник данных: бесплатная квота закончилась.',403:'Источник данных: доступ запрещён.',404:'Подарок не найден.',429:'Источник данных: лимит запросов.',525:'Источник данных: временная ошибка TLS.'}.get(e.code,f'Источник данных: HTTP {e.code}.')) from None
        except (URLError,TimeoutError,OSError,ValueError):raise GiftError('Источник данных: ошибка сети или формата ответа.') from None
    def quota(self):
        with self.lock:
            self._request('me')
            return self.remaining
    def _free(self):
        # Recheck quota every cache miss; no billable calls after quota is exhausted.
        self._request('ping')
        if self.remaining is None or self.remaining<1:raise GiftError('Бесплатная квота не подтверждена или закончилась. Запрос отменён.')
    def search(self,count,filters=None,mode=None):
        if type(count) is not int or not 1<=count<=MAX_BATCH:raise ValueError('Количество: от 1 до 50.')
        filters=filters or {}
        if not isinstance(filters,dict) or set(filters)-{'collection','model','backdrop','symbol'}:raise ValueError('Некорректные фильтры.')
        params={'limit':100,'burned':'false'}
        for k,v in filters.items():
            if not isinstance(v,str) or len(v)>100:raise ValueError('Некорректный фильтр.')
            if v.strip():params['pattern' if k=='symbol' else k]=v.strip()
        if mode is not None:
            if mode not in ('low','mid','high','all'):raise ValueError('Неизвестный диапазон.')
            params.update(on_sale='true',sort='price',price_currency='ton')
            if mode=='low':params['price_to']=4_999_999_999
            if mode=='mid':params.update(price_from=5_000_000_000,price_to=19_999_999_999)
            if mode=='high':params['price_from']=20_000_000_000
        key=tuple(sorted(params.items()))
        with self.lock:
            cached=self.cache.get(key)
            if cached and time.monotonic()-cached[0]<300:
                items=cached[1];log.info('see cache hit count=%s',len(items))
            else:
                self._free();data=self._request('gifts',params)
                if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise GiftError('Источник данных: неизвестная структура списка.')
                items=list({g['slug']:g for x in data['items'] if (g:=normalize(x))}.values())
                if len(self.cache)>=32:self.cache.pop(next(iter(self.cache)))
                self.cache[key]=(time.monotonic(),items)
        return random.sample(items,min(count,len(items)))
    def single(self,slug):
        if not isinstance(slug,str) or not SLUG.fullmatch(slug):raise ValueError('Формат: /gift PlushPepe-1')
        with self.lock:
            self._free();raw=self._request('gift/'+slug)
        item=normalize(raw)
        if item is None:raise GiftError('Источник данных: структура карточки не распознана.')
        # Single, explicit inspection in admin private chat only; never cached/exported in batches.
        owner=raw.get('owner')
        if isinstance(owner,dict) and owner.get('seeType')=='public' and owner.get('telegramType')=='user':
            u=owner.get('username')
            if isinstance(u,str) and re.fullmatch(r'[A-Za-z0-9_]{4,32}',u):item['public_username']='@'+u
        return item
client=Client()
