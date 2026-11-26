"""Design-stage Telegram Mini App, standard-library only. Run: python app.py."""
import copy, json, os, re, threading, time, logging, hmac, hashlib, sqlite3, secrets
from contextlib import contextmanager
from urllib.error import HTTPError
from gifts import random_gifts, GiftError, COLLECTIONS, validate_filters, matches, fetch_card, MAX_BATCH
import see_api
from decimal import Decimal, InvalidOperation
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qsl
ROOT=Path(__file__).resolve().parent
CONFIG=Path(os.environ.get('CONFIG_PATH',str(ROOT/'config.json')))
TOKEN=os.environ.get('BOT_TOKEN','')
URL=os.environ.get('WEBAPP_URL','').rstrip('/')
ADMIN_IDS={8677315356}
GROUP_ID='@ZergowNft'
GROUP_URL='https://t.me/ZergowNft'
DEFAULT={'title':'Gift Radar • Discover','banner':'Бесплатный трекер подарков','banner_enabled':True,'support':'','version':1,'welcome':'Открой трекер и найди случайные подарки. Доступ бесплатный для подписчиков @ZergowNft.','button_text':'Открыть трекер','access_text':'Один шаг — и все возможности трекера твои. Подпишись на наше сообщество и подтверди подписку.'}
lock=threading.RLock(); pending={}
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
def read_config():
    with lock:
        if not CONFIG.exists(): return copy.deepcopy(DEFAULT)
        return {**copy.deepcopy(DEFAULT), **{k:v for k,v in json.loads(CONFIG.read_text(encoding='utf-8')).items() if k in DEFAULT}}
def change(key,value):
    with lock:
        c=read_config(); c[key]=value;c['version']+=1
        CONFIG.parent.mkdir(parents=True,exist_ok=True)
        temp=CONFIG.with_suffix('.tmp');temp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(CONFIG)
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
        with conn:
            yield conn
    finally:
        conn.close()

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
        try: send(admin,f'Обращение #{tid} ({kind})\n{body.strip()}\n\nОтвет: /reply {tid} текст')
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
def see_enabled(uid):
    return uid in ADMIN_IDS and see_api.enabled()

def search_gifts(uid,count=MAX_BATCH,filters=None,mode=None):
    if see_enabled(uid):return see_api.client.search(count,filters,mode)
    if mode is not None:return price_gifts(mode)
    return random_gifts(count,filters) if filters is not None else random_gifts(count)

def price_gifts(mode):
    if mode not in PRICE_MODES:raise ValueError('Неизвестный диапазон.')
    low,high=PRICE_MODES[mode]
    with db() as c:
        rows=c.execute('SELECT * FROM lots WHERE price_milli>=? AND price_milli<? AND updated>=? ORDER BY RANDOM() LIMIT ?',(low,high,int(time.time())-86400,MAX_BATCH)).fetchall()
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
    return {'kind':'group','chat_id':row['chat_id'],'title':row['title']} if row else {'kind':'private','title':'Личный чат с ботом'}

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
    send(uid,'📦 Куда присылать подборки?\n\nСейчас: '+d['title']+'\n\nПо умолчанию результаты приходят одним сообщением в этот чат. Можно подключить свою группу.',ik([[('＋ Подключить группу','delivery_link')],[('💬 В личный чат','delivery_private')],[('← Меню','home')]]))

def link_instructions(uid):
    code=create_link(uid)
    username=api('getMe')['username']
    return f'Создай свою группу, добавь @{username} администратором. Затем отправь в группе от своего имени:\n/connect@{username} {code}\n\nКод действует 10 минут и только для твоего аккаунта.'

def ik(rows):
    def button(label,value):
        if isinstance(value,dict):return {'text':label,**value}
        return {'text':label,**({'url':value} if value.startswith('https://') else {'callback_data':value})}
    return {'inline_keyboard':[[button(label,value) for label,value in row] for row in rows]}
def home(chat):
    c=read_config();send(chat,c['title']+'\n'+c['welcome'],{'inline_keyboard':[[{'text':'🎲 Случайные подарки','callback_data':'random'},{'text':'💎 По цене','callback_data':'prices'}],[{'text':'🎛 Фильтры','callback_data':'filters'}],[{'text':'📦 Куда присылать NFT','callback_data':'delivery'}],[{'text':c['button_text'],'web_app':{'url':URL+'/'}}]]})
def gate(chat,uid):
    status=membership(uid)
    if status['member']:return True
    text='✦ GIFT RADAR | ZERGOW NFT\n━━━━━━━━━━━━━━\n\n🔐 Открой бесплатный доступ\n\n'+read_config()['access_text']+'\n\n01 · Подпишись на @ZergowNft\n02 · Нажми «Проверить подписку»\n\n💎 Без оплаты и пробного периода.'
    if status.get('reason')=='verification_failed':text+='\n\n⚠️ Проверка временно недоступна. Попробуй ещё раз.'
    send(chat,text,ik([[('✦ Подписаться на Zergow NFT',GROUP_URL)],[('✓ Проверить подписку','check')]]));return False

def gift_pack(items,mode):
    title=f'✦ GIFT RADAR | ПОДБОРКА\n━━━━━━━━━━━━━━\n🔎 Найдено подарков: {len(items)} шт.'
    if any(g.get('source')=='See.tg' for g in items):title+='\nИсточник: See.tg · оценка не равна цене продажи.'
    elif mode in PRICE_MODES:title+='\nЦены из каталога администратора; проверь на площадке.'
    if not items:title+='\nСовпадений пока нет. Попробуй другой запрос.'
    def short(value):
        value=str(value)
        return value if len(value)<=60 else value[:59]+'…'
    for i,g in enumerate(items,1):
        title+=f"\n\n{i:02d} ┃ {g['name']} #{g['number']}"+(f" · {g['price_ton']:g} TON" if 'price_ton' in g else '')+(f" · оценка {g['estimate_ton']:.2f} TON" if 'estimate_ton' in g else '')+f"\n{short(g['model'])} · {short(g['backdrop'])} · {short(g['symbol'])}"
    rows=[[(f"{i:02d} · Владелец / NFT",g['url']),("Копировать ссылку",{'copy_text':{'text':g['url']}})] for i,g in enumerate(items,1)]
    return title,rows

def deliver_gifts(uid,items,mode):
    menu=[('🎛 Изменить фильтры' if mode=='filters' else '↻ Новая подборка',mode),('← Меню','home')]
    if not items:return {'kind':'empty'}
    if not membership(uid)['member']:return {'kind':'blocked'}
    d=destination(uid)
    group=d['kind']=='group'
    if group:
        try:group=group_allowed(uid,d['chat_id'])
        except Exception:group=False
    fallback=d['kind']=='group' and not group
    # Telegram text and keyboard limits: deliver bounded chunks, not one 50-item message.
    chunks=[items[i:i+10] for i in range(0,len(items),10)]
    for i,part in enumerate(chunks,1):
        title,rows=gift_pack(part,mode)
        if len(chunks)>1:title+=f'\n\nЧасть {i}/{len(chunks)} · всего {len(items)} NFT'
        if group:
            try:send(d['chat_id'],title,ik(rows));continue
            except Exception:group=False;fallback=True
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
    elif result['kind']=='failed':logging.warning('Gift delivery failed')

def callback(q):
    api('answerCallbackQuery',callback_query_id=q['id'])
    msg=q.get('message',{});chat=msg.get('chat',{});uid=q.get('from',{}).get('id');action=q.get('data','')
    if chat.get('type')!='private' or chat.get('id')!=uid:return
    chat=chat['id']
    if not gate(chat,uid):return
    if action in ('home','check'):home(chat)
    elif action=='delivery':delivery_menu(uid)
    elif action=='delivery_link':send(uid,link_instructions(uid),ik([[('← Доставка','delivery')]]))
    elif action=='delivery_private':private_destination(uid);delivery_menu(uid)
    elif action=='prices':send(chat,'💎 Выбери бюджет в TON.\nДо 5 · 5–20 · 20–999.999',ik([[('До 5','low'),('5–20','mid')],[('20+','high'),('Все цены','all')],[('← Меню','home')]]))
    elif action=='filters':send(chat,'🎛 Фильтры по атрибутам\nВведи, например: /find model=Steel; backdrop=Black; symbol=Star\nКоллекции: '+', '.join(COLLECTIONS)+'\nЛюбое поле можно пропустить.',ik([[('← Меню','home')]]))
    elif action=='random' and see_enabled(uid):
        send(chat,'Выбери размер подборки See.tg (случайная выборка из первой страницы).',ik([[('10 NFT','see_random_10'),('25 NFT','see_random_25'),('50 NFT','see_random_50')],[('← Меню','home')]]))
    elif action in ('see_random_10','see_random_25','see_random_50'):
        if not see_enabled(uid):return
        try:show_gifts(chat,search_gifts(uid,int(action.rsplit('_',1)[1])),'random')
        except (GiftError,ValueError) as e:send(chat,str(e))
    elif action=='random' or action in PRICE_MODES:
        try:show_gifts(chat,search_gifts(uid,MAX_BATCH,mode=None if action=='random' else action),action)
        except (GiftError,ValueError) as e:send(chat,str(e))

def public_config():
    return {**read_config(),'group_url':GROUP_URL}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in ('/','/index.html'):
            data=(ROOT/'index.html').read_bytes();typ='text/html; charset=utf-8'
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
        if path not in ('/api/me','/api/tickets','/api/ticket','/api/random','/api/prices','/api/delivery'): self.json_response(404,{'error':'Не найдено'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768: raise ValueError('Недопустимый размер запроса')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Недопустимый запрос')
        except (ValueError,TypeError): self.json_response(400,{'error':'Некорректный запрос'});return
        try: uid=verify_init(data.get('initData'))
        except (ValueError,TypeError,KeyError): self.json_response(401,{'error':'Открой Mini App заново в Telegram'});return
        try:
            if path=='/api/me': result={'id':uid,'admin':uid in ADMIN_IDS,'see_enabled':see_enabled(uid),'delivery':destination(uid),**membership(uid)}
            elif path=='/api/delivery':
                if not membership(uid)['member']:
                    self.json_response(403,{'error':'Подпишись на @ZergowNft.'});return
                action=data.get('action')
                if action=='link':result={'instructions':link_instructions(uid)}
                elif action=='private':result={'delivery':private_destination(uid)}
                else:raise ValueError('Неизвестное действие.')
            elif path in ('/api/random','/api/prices'):
                status=membership(uid)
                if not status['member']:
                    self.json_response(403,{'error':'Подпишись на @ZergowNft или повтори проверку подписки.','reason':status['reason']});return
                result={'gifts':search_gifts(uid,data.get('count',MAX_BATCH),data.get('filters'),data.get('mode','all') if path=='/api/prices' else None)}
                result['delivery']=deliver_gifts(uid,result['gifts'],data.get('mode','all') if path=='/api/prices' else ('filters' if 'filters' in data else 'random'))
                if result['delivery']['kind']=='blocked':
                    self.json_response(403,{'error':'Подписка не подтверждена.'});return
            elif path=='/api/tickets':result={'tickets':tickets(uid)}
            else:result={'id':new_ticket(uid,data.get('kind'),data.get('body'))}
            self.json_response(200,result)
        except GiftError as e:self.json_response(503,{'error':str(e)})
        except ValueError as e:self.json_response(400,{'error':str(e)})
        except Exception:logging.exception('Support request failed');self.json_response(500,{'error':'Ошибка сервера'})
    def log_message(self,*args): pass

def api(method,**kwargs):
    req=Request(f'https://api.telegram.org/bot{TOKEN}/{method}',data=json.dumps(kwargs).encode(),headers={'Content-Type':'application/json'})
    try:
        with urlopen(req,timeout=40) as r: out=json.load(r)
    except Exception as e: raise RuntimeError(f'Telegram API {method}: {type(e).__name__}') from None
    if not out.get('ok'):raise RuntimeError('Telegram API request failed: '+method)
    return out['result']

def send(chat,text,markup=None):
    kw={'chat_id':chat,'text':text}
    if markup:kw['reply_markup']=markup
    return api('sendMessage',**kw)
MENU={'keyboard':[[{'text':'✏️ Верхний баннер'},{'text':'👁 Показать / скрыть баннер'}],[{'text':'🏷 Название'},{'text':'💬 Контакт поддержки'}],[{'text':'👋 Приветствие'},{'text':'🔘 Кнопка запуска'}],[{'text':'🔐 Текст подписки'}],[{'text':'📥 Обращения'},{'text':'📋 Текущие настройки'}],[{'text':'📊 Квота See.tg'},{'text':'Отмена'}]],'resize_keyboard':True}
OPTIONS={'🔐 Текст подписки':('access_text','Введи текст просьбы о подписке до 500 символов.'),'👋 Приветствие':('welcome','Введи приветствие до 500 символов.'),'🔘 Кнопка запуска':('button_text','Введи подпись кнопки до 48 символов.'),'✏️ Верхний баннер':('banner','Введи текст баннера до 240 символов.'),'🏷 Название':('title','Введи название до 64 символов.'),'💬 Контакт поддержки':('support','Введи @username поддержки или «-».')}
def summary():
    c=read_config()
    return f"Название: {c['title']}\nБаннер: {c['banner']}\nПоддержка: {c['support'] or 'не задана'}\nДоступ: бесплатный\nКанал: {GROUP_ID}\nТекст подписки: {c['access_text']}"
def handle(msg):
    chat=msg.get('chat',{});uid=msg.get('from',{}).get('id');text=msg.get('text','').strip();command=text.split()[0].split('@')[0] if text else ''
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
    if command=='/start':
        pending.pop(uid,None);home(chat) if gate(chat,uid) else None;return
    if command in ('/delivery','/private'):
        if not gate(chat,uid):return
        if command=='/private':private_destination(uid)
        delivery_menu(uid);return
    if command=='/find':
        if not gate(chat,uid):return
        try:
            raw=text.partition(' ')[2];filters=dict(tuple(x.strip().split('=',1)) for x in raw.split(';') if x.strip())
            if not see_enabled(uid):filters=validate_filters(filters)
            show_gifts(chat,search_gifts(uid,MAX_BATCH,filters),'filters')
        except (ValueError,GiftError) as e:send(chat,'Ошибка поиска: '+str(e))
        return
    if uid not in ADMIN_IDS:return
    if command=='/seequota' or text=='📊 Квота See.tg':
        try:send(chat,'Остаток бесплатных запросов See.tg: '+str(see_api.client.quota()))
        except GiftError as e:send(chat,str(e))
        return
    if command=='/gift':
        if not gate(chat,uid):return
        try:
            slug=text.partition(' ')[2].strip().removeprefix('https://t.me/nft/')
            item=see_api.client.single(slug)
            title,rows=gift_pack([item],'random')
            title+='\n\nПубличный владелец: '+item.get('public_username','не указан')
            send(chat,title,ik(rows))
        except (GiftError,ValueError) as e:send(chat,str(e))
        return
    if command=='/lot':
        try:
            _,slug,price=text.split();set_lot(slug,price);send(chat,'Цена добавлена на 24 часа.')
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
        if not rows:send(chat,'Нет открытых обращений.',MENU)
        for r in rows:send(chat,f"#{r['id']} · {r['kind']}\n{r['body']}\n/reply {r['id']} текст")
        return
    if command=='/admin':pending.pop(uid,None);send(chat,'Управление оформлением\nSee.tg (тест владельца): /seequota; /gift PlushPepe-1\nКаталог V7: /lot slug цена_TON; /unlot slug',MENU);return
    if command=='/cancel' or text=='Отмена':pending.pop(uid,None);send(chat,'Изменение отменено.',MENU);return
    if text in OPTIONS:
        key,prompt=OPTIONS[text];pending[uid]=key;send(chat,prompt,{'keyboard':[[{'text':'Отмена'}]],'resize_keyboard':True});return
    if text=='📋 Текущие настройки':pending.pop(uid,None);send(chat,summary(),MENU);return
    if text=='👁 Показать / скрыть баннер':
        pending.pop(uid,None);c=change('banner_enabled',not read_config()['banner_enabled']);send(chat,'Баннер '+('показан' if c['banner_enabled'] else 'скрыт')+'.',MENU);return
    key=pending.get(uid)
    if not key:return
    try:
        if key=='support':
            if text=='-':value=''
            elif re.fullmatch(r'@[A-Za-z0-9_]{5,32}',text):value=text
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
    pending.pop(uid,None);send(chat,'✅ Сохранено. Mini App обновит настройки автоматически.',MENU)

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
                except Exception as e:logging.warning('Update failed: %s',type(e).__name__)
                offset=update['update_id']+1
        except Exception as e:logging.warning('%s',e);time.sleep(3)

def main():
    if TOKEN:
        if not URL.startswith('https://') or not ADMIN_IDS:raise SystemExit('Set HTTPS WEBAPP_URL and ADMIN_IDS before enabling bot.')
        try:api('setChatMenuButton',menu_button={'type':'web_app','text':read_config()['button_text'],'web_app':{'url':URL+'/'}})
        except Exception as e:logging.warning('%s',e)
        threading.Thread(target=bot_loop,daemon=True).start()
    else:logging.info('Preview mode: bot disabled')
    port=int(os.environ.get('PORT','8000'));host=os.environ.get('HOST','127.0.0.1');logging.info('Serving %s:%s',host,port)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
if __name__=='__main__':main()
