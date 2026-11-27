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
COLLECTION_SLUG = re.compile(r'[A-Za-z][A-Za-z0-9]{0,80}')

def gift_number(value):
    if type(value) is int and 1 <= value <= 9999999999:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value):
        return value
    return None

def gift_ref(slug, num):
    if not isinstance(slug, str):
        return None
    slug = slug.strip()
    number = gift_number(num)
    if SLUG.fullmatch(slug):
        if num is not None and number != slug.rsplit('-', 1)[1]:
            return None
        return slug
    if COLLECTION_SLUG.fullmatch(slug) and number is not None:
        return slug + '-' + number
    return None

def card_shape(raw):
    # Log only fixed field names and types, never API values or owner information.
    if not isinstance(raw, dict):
        return {'result': type(raw).__name__}
    result = {}
    gift = raw.get('gift')
    nested_details = gift.get('details') if isinstance(gift, dict) else None
    # Known schema paths only, no response values.
    for label, data in [('result', raw), ('details', raw.get('details')), ('gift', gift), ('gift.details', nested_details)]:
        result[label] = type(data).__name__
        if isinstance(data, dict):
            for key in ('slug', 'num', 'title', 'model', 'backdrop', 'pattern', 'estimate'):
                result[label + '.' + key] = type(data[key]).__name__ if key in data else 'missing'
                if key == 'slug':
                    value = data.get(key)
                    result[label + '.slug_format'] = ('full' if isinstance(value, str) and SLUG.fullmatch(value.strip()) else 'collection' if isinstance(value, str) and COLLECTION_SLUG.fullmatch(value.strip()) else 'unknown')
    return result
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
    # A single gift response may wrap its card in result.gift; batches
    # may still return flat cards. Do not invent an identifier from the request.
    details=raw.get('details') if isinstance(raw.get('details'),dict) else {}
    gift=raw.get('gift') if isinstance(raw.get('gift'),dict) else {}
    nested=gift.get('details') if isinstance(gift.get('details'),dict) else {}
    levels=(raw, details, gift, nested)
    def field(k):
        return next((level[k] for level in levels if level.get(k) is not None), None)
    # Keep number and slug within the same card, not an unrelated wrapper.
    refs=[]
    for primary, secondary in ((raw, details), (gift, nested)):
        value=primary.get('slug') if primary.get('slug') is not None else secondary.get('slug')
        num=primary.get('num') if primary.get('num') is not None else secondary.get('num')
        if value is not None:
            ref=gift_ref(value,num)
            if ref is None: return None
            refs.append(ref)
    if not refs or len({ref.casefold() for ref in refs}) != 1: return None
    slug=refs[0]
    est=next((level['estimate'] for level in levels if isinstance(level.get('estimate'),dict)),{})
    item={'slug':slug,'name':clean(field('title')),'number':int(slug.rsplit('-',1)[1]),'url':'https://t.me/nft/'+slug,'model':clean(field('model')),'backdrop':clean(field('backdrop')),'symbol':clean(field('pattern')),'source':'See.tg','on_sale':field('onSale') is True}
    for k,target in [('ton','estimate_ton'),('low','estimate_low'),('high','estimate_high')]:
        v=numeric(est.get(k))
        if v is not None:item[target]=v
    if isinstance(est.get('confidence'),str):item['confidence']=clean(est['confidence'])
    return item

class Client:
    def __init__(self):
        self.lock=threading.RLock(); self.last=0.; self.blocked=0.; self.cache={}; self.remaining=None
        self.opener=build_opener(NoRedirect()); self.billing=None
    def _request(self,path,params=None):
        token=os.environ.get('SEE_API_KEY','').strip()
        if not token:raise GiftError('Подключение к источнику данных не настроено.')
        now=time.monotonic()
        if now<self.blocked:raise GiftError('Источник данных временно ограничил запросы. Попробуй позже.')
        if self.last:time.sleep(max(0,1.2-(now-self.last)))
        self.last=time.monotonic()
        req=Request(BASE+path+('?' + urlencode(params) if params else ''),headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'GiftRadar/1.0'})
        reservation=self.billing.reserve() if self.billing and (path=='gifts' or path.startswith('gift/')) else None
        try:
            with self.opener.open(req,timeout=20) as resp:
                if reservation:self.billing.settle(reservation,resp.headers.get('X-Request-Cost'))
                payload=json.loads(resp.read(4_000_000))
                try:self.remaining=int(resp.headers.get('X-Free-Remaining',''))
                except (ValueError,TypeError):self.remaining=None
                log.info('see endpoint=%s HTTP=%s free=%s',path.split('/')[0],resp.status,self.remaining)
                if not isinstance(payload,dict) or payload.get('ok') is not True:raise GiftError('Источник данных вернул неожиданный ответ.')
                return payload.get('result')
        except HTTPError as e:
            if reservation:self.billing.settle(reservation,e.headers.get('X-Request-Cost') if e.headers else None)
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
        if self.remaining is None or (self.remaining<1 and not (self.billing and self.billing.settings()['paid'])):raise GiftError('Бесплатная квота не подтверждена или закончилась. Запрос отменён.')
    def search(self,count,filters=None,mode=None,*,exclude=None,number_window=None):
        if type(count) is not int or not 1<=count<=MAX_BATCH:raise ValueError('Количество: от 1 до 50.')
        filters=filters or {}
        if not isinstance(filters,dict) or set(filters)-{'collection','model','backdrop','symbol'}:raise ValueError('Некорректные фильтры.')
        params={'limit':100,'burned':'false'}
        if number_window is not None:
            lo,hi=number_window
            if type(lo) is not int or type(hi) is not int or not 1<=lo<=hi<=1_000_000:
                raise ValueError('Некорректный диапазон номеров.')
            params.update(number_from=lo,number_to=hi,sort='number',order='asc')
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
        excluded={str(x).casefold() for x in (exclude or ())}
        with self.lock:
            cached=self.cache.get(key)
            fresh=cached and time.monotonic()-cached[0]<300
            items=cached[1] if fresh else []
            cursor=cached[2] if fresh else None
            available=[g for g in items if g['slug'].casefold() not in excluded]
            # At most one new page per interactive request. Never scan in the background.
            advance=bool(fresh and exclude is not None and len(available)<count and cursor)
            if not fresh or advance:
                request_params=dict(params)
                if advance:request_params['cursor']=cursor
                try:
                    self._free();data=self._request('gifts',request_params)
                except GiftError:
                    if available:return random.sample(available,min(count,len(available)))
                    raise
                if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise GiftError('Источник данных: неизвестная структура списка.')
                page=list({g['slug'].casefold():g for x in data['items'] if (g:=normalize(x))}.values())
                log.info('gift batch returned=%s recognized=%s next_page=%s',len(data['items']),len(page),advance)
                if data['items'] and not page:
                    log.warning('gift batch schema rejected: %s',card_shape(data['items'][0]))
                    raise GiftError('Источник данных: структура списка подарков не распознана.')
                next_cursor=data.get('nextCursor')
                if not isinstance(next_cursor,str) or not next_cursor or len(next_cursor)>4096 or next_cursor==cursor:next_cursor=None
                items=list({g['slug'].casefold():g for g in (available if advance else [])+page}.values())
                if len(self.cache)>=32 and key not in self.cache:self.cache.pop(next(iter(self.cache)))
                self.cache[key]=(time.monotonic(),items,next_cursor)
                available=[g for g in items if g['slug'].casefold() not in excluded]
            else:log.info('gift cache hit available=%s',len(available))
            return random.sample(available,min(count,len(available)))
    def search_spread(self,count,*,exclude=None):
        """Private, bounded interactive sampling of gift attributes only.

        Sample up to three number bands via documented filters, never by
        constructing opaque cursors. Not a uniform sample of the entire market.
        """
        if type(count) is not int or not 1<=count<=MAX_BATCH:
            raise ValueError('Количество: от 1 до 50.')
        bands=[(1,999),(1000,99999),(100000,1000000)]
        random.shuffle(bands)
        seen={str(x).casefold() for x in (exclude or ())}
        pools=[];error=None
        with self.lock:
            for low,high in bands:
                # Leave space for a page; select a fresh start in this band.
                start=random.randint(low,max(low,high-100))
                try:
                    page=self.search(count,exclude=seen,number_window=(start,high))
                except GiftError as exc:
                    error=exc;break
                page=[g for g in page if start<=g['number']<=high and g['slug'].casefold() not in seen]
                seen.update(g['slug'].casefold() for g in page)
                pools.append(page)
                if len(pools)>=min(count,3) and sum(map(len,pools))>=count:break
            # Round-robin across number bands before taking more from a band.
            result=[]
            while len(result)<count and any(pools):
                for pool in pools:
                    if pool and len(result)<count:result.append(pool.pop())
            if not result and error is not None:raise error
            random.shuffle(result)
            log.info('gift diverse sample bands=%s returned=%s',len(pools),len(result))
            return result

    def single(self,slug,*,include_owner=True):
        if not isinstance(slug,str) or not SLUG.fullmatch(slug):raise ValueError('Формат: /gift PlushPepe-1')
        with self.lock:
            self._free();raw=self._request('gift/'+slug)
        item=normalize(raw)
        if item is None:
            log.warning('gift card schema rejected: %s', card_shape(raw))
            raise GiftError('Источник данных: структура карточки не распознана. Проверь логи запуска (gift card schema rejected).')
        if item['slug'].casefold() != slug.casefold():
            log.warning('gift card identifier mismatch')
            raise GiftError('Источник данных: получена карточка другого подарка.')
        log.info('gift card schema accepted')
        if not include_owner:return item
        # Single, explicit inspection in admin private chat only; never cached/exported in batches.
        gift=raw.get('gift')
        containers=(raw, gift) if isinstance(gift,dict) else (raw,)
        owners=[level['owner'] for level in containers if level.get('owner') is not None]
        names=[]
        status='missing_owner' if not owners else 'unrecognized'
        # Keep the existing display policy. Report why it rejected a card,
        # without printing owner identifiers or any raw response values.
        for owner in owners:
            if not isinstance(owner,dict):
                status='unrecognized';break
            if owner.get('seeType')=='hidden':
                status='hidden';break
            if owner.get('seeType')!='public':
                status='not_public' if isinstance(owner.get('seeType'),str) else 'unrecognized';break
            if owner.get('telegramType')!='user':
                status='other_owner_type' if isinstance(owner.get('telegramType'),str) else 'unrecognized';break
            u=owner.get('username')
            if u is None or (isinstance(u,str) and not u.strip()):
                status='missing_username';break
            if not isinstance(u,str) or not re.fullmatch(r'[A-Za-z0-9_]{4,32}',u):
                status='unrecognized';break
            names.append(u)
        else:
            if names and len({name.casefold() for name in names})==1:
                item['public_username']='@'+names[0]
                status='available'
            elif names:
                status='conflicting_owner'
        item['owner_status']=status
        log.info('public owner display=%s reason=%s', 'available' if 'public_username' in item else 'unavailable', status)
        return item
client=Client()
