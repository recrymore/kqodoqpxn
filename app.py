"""Design-stage Telegram Mini App, standard-library only. Run: python app.py."""
import copy, json, os, re, threading, time, logging, hmac, hashlib, sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qsl
ROOT=Path(__file__).resolve().parent
CONFIG=Path(os.environ.get('CONFIG_PATH',str(ROOT/'config.json')))
TOKEN=os.environ.get('BOT_TOKEN','')
URL=os.environ.get('WEBAPP_URL','').rstrip('/')
ADMIN_IDS={int(x.strip()) for x in os.environ.get('ADMIN_IDS','').split(',') if x.strip()}
DEFAULT={'title':'Трекер подарков','banner':'@team_example панель тимы для ворка','banner_enabled':True,'tariffs':[{'days':d,'price':None} for d in (1,3,7)],'referral_hours':6,'support':'','version':1,'welcome':'Открой трекер кнопкой ниже.','button_text':'Открыть трекер'}
lock=threading.RLock(); pending={}
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
def read_config():
    with lock:
        if not CONFIG.exists(): return copy.deepcopy(DEFAULT)
        return {**copy.deepcopy(DEFAULT), **json.loads(CONFIG.read_text(encoding='utf-8'))}
def change(key,value):
    with lock:
        c=read_config(); c[key]=value;c['version']+=1
        CONFIG.parent.mkdir(parents=True,exist_ok=True)
        temp=CONFIG.with_suffix('.tmp');temp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(CONFIG)
        return c

def parse_tariffs(text):
    result=[]; seen=set()
    for line in text.splitlines():
        if not line.strip(): continue
        parts=line.replace(':',' ').split()
        if len(parts)!=2: raise ValueError('Каждая строка: дни цена')
        d=int(parts[0]);p=parts[1].replace(',','.')
        if not re.fullmatch(r'\d{1,9}(?:\.\d{1,9})?',p): raise ValueError('Цена должна быть положительным числом, до 9 знаков после точки')
        amount=Decimal(p)
        if not 1<=d<=3650 or d in seen or amount<=0: raise ValueError('Уникальный срок от 1 до 3650 дней, цена больше нуля')
        seen.add(d);result.append({'days':d,'price':format(amount.normalize(),'f')})
    if not 1<=len(result)<=20: raise ValueError('От 1 до 20 тарифов')
    return sorted(result,key=lambda x:x['days'])

DB=Path(os.environ.get('DB_PATH',str(ROOT/'tracker.sqlite3')))
def db():
    DB.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB,timeout=10)
    conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL, reply TEXT, replied INTEGER)')
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

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in ('/','/index.html'):
            data=(ROOT/'index.html').read_bytes();typ='text/html; charset=utf-8'
        elif path=='/api/config':
            data=json.dumps(read_config(),ensure_ascii=False).encode();typ='application/json; charset=utf-8'
        elif path=='/health':
            data=b'ok';typ='text/plain'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
    def json_response(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_POST(self):
        path=urlsplit(self.path).path
        if path not in ('/api/me','/api/tickets','/api/ticket'): self.json_response(404,{'error':'Не найдено'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768: raise ValueError('Недопустимый размер запроса')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Недопустимый запрос')
        except (ValueError,TypeError): self.json_response(400,{'error':'Некорректный запрос'});return
        try: uid=verify_init(data.get('initData'))
        except (ValueError,TypeError,KeyError): self.json_response(401,{'error':'Открой Mini App заново в Telegram'});return
        try:
            if path=='/api/me': result={'id':uid,'admin':uid in ADMIN_IDS}
            elif path=='/api/tickets':result={'tickets':tickets(uid)}
            else:result={'id':new_ticket(uid,data.get('kind'),data.get('body'))}
            self.json_response(200,result)
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
MENU={'keyboard':[[{'text':'✏️ Верхний баннер'},{'text':'💰 Тарифы и цены'}],[{'text':'👁 Показать / скрыть баннер'},{'text':'🏷 Название'}],[{'text':'🎁 Реферальный бонус'},{'text':'💬 Контакт поддержки'}],[{'text':'👋 Приветствие'},{'text':'🔘 Кнопка запуска'}],[{'text':'📥 Обращения'},{'text':'📋 Текущие настройки'}],[{'text':'Отмена'}]],'resize_keyboard':True,'one_time_keyboard':False}
OPTIONS={'👋 Приветствие':('welcome','Введи текст приветствия до 500 символов.'),'🔘 Кнопка запуска':('button_text','Введи подпись кнопки до 48 символов.'),'✏️ Верхний баннер':('banner','Введи новый текст баннера (до 240 символов). @username станет ссылкой.'),'💰 Тарифы и цены':('tariffs','Отправь тарифы: каждая строка — срок в днях и цена GRAM. Например:\n1 100\n3 220\n7 400\nЭто только пример.'),'🏷 Название':('title','Введи новое название (до 64 символов).'),'🎁 Реферальный бонус':('referral_hours','Введи число часов от 0 до 720.'),'💬 Контакт поддержки':('support','Введи @username поддержки или «-», чтобы убрать контакт.')}
def summary():
    c=read_config(); tariffs='\n'.join(f"{t['days']} дн. — {t['price'] if t['price'] is not None else 'не задано'} GRAM" for t in c['tariffs'])
    return f"Название: {c['title']}\nБаннер: {'вкл.' if c['banner_enabled'] else 'выкл.'}\n{c['banner']}\n\nТарифы:\n{tariffs}\n\nРеферальный бонус: {c['referral_hours']} ч.\nПоддержка: {c['support'] or 'не задана'}"
def handle(msg):
    if msg.get('chat',{}).get('type')!='private':return
    chat=msg['chat']['id'];uid=msg.get('from',{}).get('id');text=msg.get('text','').strip();command=text.split()[0].split('@')[0] if text else ''
    if command=='/start':
        pending.pop(uid,None);c=read_config();send(chat,c['title']+'\n'+c['welcome'],{'inline_keyboard':[[{'text':c['button_text'],'web_app':{'url':URL+'/'} }]]});return
    if uid not in ADMIN_IDS:
        if command=='/admin':send(chat,'Нет доступа.')
        return
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
    if command=='/admin':pending.pop(uid,None);send(chat,'Управление оформлением и тарифами',MENU);return
    if command=='/cancel' or text=='Отмена':pending.pop(uid,None);send(chat,'Изменение отменено.',MENU);return
    if text in OPTIONS:
        key,prompt=OPTIONS[text];pending[uid]=key;send(chat,prompt,{'keyboard':[[{'text':'Отмена'}]],'resize_keyboard':True});return
    if text=='📋 Текущие настройки':pending.pop(uid,None);send(chat,summary(),MENU);return
    if text=='👁 Показать / скрыть баннер':
        pending.pop(uid,None);c=change('banner_enabled',not read_config()['banner_enabled']);send(chat,'Баннер '+('показан' if c['banner_enabled'] else 'скрыт')+'.',MENU);return
    key=pending.get(uid)
    if not key:return
    try:
        if key=='tariffs':value=parse_tariffs(text)
        elif key=='referral_hours':
            value=int(text)
            if not 0<=value<=720:raise ValueError('Допустимо 0–720 часов')
        elif key=='support':
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
    except (ValueError,InvalidOperation) as e:send(chat,'Не сохранено: '+str(e));return
    pending.pop(uid,None);send(chat,'✅ Сохранено. Mini App обновит настройки автоматически.',MENU)

def bot_loop():
    offset=None
    while True:
        try:
            params={'timeout':25,'allowed_updates':['message']}
            if offset is not None:params['offset']=offset
            for update in api('getUpdates',**params):
                try:handle(update.get('message',{}))
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
