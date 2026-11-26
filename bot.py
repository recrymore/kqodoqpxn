import asyncio, html, logging, os, time, math
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from telethon import TelegramClient, functions, errors
from core import Store, parse_gift, slug_of, USER

load_dotenv()
logging.basicConfig(level=logging.INFO)
log=logging.getLogger('gift_parser')
TOKEN=os.environ.get('BOT_TOKEN','')
TTL=int(os.getenv('MARKET_TTL','3600'))
INTERVAL=max(60,int(os.getenv('SCAN_INTERVAL','300')))
DELAY=max(1,float(os.getenv('REQUEST_DELAY','2')))
MAX_PAGES=max(1,int(os.getenv('MAX_PAGES_PER_COLLECTION','10')))
IDS={int(x) for x in os.getenv('GIFT_IDS','').split(',') if x.strip()}
store=Store(os.getenv('DB_PATH','gifts.sqlite3'))
dp=Dispatcher(); states={}; client=None; api_lock=asyncio.Lock()

def state(uid): return states.setdefault(uid,{'filters':{},'market':False,'page':0})
def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=c) for t,c in row] for row in rows])
def b(t,c): return (t,c)
def menu():
    return kb([b('🔍 Каталог NFT','catalog'),b('🛒 Telegram Market','market')],[b('🎁 Добавить NFT по ссылке','add')],[b('🤝 Мой подарок: открыть контакт','consent')],[b('🔒 Отозвать согласие','revoke')])
async def call(req):
    async with api_lock:
        while True:
            try:
                result=await client(req); await asyncio.sleep(DELAY); return result
            except errors.FloodWaitError as e:
                log.warning('FloodWait: %s sec',e.seconds); await asyncio.sleep(e.seconds+1)
async def unique(slug): return await call(functions.payments.GetUniqueStarGiftRequest(slug=slug))

def owner_matches(g, uid):
    p=getattr(g,'owner_id',None)
    return type(p).__name__=='PeerUser' and getattr(p,'user_id',None)==uid

async def verified_contact(slug):
    uid=store.consent(slug)
    if not uid: return None
    try:
        r=await unique(slug)
        if not owner_matches(r.gift,uid) or store.consent(slug)!=uid: return None
        u=next((u for u in r.users if u.id==uid),None)
        tag=getattr(u,'username',None)
        return tag if tag and USER.fullmatch(tag) else None
    except errors.RPCError: return None

async def scan_once():
    catalog=await call(functions.payments.GetStarGiftsRequest(hash=0))
    ids=IDS or {g.id for g in catalog.gifts if getattr(g,'availability_resale',0)}
    for gift_id in sorted(ids):
        offset=''; stamp=int(time.time()); complete=False; seen=set()
        try:
            for _ in range(MAX_PAGES):
                r=await call(functions.payments.GetResaleStarGiftsRequest(gift_id=gift_id,offset=offset,limit=50))
                for g in r.gifts:
                    if not getattr(g,'slug',None): continue
                    data=parse_gift(g)
                    if data['stars'] or data['ton']: store.save(data,gift_id,stamp)
                nxt=getattr(r,'next_offset',None)
                if not nxt: complete=True; break
                if nxt in seen: break
                seen.add(nxt); offset=nxt
            if complete: store.complete(gift_id,stamp)
            else: log.info('Collection %s: partial scan (page limit)',gift_id)
        except (errors.RPCError,ValueError) as e: log.warning('Collection %s: %s',gift_id,type(e).__name__)
async def scanner():
    while True:
        try: await scan_once()
        except Exception: log.exception('Market scan failed')
        await asyncio.sleep(INTERVAL)

FIELDS={'title':'Коллекция','model':'Модель','backdrop':'Фон','pattern':'Узор','num':'Номер'}
async def filters_view(msg,s):
    f=s['filters']; text='⚙️ <b>Фильтры</b> — '+('Telegram Market' if s['market'] else 'Каталог')+'\n'
    text+='\n'.join(html.escape(label+': '+str(f.get(k,'Все'))) for k,label in FIELDS.items())
    text+='\nЦена: '+html.escape(str(f.get('min','—'))+' – '+str(f.get('max','—'))+' '+f.get('currency','stars'))
    text+='\nРедкость модели ≤1%: '+('да' if f.get('rare') else 'нет')
    await msg.edit_text(text,parse_mode='HTML',reply_markup=kb([b('🎁 Коллекция','pick:title')],[b('💎 Модель','pick:model'),b('🎨 Фон','pick:backdrop')],[b('🖌 Узор','pick:pattern'),b('🔢 Номер','input:num')],[b('🏆 Редкие модели','rare'),b('💰 Цена','input:price')],[b('Сортировка: цена','sort:price'),b('По номеру','sort:number')],[b('По свежести','sort:new'),b('Сбросить','reset')],[b('🔎 Найти','results'),b('↩ Меню','home')]))

async def options_view(msg,s,field,page=0):
    vals=store.options(field,s['filters'].get('title')); pages=max(1,math.ceil(len(vals)/12)); page=max(0,min(page,pages-1))
    s['options']=(field,vals[page*12:(page+1)*12]); rows=[[b(v[:60],f'val:{i}')] for i,v in enumerate(s['options'][1])]
    rows += [[b('Все','val:all')],[b('◀',f'opt:{field}:{page-1}'),b(f'{page+1}/{pages}','noop'),b('▶',f'opt:{field}:{page+1}')],[b('Ввести вручную',f'input:{field}'),b('Назад','filters')]]
    await msg.edit_text(FIELDS[field]+': выбери значение',reply_markup=kb(*rows))

async def results(msg,s):
    rows,total,page=store.query(s['filters'],s['market'],s['page'],TTL); s['page']=page
    lines=[f'🔎 <b>Результаты: {total}</b> · {page+1}/{max(1,math.ceil(total/10))}']
    for r in rows:
        link='https://t.me/nft/'+r['slug']; tag=await verified_contact(r['slug']); price=[]
        if r['market_at'] and r['market_at']>=time.time()-TTL:
            if r['stars']: price.append(f"{r['stars']:g} ⭐")
            if r['ton']: price.append(f"{r['ton']:g} TON")
        # A tag is displayed ONLY after explicit self-registration and fresh verification.
        contact=f' | <a href="https://t.me/{tag}">@{tag}</a>' if tag else ''
        lines.append(f'🔗 <a href="{link}">{html.escape(r["title"])} #{r["num"]}</a>'+contact+'\n'+html.escape(' · '.join(x for x in (r['model'],r['backdrop'],r['pattern'],', '.join(price)) if x)))
    if not rows: lines.append('Ничего не найдено. База пополняется по мере сканирования и добавления ссылок.')
    lines.append('Данные рынка — снимок, не гарантия наличия в момент покупки.')
    await msg.edit_text('\n\n'.join(lines),parse_mode='HTML',link_preview_options=LinkPreviewOptions(is_disabled=True),reply_markup=kb([b('◀ Назад','prev'),b('Далее ▶','next')],[b('🔄 Обновить','results'),b('⚙️ Фильтры','filters')],[b('↩ Меню','home')]))

@dp.message(CommandStart())
async def start(m):
    if m.chat.type!='private': return
    states.pop(m.from_user.id,None); await m.answer('⚡ NFT Gift Parser\nВыбери действие:',reply_markup=menu())

@dp.callback_query()
async def callbacks(q):
    if not q.message or q.message.chat.type!='private': await q.answer(); return
    s=state(q.from_user.id); a=q.data or ''; await q.answer()
    try:
        if a=='home': s.pop('pending',None); await q.message.edit_text('⚡ NFT Gift Parser',reply_markup=menu())
        elif a in ('market','catalog','filters','reset','rare') or a.startswith('sort:'):
            if a in ('market','catalog'): s.update(market=a=='market',filters={},page=0)
            if a=='reset': s['filters']={}
            if a=='rare': s['filters']['rare']=not s['filters'].get('rare')
            if a.startswith('sort:'): s['filters'].update(price_sort=a=='sort:price',number_sort=a=='sort:number')
            s.pop('pending',None); await filters_view(q.message,s)
        elif a.startswith('pick:'): await options_view(q.message,s,a.split(':')[1])
        elif a.startswith('opt:'):
            _,field,page=a.split(':'); await options_view(q.message,s,field,int(page))
        elif a.startswith('val:'):
            field,vals=s['options']; v=a.split(':')[1]
            if v=='all': s['filters'].pop(field,None)
            else: s['filters'][field]=vals[int(v)]
            if field=='title':
                for key in ('model','backdrop','pattern'): s['filters'].pop(key,None)
            await filters_view(q.message,s)
        elif a in ('results','prev','next'):
            if a=='prev': s['page']-=1
            elif a=='next': s['page']+=1
            else: s['page']=0
            await results(q.message,s)
        elif a in ('add','consent') or a.startswith('input:'):
            s['pending']=a; s['message']=q.message.message_id
            prompts={'add':'Отправь ссылку NFT (без контакта).','consent':'Отправь ссылку своего NFT. Ты добровольно разрешаешь показывать свой публичный @username рядом с подарком на 24 часа. Отзыв — кнопка в меню.','input:price':'Введи: stars 100 5000 или ton 1 100 (мин. макс.).'}
            await q.message.edit_text(prompts.get(a,'Введи точное значение фильтра:'),reply_markup=kb([b('Отмена','home')]))
        elif a=='revoke': store.revoke(q.from_user.id); await q.message.edit_text('Согласие отозвано.',reply_markup=menu())
    except Exception as e:
        if 'message is not modified' not in str(e).lower(): log.warning('Callback failed: %s',type(e).__name__); await q.message.answer('Не удалось выполнить запрос. /start',reply_markup=menu())

@dp.message(F.text)
async def text_input(m):
    if m.chat.type!='private': return
    s=state(m.from_user.id); action=s.get('pending'); text=m.text.strip()
    if not action: await m.answer('Выбери действие:',reply_markup=menu()); return
    try:
        if action in ('add','consent'):
            slug=slug_of(text); r=await unique(slug); g=parse_gift(r.gift)
            if action=='consent':
                if not m.from_user.username or not owner_matches(r.gift,m.from_user.id): raise ValueError('Telegram не подтвердил, что это твой NFT с публичным username.')
                store.grant(g['slug'],m.from_user.id)
            store.save(g); s.pop('pending',None); await m.answer('NFT добавлен.'+(' Контакт открыт на 24 часа.' if action=='consent' else ''),reply_markup=menu()); return
        field=action.split(':')[1]
        if field=='price':
            currency,lo,hi=text.split(); lo=float(lo); hi=float(hi)
            if currency not in ('stars','ton') or not all(map(math.isfinite,(lo,hi))) or not 0<=lo<=hi: raise ValueError('Формат: stars 100 5000 или ton 1 100')
            s['filters'].update(currency=currency,min=lo,max=hi)
        elif field=='num':
            n=int(text)
            if not 0<n<10**12: raise ValueError('Введите положительный номер')
            s['filters'][field]=n
        elif field in FIELDS:
            if not text or len(text)>100: raise ValueError('Введите название до 100 символов')
            s['filters'][field]=text
        else: raise ValueError('Неизвестный фильтр')
        s.pop('pending',None); s['page']=0
        await m.answer('Фильтр установлен.',reply_markup=kb([b('🔎 Найти','results'),b('⚙️ Фильтры','filters')]))
    except (ValueError,errors.RPCError) as e: await m.answer('Не удалось обработать: '+str(e)[:180])

async def main():
    global client
    if not TOKEN: raise SystemExit('Заполни BOT_TOKEN в .env')
    client=TelegramClient('market',int(os.environ['API_ID']),os.environ['API_HASH'])
    await client.start()
    if (await client.get_me()).bot: raise SystemExit('Для рынка нужна пользовательская MTProto-сессия, не бот.')
    task=asyncio.create_task(scanner())
    bot=Bot(TOKEN)
    try: await dp.start_polling(bot)
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True); await client.disconnect(); await bot.session.close()
if __name__=='__main__': asyncio.run(main())
