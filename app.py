"""Design-stage Telegram Mini App, standard-library only. Run: python app.py."""
import copy, json, os, re, threading, time, logging, hmac, hashlib, sqlite3
from urllib.error import HTTPError
from gifts import random_gifts, GiftError, COLLECTIONS, validate_filters, matches, fetch_card
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
GROUP_ID=os.environ.get('GROUP_ID','').strip()
GROUP_URL=os.environ.get('GROUP_URL','https://t.me/+DxLz5PTGdiRlZjVi')
DEFAULT={'title':'Gift Radar • Discover','banner':'Бесплатный трекер подарков','banner_enabled':True,'support':'','version':1,'welcome':'Открой трекер и найди случайные подарки. Доступ бесплатный для участников группы.','button_text':'Открыть трекер'}
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
def db():
    DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB,timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL, reply TEXT, replied INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS lots (slug TEXT PRIMARY KEY, price_milli INTEGER NOT NULL, card TEXT NOT NULL, updated INTEGER NOT NULL)')
    return conn

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
        m=api('getChatMember',chat_id=int(GROUP_ID),user_id=uid)
        ok=m.get('status') in ('creator','administrator','member') or (m.get('status')=='restricted' and m.get('is_member') is True)
        return {'member':bool(ok),'reason':'ok' if ok else 'not_member'}
    except Exception:
        return {'member':False,'reason':'verification_failed'}

PRICE_MODES={'low':(0,5000),'mid':(5000,20000),'high':(20000,1000000),'all':(0,1000000)}
def price_gifts(mode):
    if mode not in PRICE_MODES:raise ValueError('Неизвестный диапазон.')
    low,high=PRICE_MODES[mode]
    with db() as c:
        rows=c.execute('SELECT * FROM lots WHERE price_milli>=? AND price_milli<? AND updated>=? ORDER BY RANDOM() LIMIT 9',(low,high,int(time.time())-86400)).fetchall()
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

def ik(rows):return {'inline_keyboard':[[{'text':label,**({'url':value} if value.startswith('https://') else {'callback_data':value})} for label,value in row] for row in rows]}
def home(chat):
    c=read_config();send(chat,c['title']+'\n'+c['welcome'],{'inline_keyboard':[[{'text':'🎲 Случайные подарки','callback_data':'random'},{'text':'💎 По цене','callback_data':'prices'}],[{'text':'🎛 Фильтры','callback_data':'filters'}],[{'text':c['button_text'],'web_app':{'url':URL+'/'}}]]})
def gate(chat,uid):
    if membership(uid)['member']:return True
    send(chat,'Для бесплатного доступа вступи в группу и подтверди участие.',ik([[('Вступить',GROUP_URL)],[('Проверить','check')]]));return False

def show_gifts(chat,items,mode):
    title=f'🔎 Найдено: {len(items)}'
    if not items:title+='\nСовпадений пока нет. Попробуй другой запрос.'
    if mode in PRICE_MODES:title+='\nЦены из каталога администратора, проверь на площадке.'
    for i,g in enumerate(items,1):
        title+=f"\n\n{i}. {g['name']} #{g['number']}"+(f" · {g['price_ton']:g} TON" if 'price_ton' in g else '')+f"\n{g['model']} · {g['backdrop']} · {g['symbol']}"
    rows=[[(f"{i}. Открыть NFT",g['url'])] for i,g in enumerate(items,1)]
    rows.append([('🎛 Изменить фильтры' if mode=='filters' else '↻ Новая подборка',mode),('← Меню','home')]);send(chat,title,ik(rows))

def callback(q):
    api('answerCallbackQuery',callback_query_id=q['id'])
    msg=q.get('message',{});chat=msg.get('chat',{});uid=q.get('from',{}).get('id');action=q.get('data','')
    if chat.get('type')!='private' or chat.get('id')!=uid:return
    chat=chat['id']
    if action=='home':home(chat);return
    if not gate(chat,uid):return
    if action=='check':home(chat)
    elif action=='prices':send(chat,'💎 Выбери бюджет в TON.\nДо 5 · 5–20 · 20–999.999',ik([[('До 5','low'),('5–20','mid')],[('20+','high'),('Все цены','all')],[('← Меню','home')]]))
    elif action=='filters':send(chat,'🎛 Фильтры по атрибутам\nВведи, например: /find model=Steel; backdrop=Black; symbol=Star\nКоллекции: '+', '.join(COLLECTIONS)+'\nЛюбое поле можно пропустить.',ik([[('← Меню','home')]]))
    elif action=='random' or action in PRICE_MODES:
        try:show_gifts(chat,random_gifts(3) if action=='random' else price_gifts(action),action)
        except GiftError as e:send(chat,str(e))

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
        if path not in ('/api/me','/api/tickets','/api/ticket','/api/random','/api/prices'): self.json_response(404,{'error':'Не найдено'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768: raise ValueError('Недопустимый размер запроса')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Недопустимый запрос')
        except (ValueError,TypeError): self.json_response(400,{'error':'Некорректный запрос'});return
        try: uid=verify_init(data.get('initData'))
        except (ValueError,TypeError,KeyError): self.json_response(401,{'error':'Открой Mini App заново в Telegram'});return
        try:
            if path=='/api/me': result={'id':uid,'admin':uid in ADMIN_IDS,**membership(uid)}
            elif path in ('/api/random','/api/prices'):
                status=membership(uid)
                if not status['member']:
                    self.json_response(403,{'error':'Вступи в группу или повтори проверку доступа.','reason':status['reason']});return
                result={'gifts':price_gifts(data.get('mode','all')) if path=='/api/prices' else (random_gifts(data.get('count',3),data['filters']) if 'filters' in data else random_gifts(data.get('count',3)))}
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
MENU={'keyboard':[[{'text':'✏️ Верхний баннер'},{'text':'👁 Показать / скрыть баннер'}],[{'text':'🏷 Название'},{'text':'💬 Контакт поддержки'}],[{'text':'👋 Приветствие'},{'text':'🔘 Кнопка запуска'}],[{'text':'📥 Обращения'},{'text':'📋 Текущие настройки'}],[{'text':'Отмена'}]],'resize_keyboard':True}
OPTIONS={'👋 Приветствие':('welcome','Введи приветствие до 500 символов.'),'🔘 Кнопка запуска':('button_text','Введи подпись кнопки до 48 символов.'),'✏️ Верхний баннер':('banner','Введи текст баннера до 240 символов.'),'🏷 Название':('title','Введи название до 64 символов.'),'💬 Контакт поддержки':('support','Введи @username поддержки или «-».')}
def summary():
    c=read_config()
    return f"Название: {c['title']}\nБаннер: {c['banner']}\nПоддержка: {c['support'] or 'не задана'}\nДоступ: бесплатный\nID группы: {GROUP_ID or 'не настроен'}"
def handle(msg):
    chat=msg.get('chat',{});uid=msg.get('from',{}).get('id');text=msg.get('text','').strip();command=text.split()[0].split('@')[0] if text else ''
    if chat.get('type') in ('group','supergroup'):
        if command=='/groupid' and uid in ADMIN_IDS:send(chat['id'],f"GROUP_ID: {chat['id']}")
        return
    if chat.get('type')!='private':return
    chat=chat['id']
    if command=='/start':
        pending.pop(uid,None);home(chat) if gate(chat,uid) else None;return
    if command=='/find':
        if not gate(chat,uid):return
        try:
            raw=text.partition(' ')[2];filters=validate_filters(dict(tuple(x.strip().split('=',1)) for x in raw.split(';') if x.strip()))
            show_gifts(chat,random_gifts(3,filters),'filters')
        except (ValueError,GiftError) as e:send(chat,'Ошибка поиска: '+str(e))
        return
    if uid not in ADMIN_IDS:return
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
    if command=='/admin':pending.pop(uid,None);send(chat,'Управление оформлением\nКаталог: /lot slug цена_TON; /unlot slug',MENU);return
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
            limit={'title':64,'welcome':500,'button_text':48,'banner':240}[key]
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
