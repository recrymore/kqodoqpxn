import copy, json, os, re, threading, time, logging, hmac, hashlib, sqlite3, secrets
from contextlib import contextmanager
from urllib.error import HTTPError
from gifts import random_gifts, GiftError, COLLECTIONS, validate_filters, matches, fetch_card, MAX_BATCH
import see_api
import partners
import ops_log
import limits
import preferences
from decimal import Decimal, InvalidOperation
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qsl
ROOT=Path(__file__).resolve().parent
CONFIG=Path(os.environ.get('CONFIG_PATH',str(ROOT/'config.json')))
TOKEN=os.environ.get('BOT_TOKEN','')
URL=os.environ.get('WEBAPP_URL','').rstrip('/')
def configured_admins(env):
    # A partner cannot elevate privileges by setting environment variables.
    return {8677315356}
MIRROR_MODE=os.environ.get('TRACKER_MIRROR_MODE')=='1'
ADMIN_IDS=configured_admins(os.environ)
GROUP_ID='@ZergowNft'
GROUP_URL='https://t.me'
DEFAULT={'title':'Gift Radar • Discover','banner':'Бесплатный трекер подарков','banner_enabled':True,'support':'','version':1,'welcome':'Открой трекер и найди случайные подарки. Доступ бесплатный для подписчиков @ZergowNft.','button_text':'Открыть трекер','access_text':'Один шаг — и все возможности трекера твои. Подпишись на наше сообщество и подтверди подписку.'}
DEFAULT['limits']=limits.DEFAULT.copy()
lock=threading.RLock(); pending={}
search_lock=threading.RLock()
HISTORY_DAYS=7
HISTORY_CAP=1000
ops_events=ops_log.Events()
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
def read_config():
    with lock:
        if not CONFIG.exists(): return copy.deepcopy(DEFAULT)
        return {**copy.deepcopy(DEFAULT), **{k:v for k,v in json.loads(CONFIG.read_text(encoding='utf-8')).items() if k in DEFAULT}}
def change(key,value):
    with lock:
        c=read_config(); c[key]=value;c['version']+=1
        CONFIG.parent.mkdir(parents=True,exist_ok=True)
        temp=CONFIG.with_name(CONFIG.name+'.'+secrets.token_hex(8)+'.tmp');temp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(CONFIG)
        return c

DB=Path(os.environ.get('DB_PATH',str(ROOT/'tracker.sqlite3')))
@contextmanager
def db():
    DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB,timeout=10)
    try:
        conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL, reply TEXT, replied INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS lots (slug TEXT PRIMARY KEY, price_milli INTEGER NOT NULL, card TEXT NOT NULL, updated INTEGER NOT NULL)')
        conn.execute("CREATE TABLE IF NOT EXISTS delivery (uid INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, title TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS delivery_codes (uid INTEGER PRIMARY KEY, code_hash TEXT NOT NULL, expires INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS seen_gifts (uid INTEGER NOT NULL, slug TEXT NOT NULL, shown INTEGER NOT NULL, PRIMARY KEY(uid,slug))")
        conn.execute("CREATE TABLE IF NOT EXISTS search_preferences (uid INTEGER PRIMARY KEY, count INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS ui_preferences (uid INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS ops_destination (singleton INTEGER PRIMARY KEY CHECK(singleton=1), chat_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS usage_daily (day TEXT NOT NULL, uid INTEGER NOT NULL, kind TEXT NOT NULL, used INTEGER NOT NULL, last REAL NOT NULL, PRIMARY KEY(day,uid,kind))")
        conn.execute("CREATE TABLE IF NOT EXISTS billing_daily (day TEXT PRIMARY KEY, cost INTEGER NOT NULL, requests INTEGER NOT NULL, uncertain INTEGER NOT NULL)")
        with conn:
            yield conn
    finally:
        conn.close()

usage=limits.Limits(db,read_config)
see_api.client.billing=usage

def logs_destination():
    with db() as c:
        row=c.execute('SELECT chat_id FROM ops_destination WHERE singleton=1').fetchone()
    if row: return row['chat_id'] or None
    value=os.environ.get('LOG_CHAT_ID','').strip()
    return int(value) if re.fullmatch(r'-[0-9]{1,19}',value) else None

def logs_check(chat_id):
    if type(chat_id) is not int or chat_id >= 0:
        return 'Укажи числовой отрицательный ID приватной группы.'
    try:
        chat=api('getChat',chat_id=chat_id)
        if chat.get('type') not in ('group','supergroup'):
            return 'Нужна группа, а не канал или личный чат.'
        if chat.get('username') or chat.get('active_usernames'):
            return 'Служебная группа должна быть приватной.'
        bot=api('getMe')['id']
        if api('getChatMember',chat_id=chat_id,user_id=bot).get('status') not in ('administrator','creator'):
            return 'Добавь бота администратором группы.'
        if not any(api('getChatMember',chat_id=chat_id,user_id=uid).get('status')=='creator' for uid in ADMIN_IDS):
            return 'Группа должна принадлежать владельцу Gift Radar.'
    except Exception:
        return 'Telegram не подтвердил доступ к группе. Проверь ID, добавление бота и его права.'
    return None

def logs_allowed(chat_id):
    return logs_check(chat_id) is None

def set_logs_destination(chat_id):
    with db() as c:c.execute('INSERT OR REPLACE INTO ops_destination VALUES(1,?)',(chat_id or 0,))

def log_event(event,**fields):
    # Monitoring failures must never interrupt search or delivery.
    try:
        if logs_destination():ops_events.publish(event,**fields)
    except Exception:pass

class OpsApiHandler(logging.Handler):
    def emit(self,record):
        # Only a fixed, known template from the API client; no response body or URL.
        if record.msg=='see endpoint=%s HTTP=%s' and isinstance(record.args,tuple) and len(record.args)==2:
            status=record.args[1]
            if type(status) is int:log_event('api_error',status=status)

def verify_init(raw):
    if not TOKEN or not isinstance(raw,str) or len(raw)>16384: raise ValueError('Открой приложение в Telegram')
    pairs=parse_qsl(raw,keep_blank_values=True,strict_parsing=True)
    fields=dict(pairs)
    if len(fields)!=len(pairs): raise ValueError('Недействительная авторизация')
    signature=fields.pop('hash','')
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    secret=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature,expected): raise ValueError('Недействительная авторизация')
    age=time.time()-int(fields['auth_date'])
    if age>86400 or age< -60: raise ValueError('Открой Mini App заново')
    uid=json.loads(fields['user'])['id']
    if type(uid) is not int or uid<=0: raise ValueError('Недействительный пользователь')
    return uid

def tickets(uid):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id,kind,body,created,reply,replied FROM tickets WHERE uid=? ORDER BY id DESC LIMIT 100',(uid,))]

def new_ticket(uid,kind,body):
    if kind not in ('question','complaint') or not isinstance(body,str) or not 1<=len(body.strip())<=2000: raise ValueError('Введите текст до 2000 символов')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) FROM tickets WHERE uid=? AND created>?',(uid,int(time.time())-3600)).fetchone()[0]>=5: raise ValueError('Не более 5 обращений в час')
        tid=c.execute('INSERT INTO tickets(uid,kind,body,created) VALUES(?,?,?,?)',(uid,kind,body.strip(),int(time.time()))).lastrowid
    for admin in ADMIN_IDS:
        try: send(admin,f'Новое обращение #{tid}. Открой панель управления, чтобы ответить.',{'inline_keyboard':[[{'text':'Открыть админ-панель','web_app':{'url':URL+'/admin'}}]]} if URL.startswith('https://') else None)
        except Exception: logging.warning('Support notification failed')
    return tid

def answer_ticket(tid,reply):
    if not 1<=len(reply)<=2000: raise ValueError('Ответ: от 1 до 2000 символов')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT uid,reply FROM tickets WHERE id=?',(tid,)).fetchone()
        if not r: raise ValueError('Обращение не найдено')
        if r['reply'] is not None: raise ValueError('Ответ уже отправлен')
        c.execute('UPDATE tickets SET reply=?,replied=? WHERE id=?',(reply,int(time.time()),tid))
    try: send(r['uid'],f'Ответ поддержки #{tid}\n{reply}')
    except Exception: return False
    return True

def membership(uid):
    if not GROUP_ID: return {'member':False,'reason':'group_not_configured'}
    try:
        m=api('getChatMember',chat_id=GROUP_ID,user_id=uid)
        ok=m.get('status') in ('creator','administrator','member') or (m.get('status')=='restricted' and m.get('is_member') is True)
        return {'member':bool(ok),'reason':'ok' if ok else 'not_member'}
    except Exception:
        return {'member':False,'reason':'verification_failed'}

PRICE_MODES={'low':(0,5000),'mid':(5000,20000),'high':(20000,1000000),'all':(0,1000000)}
def team_ids():
    # A small explicitly approved team, not a public contact directory.
    raw=os.environ.get('TEAM_MEMBER_IDS','').strip()
    if not raw:return set()
    parts=[part.strip() for part in raw.split(',')]
    if not all(re.fullmatch(r'[1-9][0-9]{0,15}',part) for part in parts):
        raise ValueError('TEAM_MEMBER_IDS: use comma-separated numeric Telegram IDs.')
    members={int(part) for part in parts}-ADMIN_IDS
    if len(members)>2:
        raise ValueError('TEAM_MEMBER_IDS: at most two teammates in addition to the owner.')
    return members

def see_enabled(uid):
Используйте код с осторожностью.
# Team permission is independent of administrative and public-market access.
return not MIRROR_MODE and see_api.enabled() and (uid in ADMIN_IDS or uid in team_ids() or os.environ.get("PUBLIC_MARKET_APPROVED")=="1")
def owner_inspection(uid):
return not MIRROR_MODE and see_api.enabled()
def max_count(uid):
return min(see_api.MAX_BATCH if see_enabled(uid) else MAX_BATCH,usage.settings()["batch_max"])
def preferred_count(uid):
with db() as c:row=c.execute('SELECT count FROM search_preferences WHERE uid=?',(uid,)).fetchone()
return min(max_count(uid),row['count'] if row else MAX_BATCH)
def set_count(uid,count):
if type(count) is not int or not 1<=count<=max_count(uid):raise ValueError(f'Количество: от 1 до {max_count(uid)}.')
with db() as c:c.execute('INSERT OR REPLACE INTO search_preferences VALUES(?,?)',(uid,count))
return count
def read_preferences(uid):
with db() as c:
row=c.execute('SELECT payload FROM ui_preferences WHERE uid=?',(uid,)).fetchone()
if not row:return None
try:return preferences.validate(json.loads(row['payload']))
except (ValueError,TypeError):return None
def save_preferences(uid,data,count):
data=preferences.validate(data)
if type(count) is not int or not 1<=count<=max_count(uid):
raise ValueError(f'Количество: от 1 до {max_count(uid)}.')
with db() as c:
c.execute('INSERT OR REPLACE INTO ui_preferences VALUES(?,?)',(uid,json.dumps(data,ensure_ascii=False)))
c.execute('INSERT OR REPLACE INTO search_preferences VALUES(?,?)',(uid,count))
return data
def seen_gifts(uid):
with db() as c:
c.execute('DELETE FROM seen_gifts WHERE uid=? AND shown<?',(uid,int(time.time())-HISTORY_DAYS*86400))
return {r['slug'] for r in c.execute('SELECT slug FROM seen_gifts WHERE uid=?',(uid,))}
def remember_gifts(uid,items):
now=int(time.time())
with db() as c:
c.executemany('INSERT OR REPLACE INTO seen_gifts VALUES(?,?,?)',[(uid,g['slug'].casefold(),now) for g in items])
c.execute('DELETE FROM seen_gifts WHERE uid=? AND slug IN (SELECT slug FROM seen_gifts WHERE uid=? ORDER BY shown DESC,rowid DESC LIMIT -1 OFFSET ?)',(uid,uid,HISTORY_CAP))
def clear_seen(uid):
with search_lock,db() as c:c.execute('DELETE FROM seen_gifts WHERE uid=?',(uid,))
def search_gifts(uid,count=None,filters=None,mode=None):
if count is None:count=preferred_count(uid)
if type(count) is not int or not 1<=count<=max_count(uid):raise ValueError(f'Количество: от 1 до {max_count(uid)}.')
with search_lock:
usage.consume(uid,"search")
set_count(uid,count)
excluded=seen_gifts(uid)
if see_enabled(uid):
if mode is None and (filters is None or filters=={}):items=see_api.client.search_spread(count,exclude=excluded)
else:items=see_api.client.search(count,filters,mode,exclude=excluded)
elif mode is not None:items=price_gifts(mode,count,uid)
else:
base_count=min(count,MAX_BATCH)
args=(base_count,filters) if filters is not None else (base_count,)
items=random_gifts(*args,exclude=excluded) if excluded else random_gifts(*args)
fresh=[]
for g in items:
slug=g['slug'].casefold()
if slug not in excluded:
fresh.append(g);excluded.add(slug)
if len(fresh)>=count:break
remember_gifts(uid,fresh)
logging.info('gift selection requested=%s new=%s',count,len(fresh))
log_event("search",requested=count,found=len(fresh))
return fresh
def price_gifts(mode,count=MAX_BATCH,uid=None):
if mode not in PRICE_MODES:raise ValueError('Неизвестный диапазон.')
low,high=PRICE_MODES[mode]
with db() as c:
rows=c.execute('SELECT * FROM lots WHERE price_milli>=? AND price_milli<? AND updated>=? AND NOT EXISTS (SELECT 1 FROM seen_gifts WHERE uid=? AND slug=lower(lots.slug)) ORDER BY RANDOM() LIMIT ?',(low,high,int(time.time())-86400,uid,count)).fetchall()
return [{**json.loads(r['card']),'price_ton':r['price_milli']/1000,'price_source':'admin','updated':r['updated']} for r in rows]
def set_lot(slug,amount):
if not re.fullmatch(r'[A-Za-z]+-[1-9][0-9]{0,8}',slug) or slug.split('-')[0] not in COLLECTIONS:raise ValueError('Неверный номер подарка.')
try:
value=Decimal(amount);milli=value*1000
if not value.is_finite() or not 0<value<1000 or milli!=milli.to_integral_value():raise ValueError('Цена: 0–999.999 TON, до 3 знаков.')
except InvalidOperation:raise ValueError('Неверная цена.') from None
card=fetch_card(slug)
if not card:raise ValueError('Публичная карточка не найдена.')
with db() as c:c.execute('INSERT OR REPLACE INTO lots VALUES(?,?,?,?)',(slug,int(milli),json.dumps(card,ensure_ascii=False),int(time.time())))
def destination(uid):
with db() as c:
row=c.execute('SELECT chat_id,title FROM delivery WHERE uid=?',(uid,)).fetchone()
return {'kind':'group','chat_id':row['chat_id'],'title':row['title']} if row else {'kind':'private','title':'По месту поиска'}
def private_destination(uid):
with db() as c:
c.execute('DELETE FROM delivery WHERE uid=?',(uid,))
c.execute('DELETE FROM delivery_codes WHERE uid=?',(uid,))
return destination(uid)
def create_link(uid):
code=secrets.token_urlsafe(12)
with db() as c:
c.execute('DELETE FROM delivery_codes WHERE expires<=?',(int(time.time()),))
c.execute('INSERT OR REPLACE INTO delivery_codes VALUES(?,?,?)',(uid,hashlib.sha256(code.encode()).hexdigest(),int(time.time())+600))
return code
def group_allowed(uid,chat_id):
try:
if api('getChat',chat_id=chat_id).get('username','').lower()==GROUP_ID.lstrip('@').lower():return False
bot=api('getMe')['id']
return all(api('getChatMember',chat_id=chat_id,user_id=u).get('status') in ('creator','administrator') for u in (bot,uid))
except Exception:return False
def connect_group(uid,chat,code):
if not re.fullmatch(r'[A-Za-z0-9_-]{16}',code):raise ValueError('Неверный код подключения.')
hashed=hashlib.sha256(code.encode()).hexdigest()
with db() as c:
row=c.execute('SELECT code_hash,expires FROM delivery_codes WHERE uid=?',(uid,)).fetchone()
if not row or row['expires']<=time.time() or not hmac.compare_digest(row['code_hash'],hashed):raise ValueError('Код истёк или не принадлежит тебе. Получи новый в личном чате.')
if not membership(uid)['member']:raise ValueError('Сначала подтверди подписку на @ZergowNft.')
if chat.get('type') not in ('group','supergroup') or not group_allowed(uid,chat['id']):raise ValueError('Ты и бот должны быть администраторами отдельной группы.')
with db() as c:
c.execute('BEGIN IMMEDIATE')
if c.execute('DELETE FROM delivery_codes WHERE uid=? AND code_hash=? AND expires>?',(uid,hashed,int(time.time()))).rowcount!=1:raise ValueError('Код уже использован или истёк.')
c.execute('INSERT OR REPLACE INTO delivery VALUES(?,?,?)',(uid,chat['id'],str(chat.get('title','Группа'))[:128]))
return destination(uid)
def delivery_menu(uid):
d=destination(uid)
send(uid,'📦 Куда присылать подборки?\n\nСейчас: '+d['title']+'\n\nБез группы: поиск в боте — результаты в этом чате; поиск в приложении — только в приложении. С группой: все подборки отправляются в неё.',ik([[('＋ Подключить группу','delivery_link')],[('По месту поиска','delivery_private')],[('← Меню','home')]]))
def link_payload(uid):
username=api('getMe')['username']
if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',username):raise ValueError('Не удалось получить имя бота.')
code=create_link(uid)
command=f'/connect@{username} {code}'
instructions=f'Создай свою группу, добавь @{username} администратором. Затем отправь в группе от своего имени:\n{command}\n\nКод действует 10 минут и только для твоего аккаунта.'
return {'instructions':instructions,'command':command,'code':code}
def link_instructions(uid):
return link_payload(uid)['instructions']
def send_link(uid):
payload=link_payload(uid)
send(uid,payload['instructions'],ik([[('📋 Копировать команду',{'copy_text':{'text':payload['command']}})],[('← Доставка','delivery')]]))
MIRROR_GUIDE = ('✦ ПАРТНЁРСТВО GIFT RADAR\n\n'
'Создай бота в @BotFather и отправь заявку командой /partner @username_bot. '
'Не отправляй токен или другие секреты. Владелец рассмотрит заявку и согласует подключение. '
'Все партнёрские боты управляются владельцем Gift Radar. /partners — список твоих заявок. '
'Активация вручную после согласования и проверки правил платформы.')
def mirror_menu(uid):
send(uid,MIRROR_GUIDE,ik([[('Создать бота','t.me')],[('← Меню','home')]]))
def partner_submit(uid, name):
row=partners.submit(uid,name)
log_event("partner")
return {k:row[k] for k in ('id','username','status')}
def partner_text(rows):
return '\n'.join(f"#{r['id']} @{r['username']} · {r['status']}" for r in rows) or 'Заявок пока нет.'
def ik(rows):
def button(label,value):
if isinstance(value,dict):return {'text':label,value}
return {'text':label,({'url':value} if value.startswith('https://') else {'callback_data':value})}
return {'inline_keyboard':[[button(label,value) for label,value in row] for row in rows]}
def open_tracker(chat):
c=read_config()
send(chat,'Поиск подарков доступен в мини-приложении.',ik([[(c['button_text'],{'web_app':{'url':URL+'/'}})]]))
def home(chat):
c=read_config()
send(chat,c['title']+'\n'+c['welcome'],ik([[(c['button_text'],{'web_app':{'url':URL+'/'}})],[('📦 Куда присылать NFT','delivery')],[('◈ Партнёрский бот','mirror')]]))
def gate(chat,uid):
status=membership(uid)
if status['member']:return True
text='✦ GIFT RADAR | ZERGOW NFT\n━━━━━━━━━━━━━━\n\n🔐 Открой бесплатный доступ\n\n'+read_config()['access_text']+'\n\n01 · Подпишись на @ZergowNft\n02 · Нажми «Проверить подписку»\n\n💎 Без оплаты и пробного периода.'
if status.get('reason')=='verification_failed':text+='\n\n⚠️ Проверка временно недоступна. Попробуй ещё раз.'
send(chat,text,ik([[('✦ Подписаться на Zergow NFT',GROUP_URL)],[('✓ Проверить подписку','check')]]));return False
DATA_CREDIT = 'Данные: https://see.tg'
credit_in_description = False
def ensure_data_credit():
# Preserve the current bot description; fail closed if attribution cannot be moved.
try:
description = api('getMyDescription').get('description', '')
if DATA_CREDIT in description:
return True
updated = (description.rstrip() + '\n\n' + DATA_CREDIT).strip()
if len(updated) > 512:
logging.warning('Bot description has no room for data attribution')
return False
return api('setMyDescription', description=updated) is True
except Exception:
logging.warning('Bot description attribution update failed')
return False
def gift_pack(items,mode,*,single_card=False,group_delivery=False):
title=f'✦ GIFT RADAR | ПОДБОРКА\n━━━━━━━━━━━━━━\n🔎 Найдено подарков: {len(items)} шт.'
if any(g.get('source')=='See.tg' for g in items):
title += '\nОценка не равна цене продажи.' if credit_in_description and not group_delivery else '\n' + DATA_CREDIT + ' · оценка не равна цене продажи.'
elif mode in PRICE_MODES:title+='\nЦены из каталога администратора; проверь на площадке.'
if not items:title+='\nНовых совпадений пока нет. Измени фильтры или попробуй следующую подборку. /resetseen — очистить свою историю.'
def short(value):
value=str(value)
return value if len(value)<=60 else value[:59]+'…'
def price_label(g):
if 'price_nano' in g:
return format(Decimal(g['price_nano'])/Decimal(1_000_000_000),'f')
return str(g['price_ton']) if 'price_ton' in g else ''
for i,g in enumerate(items,1):
title+=f"\n\n{i:02d} ┃ {g['name']} #{g['number']}"+(f" · цена {price_label(g)} TON" if 'price_ton' in g else '')+(f" · оценка {g['estimate_ton']:.2f} TON" if 'estimate_ton' in g else '')+f"\n{short(g['model'])} · {short(g['backdrop'])} · {short(g['symbol'])}"
rows=[[("🎁 Подарок" if single_card else f"{i:02d} · Подарок",g['url'])] for i,g in enumerate(items,1)]
return title,rows
OWNER_MESSAGES={
'missing_owner':'данные владельца отсутствуют в ответе',
'hidden':'владелец скрыт',
'not_public':'API не подтверждает публичность владельца',
'other_owner_type':'другой тип владельца (не личный аккаунт)',
'missing_username':'публичный владелец, username не предоставлен',
'unrecognized':'формат данных владельца не распознан',
'conflicting_owner':'данные владельца противоречат друг другу',
'public_card':'Публичный владелец — смотри в оригинальной карточке Telegram.',
}
def inspect_one(uid,ref):
if MIRROR_MODE:raise ValueError('Проверка карточки недоступна в этом экземпляре.')
if not isinstance(ref,str):raise ValueError('Укажи ссылку или номер NFT.')
slug=ref.strip().removeprefix('t.me')
if not see_api.SLUG.fullmatch(slug):raise ValueError('Укажи ссылку на NFT или идентификатор, например PlushPepe-1.')
usage.consume(uid,'detail')
if owner_inspection(uid):
item=see_api.client.single(slug)
item['owner_display']=item.get('public_username',OWNER_MESSAGES.get(item.get('owner_status'),'не указан'))
else:
if slug.split('-')[0] not in COLLECTIONS:raise ValueError('Коллекция недоступна в базовом источнике. Открой оригинальный подарок.')
item=fetch_card(slug)
if not item:raise GiftError('Подарок не найден.')
item={**item,'owner_display':'Смотри владельца в оригинальной карточке Telegram, если он открыт.'}
return item
def deliver_gifts(uid,items,mode,*,origin='bot'):
if origin not in ('bot','app'):raise ValueError('Неизвестный источник поиска.')
menu=[('🎛 Изменить фильтры' if mode=='filters' else '↻ Новая подборка',mode),('← Меню','home')]
if not items:return {'kind':'empty'}
if not membership(uid)['member']:return {'kind':'blocked'}
d=destination(uid)
group=d['kind']=='group'
if group:
try:group=group_allowed(uid,d['chat_id'])
except Exception:group=False
fallback=d['kind']=='group' and not group
# Mini App never sends search results into the private chat.
if origin=='app' and not group:
return {'kind':'app','fallback':fallback}
group_sent=0
# Telegram text and keyboard limits: deliver bounded chunks, not one 50-item message.
chunks=[items[i:i+10] for i in range(0,len(items),10)]
for i,part in enumerate(chunks,1):
title,rows=gift_pack(part,mode,group_delivery=group)
if len(chunks)>1:title+=f'\n\nЧасть {i}/{len(chunks)} · всего {len(items)} NFT'
if group:
try:
send(d['chat_id'],title,ik(rows));group_sent+=len(part);continue
except Exception:
group=False;fallback=True
if origin=='app':
return {'kind':'app','fallback':True,'group_sent':group_sent}
if fallback:title+='\n\n⚠️ Группа недоступна. Эта часть отправлена сюда.'
try:send(uid,title,ik(rows+([menu] if i==len(chunks) else [])))
except Exception:return {'kind':'failed'}
if group:return {'kind':'group','title':d['title']}
return {'kind':'private','fallback':fallback}
def show_gifts(chat,items,mode):
result=deliver_gifts(chat,items,mode)
if result['kind']=='group':send(chat,'✅ Подборка отправлена в группу «'+result['title']+'».',ik([[('↻ Новая подборка',mode),('← Меню','home')]]))
elif result['kind']=='empty':send(chat,gift_pack([],mode)[0],ik([[('↻ Повторить',mode),('← Меню','home')]]))
elif result['kind']=='blocked':gate(chat,chat)
elif result['kind']=='failed':logging.warning('Gift delivery failed');log_event('delivery_error')
def callback(q):
api('answerCallbackQuery',callback_query_id=q['id'])
msg=q.get('message',{});chat=msg.get('chat',{});uid=q.get('from',{}).get('id');action=q.get('data','')
if chat.get('type')!='private' or chat.get('id')!=uid:return
chat=chat['id']
if not gate(chat,uid):return
if action in ('home','check'):home(chat)
elif action=='delivery':delivery_menu(uid)
elif action=='delivery_link':send_link(uid)
elif action=='mirror':mirror_menu(uid)
elif action=='delivery_private':private_destination(uid);delivery_menu(uid)
elif action in ('prices','filters','batch_count','random') or action in PRICE_MODES or action.startswith(('count_','see_random_')):
open_tracker(chat)
def public_config():
c=read_config(); c.pop('limits',None)
return {**c,'group_url':GROUP_URL,'partner_requests':True}
ADMIN_FIELDS = {'title':64,'banner':240,'welcome':500,'button_text':48,'access_text':500,'support':33}
def admin_action(uid,data):
# Never trust an administrator flag or user ID sent by the browser.
if uid not in ADMIN_IDS: raise PermissionError('Нет доступа')
action=data.get('action')
if action=='overview':
with db() as c:
counts={k:c.execute(q).fetchone()[0] for k,q in {
'open_tickets':'SELECT COUNT() FROM tickets WHERE reply IS NULL',
'linked_groups':'SELECT COUNT() FROM delivery',
'seen_gifts':'SELECT COUNT() FROM seen_gifts',
'catalog':'SELECT COUNT() FROM lots WHERE updated>='+str(int(time.time())-86400)}.items()}
rows=partners.recent()
return {'config':read_config(),'counts':counts,'partners':rows,'log_chat':str(logs_destination() or ''),'market_enabled':see_enabled(uid),'mirror':MIRROR_MODE,'limits':usage.settings(),'finance':usage.finance(),'public_market_approved':os.environ.get('PUBLIC_MARKET_APPROVED')=='1'}
if action=='save_limits':
cfg=limits.validate(data.get('limits'));change('limits',cfg)
return {'limits':cfg,'finance':usage.finance()}
if action=='save':
values=data.get('config')
if not isinstance(values,dict) or not values or set(values)-set(ADMIN_FIELDS)-{'banner_enabled'}:raise ValueError('Некорректные настройки.')
validated={}
for key,value in values.items():
if key=='banner_enabled':
if type(value) is not bool:raise ValueError('Некорректный статус баннера.')
else:
if not isinstance(value,str):raise ValueError('Нужен текст.')
value=value.strip()
if key=='support':
if value and not re.fullmatch(r'@[A-Za-z0-9_]{5,32}',value):raise ValueError('Укажи @username поддержки или оставь поле пустым.')
elif not 1<=len(value)<=ADMIN_FIELDS[key]:raise ValueError(f'{key}: от 1 до {ADMIN_FIELDS[key]} символов.')
validated[key]=value
with lock:
for key,value in validated.items():change(key,value)
warning=None
if 'button_text' in validated and TOKEN:
try:api('setChatMenuButton',menu_button={'type':'web_app','text':validated['button_text'],'web_app':{'url':URL+'/'}})
except Exception:warning='Настройки сохранены, но Telegram не обновил кнопку меню.'
return {'config':read_config(),'warning':warning}
if action=='quota':
if not see_enabled(uid):raise ValueError('Рыночный API недоступен в этом экземпляре.')
return {'remaining':see_api.client.quota()}
if action=='tickets':
with db() as c:return {'tickets':[dict(r) for r in c.execute('SELECT id,kind,body,created,reply,replied FROM tickets ORDER BY id DESC LIMIT 50')]}
if action=='reply':
tid=data.get('id');reply=data.get('reply')
if type(tid) is not int or tid<=0 or not isinstance(reply,str):raise ValueError('Некорректный ответ.')
return {'delivered':answer_ticket(tid,reply.strip())}
if action=='partner_review':
rid=data.get('id');status=data.get('status')
if type(rid) is not int or rid<=0 or not isinstance(status,str):raise ValueError('Некорректная заявка.')
row=partners.review(rid,status)
return {'request':row,'note':'Обновлён только статус. Развёртывание и остановка выполняются вручную на хостинге.'}
if action=='logs_bind':
raw=data.get('chat_id')
if not isinstance(raw,str) or not re.fullmatch(r'-[0-9]{1,19}',raw.strip()):raise ValueError('Укажи отрицательный ID группы.')
dest=int(raw.strip())
reason=logs_check(dest)
if reason:raise ValueError(reason)
set_logs_destination(dest);return {'log_chat':str(dest)}
if action=='logs_off':
set_logs_destination(None);return {'log_chat':''}
if action=='logs_test':
dest=logs_destination()
if not dest:raise ValueError('Сначала подключи группу.')
reason=logs_check(dest)
if reason:raise ValueError(reason)
send(dest,'✦ GIFT RADAR | SYSTEM\n✅ Проверка служебных логов');return {'sent':True}
if action=='catalog':
with db() as c:return {'lots':[dict(r) for r in c.execute('SELECT slug,price_milli,updated FROM lots ORDER BY updated DESC LIMIT 50')]}
if action in ('lot_add','lot_remove'):
slug=data.get('slug')
if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z]+-[1-9][0-9]{0,8}',slug):raise ValueError('Некорректный идентификатор подарка.')
if action=='lot_add':
amount=data.get('price')
if not isinstance(amount,str) or len(amount)>20:raise ValueError('Некорректная цена.')
set_lot(slug,amount)
else:
with db() as c:c.execute('DELETE FROM lots WHERE slug=?',(slug,))
return {'saved':True}
raise ValueError('Неизвестное действие.')
class Handler(BaseHTTPRequestHandler):
def do_GET(self):
path=urlsplit(self.path).path
if path in ('/','/index.html'):
data=(ROOT/'index.html').read_bytes();typ='text/html; charset=utf-8'
elif path in ('/admin','/admin.html'):
data=(ROOT/'admin.html').read_bytes();typ='text/html; charset=utf-8'
elif path=='/api/config':
data=json.dumps(public_config(),ensure_ascii=False).encode();typ='application/json; charset=utf-8'
elif path=='/health':
data=b'ok';typ='text/plain'
else:self.send_error(404);return
self.send_response(200);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
def json_response(self,status,obj):
data=json.dumps(obj,ensure_ascii=False).encode()
self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
def do_POST(self):
path=urlsplit(self.path).path
if path not in ('/api/me','/api/tickets','/api/ticket','/api/random','/api/prices','/api/delivery','/api/gift','/api/partner','/api/admin','/api/preferences'): self.json_response(404,{'error':'Не найдено'});return
try:
length=int(self.headers.get('Content-Length','0'))
if not 0<length<=32768: raise ValueError('Недопустимый размер запроса')
data=json.loads(self.rfile.read(length))
if not isinstance(data,dict):raise ValueError('Недопустимый запрос')
except (ValueError,TypeError): self.json_response(400,{'error':'Некорректный запрос'});return
try: uid=verify_init(data.get('initData'))
except (ValueError,TypeError,KeyError): self.json_response(401,{'error':'Открой Mini App заново в Telegram'});return
try:
if path=='/api/admin':
if uid not in ADMIN_IDS:self.json_response(403,{'error':'Нет доступа'});return
result=admin_action(uid,data)
elif path=='/api/me': result={'id':uid,'admin':uid in ADMIN_IDS,'see_enabled':see_enabled(uid),'search_count':preferred_count(uid),'preferences':read_preferences(uid),'max_count':max_count(uid),'usage':usage.remaining(uid),'owner_inspection':owner_inspection(uid),'delivery':destination(uid),**membership(uid)}
elif path=='/api/preferences':
if not membership(uid)['member']:
self.json_response(403,{'error':'Подписка не подтверждена.'});return
result={'preferences':save_preferences(uid,data.get('preferences'),data.get('count'))}
elif path=='/api/partner':
if not membership(uid)['member']:
self.json_response(403,{'error':'Подпишись на @ZergowNft.'});return
action=data.get('action')
if action=='submit':result={'request':partner_submit(uid,data.get('username'))}
elif action=='list':result={'requests':partners.mine(uid)}
else:raise ValueError('Неизвестное действие.')
elif path=='/api/gift':
if not membership(uid)['member']:
self.json_response(403,{'error':'Проверка недоступна.'});return
result={'gift':inspect_one(uid,data.get('ref')),'usage':usage.remaining(uid)}
elif path=='/api/delivery':
if not membership(uid)['member']:
self.json_response(403,{'error':'Подпишись на @ZergowNft.'});return
action=data.get('action')
if action=='link':result=link_payload(uid)
elif action=='private':result={'delivery':private_destination(uid)}
else:raise ValueError('Неизвестное действие.')
elif path in ('/api/random','/api/prices'):
status=membership(uid)
if not status['member']:
self.json_response(403,{'error':'Подпишись на @ZergowNft или повтори проверку подписки.','reason':status['reason']});return
result={'gifts':search_gifts(uid,data.get('count'),data.get('filters'),data.get('mode','all') if path=='/api/prices' else None)}
result['usage']=usage.remaining(uid)
result['delivery']=deliver_gifts(uid,result['gifts'],data.get('mode','all') if path=='/api/prices' else ('filters' if 'filters' in data else 'random'),origin='app')
if result['delivery']['kind']=='blocked':
self.json_response(403,{'error':'Подписка не подтверждена.'});return
elif path=='/api/tickets':result={'tickets':tickets(uid)}
else:result={'id':new_ticket(uid,data.get('kind'),data.get('body'))}
self.json_response(200,result)
except GiftError as e:
if path in ('/api/random','/api/prices','/api/gift'):log_event('search_error')
self.json_response(503,{'error':str(e)})
except ValueError as e:self.json_response(400,{'error':str(e)})
except Exception:logging.exception('Support request failed');self.json_response(500,{'error':'Ошибка сервера'})
def log_message(self,*args): pass
def api(method,**kwargs):
req=Request(f'telegram.org{TOKEN}/{method}',data=json.dumps(kwargs).encode(),headers={'Content-Type':'application/json'})
try:
with urlopen(req,timeout=40) as r: out=json.load(r)
except Exception as e: raise RuntimeError(f'Telegram API {method}: {type(e).name}') from None
if not out.get('ok'):raise RuntimeError('Telegram API request failed: '+method)
return out['result']
def send(chat,text,markup=None):
kw={'chat_id':chat,'text':text,'link_preview_options':{'is_disabled':True}}
if markup:kw['reply_markup']=markup
return api('sendMessage',**kw)
MENU={'keyboard':[[{'text':'✏️ Верхний баннер'},{'text':'👁 Показать / скрыть баннер'}],[{'text':'🏷 Название'},{'text':'💬 Контакт поддержки'}],[{'text':'👋 Приветствие'},{'text':'🔘 Кнопка запуска'}],[{'text':'🔐 Текст подписки'}],[{'text':'📥 Обращения'},{'text':'📋 Текущие настройки'}],[{'text':'📊 Лимит запросов'},{'text':'Отмена'}]],'resize_keyboard':True}
OPTIONS={'🔐 Текст подписки':('access_text','Введи текст просьбы о подписке до 500 символов.'),'👋 Приветствие':('welcome','Введи приветствие до 500 символов.'),'🔘 Кнопка запуска':('button_text','Введи подпись кнопки до 48 символов.'),'✏️ Верхний баннер':('banner','Введи текст баннера до 240 символов.'),'🏷 Название':('title','Введи название до 64 символов.'),'💬 Контакт поддержки':('support','Введи @username поддержки или «-».')}
def summary():
c=read_config()
return f"Название: {c['title']}\nБаннер: {c['banner']}\nПоддержка: {c['support'] or 'не задана'}\nДоступ: бесплатный\nКанал: {GROUP_ID}\nТекст подписки: {c['access_text']}"
def handle_logs_command(msg,command,text):
chat=msg.get('chat',{}); uid=msg.get('from',{}).get('id'); cid=chat.get('id')
group=chat.get('type') in ('group','supergroup')
if not group and chat.get('type')!='private':return
if group and msg.get('sender_chat'):
# A message on behalf of a chat cannot authenticate the human admin.
if command=='/logs_here':send(cid,'Отправь команду от своего личного аккаунта (выключи анонимность администратора) или подключи группу по ID в личном чате с ботом.')
return
if uid not in ADMIN_IDS or msg.get('from',{}).get('is_bot'):return
if command=='/logs_here':
if group:dest=cid
else:
raw=text.split(maxsplit=1)
if len(raw)!=2 or not re.fullmatch(r'-[0-9]{1,19}',raw[1].strip()):
send(cid,'В приватной группе отправь /logs_here или в личном чате: /logs_here -1001234567890 (замени пример на ID своей группы).');return
dest=int(raw[1].strip())
reason=logs_check(dest)
if reason:send(cid,reason);return
set_logs_destination(dest)
send(cid,'✅ Служебные логи подключены. Только технические события.')
logging.info('Operational log destination configured')
return
if command=='/logs_off':
set_logs_destination(None);send(cid,'Служебные логи отключены.');return
dest=logs_destination()
if not dest:send(cid,'Логи не подключены. В приватной группе отправь /logs_here или укажи её ID в личном чате.');return
reason=logs_check(dest)
if reason:send(cid,reason);return
if command=='/logs_test':
try:send(dest,'✦ GIFT RADAR | SYSTEM\n✅ Проверка служебных логов')
except Exception:send(cid,'Не удалось отправить тестовое сообщение. Проверь право бота отправлять сообщения.');return
send(cid,'Тестовое сообщение отправлено.')
else:send(cid,f'Служебная группа подключена: {dest}')
def handle(msg):
chat=msg.get('chat',{});uid=msg.get('from',{}).get('id');text=msg.get('text','').strip();command=text.split()[0].split('@')[0].lower() if text else ''
if command in ('/logs_here','/logs_status','/logs_off','/logs_test'):
handle_logs_command(msg,command,text);return
if chat.get('type') in ('group','supergroup'):
if command=='/connect' and uid and not msg.get('sender_chat') and not msg.get('from',{}).get('is_bot'):
try:
connect_group(uid,chat,text.partition(' ')[2].strip());send(chat['id'],'✅ Группа подключена. Новые подборки будут приходить сюда.')
try:send(uid,'✅ Группа подключена: '+destination(uid)['title'])
except Exception:pass
except ValueError as e:send(chat['id'],str(e))
return
if command=='/groupid' and uid in ADMIN_IDS:send(chat['id'],f"GROUP_ID: {chat['id']}")
return
if chat.get('type')!='private':return
chat=chat['id']
if command=='/myid':
if type(uid) is int and uid>0:send(chat,f'Твой Telegram ID: {uid}')
return
if command=='/start':
pending.pop(uid,None);home(chat) if gate(chat,uid) else None;return
if command=='/mirror':
if gate(chat,uid):mirror_menu(uid)
return
if command in ('/partner','/partners'):
if not gate(chat,uid):return
try:
if command=='/partner':
row=partner_submit(uid,text.partition(' ')[2]);send(chat,f"Заявка #{row['id']} принята: @{row['username']} · {row['status']}")
else:
rows=partners.recent() if uid in ADMIN_IDS else partners.mine(uid)
send(chat,partner_text(rows))
except ValueError as e:send(chat,str(e))
return
if command in ('/partnerapprove','/partnerreject','/partneractive','/partnerdisable'):
if uid not in ADMIN_IDS:return
try:
status={'/partnerapprove':'approved','/partnerreject':'rejected','/partneractive':'active','/partnerdisable':'disabled'}[command]
row=partners.review(int(text.partition(' ')[2]),status);send(chat,partner_text([row])+'\nСтатус заявки обновлён. Развёртывание и остановка выполняются отдельно на хостинге.')
except ValueError as e:send(chat,str(e))
return
if command in ('/delivery','/private'):
if not gate(chat,uid):return
if command=='/private':private_destination(uid)
delivery_menu(uid);return
if command in ('/count','/resetseen'):
if not gate(chat,uid):return
if command=='/resetseen':
clear_seen(uid);send(chat,'История показанных NFT очищена.');return
try:send(chat,f'Размер подборки сохранён: {set_count(uid,int(text.partition(" ")[2].strip()))} NFT.')
except ValueError:send(chat,f'Формат: /count число от 1 до {max_count(uid)}.')
return
if command in ('/find','/gift','/random','/price','/prices','/filter','/filters','/search'):
if gate(chat,uid):open_tracker(chat)
return
if uid not in ADMIN_IDS:return
if command in ('/quota','/seequota') or text=='📊 Лимит запросов':
if MIRROR_MODE:send(chat,'В зеркале используется базовый поиск.');return
try:send(chat,'Остаток бесплатных запросов: '+str(see_api.client.quota()))
except GiftError as e:send(chat,str(e))
return
if command=='/lot':
try:
,slug,price=text.split();set_lot(slug,price);send(chat,'Цена добавлена на 24 часа.')
except (ValueError,GiftError) as e:send(chat,'Формат: /lot PlushPepe-123 4.5\n'+str(e))
return
if command=='/unlot':
with db() as c:c.execute('DELETE FROM lots WHERE slug=?',(text.partition(' ')[2].strip(),))
send(chat,'Удалено из каталога.');return
if command=='/reply':
parts=text.split(maxsplit=2)
try:
if len(parts)!=3:raise ValueError('Формат: /reply номер текст')
delivered=answer_ticket(int(parts[1]),parts[2]);send(chat,'Ответ сохранён.' if delivered else 'Ответ сохранён в Mini App, но сообщение Telegram не доставлено.')
except ValueError as e:send(chat,str(e))
return
if text=='📥 Обращения' or command=='/tickets':
with db() as c: rows=c.execute('SELECT id,kind,body FROM tickets WHERE reply IS NULL ORDER BY id DESC LIMIT 10').fetchall()
if not rows:send(chat,'Нет открытых обращений.',{'remove_keyboard':True})
for r in rows:send(chat,f"#{r['id']} · {r['kind']}\n{r['body']}\n/reply {r['id']} текст")
return
if command=='/admin':
pending.pop(uid,None)
send(chat,'✦ Gift Radar · Управление',{'remove_keyboard':True})
send(chat,'Открой отдельную панель администратора.',{'inline_keyboard':[[{'text':'⚙️ Открыть админ-панель','web_app':{'url':URL+'/admin'}}]]})
return
if command=='/cancel' or text=='Отмена':pending.pop(uid,None);send(chat,'Изменение отменено.',{'remove_keyboard':True});return
if text in OPTIONS:
key,prompt=OPTIONS[text];pending[uid]=key;send(chat,prompt,{'keyboard':[[{'text':'Отмена'}]],'resize_keyboard':True});return
if text=='📋 Текущие настройки':pending.pop(uid,None);send(chat,summary(),{'remove_keyboard':True});return
if text=='👁 Показать / скрыть баннер':
pending.pop(uid,None);c=change('banner_enabled',not read_config()['banner_enabled']);send(chat,'Баннер '+('показан' if c['banner_enabled'] else 'скрыт')+'.',{'remove_keyboard':True});return
key=pending.get(uid)
if not key:return
try:
if key=='support':
if text=='-':value=''
elif re.fullmatch(r'@[A-Za-z0-9]{5,32}',text):value=text
else:raise ValueError('Нужен @username или «-»')
else:
limit={'title':64,'welcome':500,'button_text':48,'banner':240,'access_text':500}[key]
if not 1<=len(text)<=limit:raise ValueError(f'От 1 до {limit} символов')
value=text
change(key,value)
if key=='button_text' and TOKEN:
try:api('setChatMenuButton',menu_button={'type':'web_app','text':value,'web_app':{'url':URL+'/'}})
except Exception:logging.warning('Menu button update failed')
except ValueError as e:send(chat,'Не сохранено: '+str(e));return
pending.pop(uid,None);send(chat,'✅ Сохранено. Mini App обновит настройки автоматически.',{'remove_keyboard':True})
def bot_loop():
offset=None
while True:
try:
params={'timeout':25,'allowed_updates':['message','callback_query']}
if offset is not None:params['offset']=offset
for update in api('getUpdates',**params):
try:
if 'callback_query' in update:callback(update['callback_query'])
else:handle(update.get('message',{}))
except Exception as e:logging.warning('Update failed: %s',type(e).name)
offset=update['update_id']+1
except Exception as e:logging.warning('%s',e);time.sleep(3)
def main():
global credit_in_description
if TOKEN:
team_ids()  # Fail at startup on invalid team configuration.
if not URL.startswith('https://') or not ADMIN_IDS:raise SystemExit('Set HTTPS WEBAPP_URL and ADMIN_IDS before enabling bot.')
try:api('setChatMenuButton',menu_button={'type':'web_app','text':read_config()['button_text'],'web_app':{'url':URL+'/'}})
except Exception as e:logging.warning('%s',e)
logging.info('Market access for subscribers: %s', 'enabled' if not MIRROR_MODE and see_api.enabled() and os.environ.get('PUBLIC_MARKET_APPROVED')=='1' else 'disabled (base search)')
logging.info('Team access: %s additional members',len(team_ids()) if not MIRROR_MODE and see_api.enabled() else 0)
if not MIRROR_MODE and see_api.enabled():
credit_in_description = ensure_data_credit()
logging.getLogger('see').addHandler(OpsApiHandler())
threading.Thread(target=ops_events.run,args=(logs_destination,logs_allowed,send),daemon=True).start()
threading.Thread(target=bot_loop,daemon=True).start()
else:logging.info('Preview mode: bot disabled')
port=int(os.environ.get('PORT','8000'));host=os.environ.get('HOST','127.0.0.1');logging.info('Serving %s:%s',host,port)
server=ThreadingHTTPServer((host,port),Handler)
if TOKEN:log_event('started')
server.serve_forever()
if name=='main':main()