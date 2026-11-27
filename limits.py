"""Persistent action limits and conservative API cost accounting (UTC)."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from gifts import GiftError

DEFAULT = dict(search_daily=19, detail_daily=10, batch_max=25, cooldown=10,
               budget_nano=200_000_000, paid=False, search_open=True)
PRICE_NANO=2_000_000  # 0.002 TON, as shown in the account UI

def day(): return datetime.now(timezone.utc).date().isoformat()

def validate(values):
    if not isinstance(values,dict) or set(values)!=set(DEFAULT):raise ValueError('Некорректные лимиты.')
    for key,lo,hi in [('search_daily',1,100),('detail_daily',1,100),('batch_max',1,50),('cooldown',5,120),('budget_nano',0,10_000_000_000)]:
        if type(values[key]) is not int or not lo<=values[key]<=hi:raise ValueError('Некорректный лимит: '+key)
    for key in ('paid','search_open'):
        if type(values[key]) is not bool:raise ValueError('Некорректный переключатель.')
    return dict(values)

class Limits:
    def __init__(self,db,config):self.db=db;self.config=config
    def settings(self):return {**DEFAULT,**self.config().get('limits',{})}
    def consume(self,uid,kind):
        cfg=self.settings();today=day();now=time.time()
        if kind not in ('search','detail'):raise ValueError('Неизвестная операция.')
        if not cfg['search_open']:raise GiftError('Поиск временно отключён администратором.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT used,last FROM usage_daily WHERE day=? AND uid=? AND kind=?',(today,uid,kind)).fetchone()
            used,last=(row['used'],row['last']) if row else (0,0)
            if used>=cfg[kind+'_daily']:raise GiftError('Дневной лимит исчерпан. Обновление в 00:00 UTC.')
            if last and now-last<cfg['cooldown']:raise GiftError('Подожди немного перед следующим запросом.')
            c.execute('INSERT OR REPLACE INTO usage_daily VALUES(?,?,?,?,?)',(today,uid,kind,used+1,now))
            c.execute('DELETE FROM usage_daily WHERE day<date(?,\'-7 days\')',(today,))
    def remaining(self,uid):
        cfg=self.settings()
        with self.db() as c:
            rows=c.execute('SELECT kind,used FROM usage_daily WHERE day=? AND uid=?',(day(),uid)).fetchall()
        used={r['kind']:r['used'] for r in rows}
        return {k:max(0,cfg[k+'_daily']-used.get(k,0)) for k in ('search','detail')}
    def finance(self):
        with self.db() as c:
            row=c.execute('SELECT cost,requests,uncertain FROM billing_daily WHERE day=?',(day(),)).fetchone()
        return dict(row) if row else dict(cost=0,requests=0,uncertain=0)
    def reserve(self):
        today=day();cfg=self.settings()
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT cost,requests,uncertain FROM billing_daily WHERE day=?',(today,)).fetchone()
            cost,requests,uncertain=tuple(row) if row else (0,0,0)
            if uncertain or cost+PRICE_NANO>cfg['budget_nano']:raise GiftError('Дневной бюджет исчерпан или расход не подтверждён. Запрос отменён.')
            c.execute('INSERT OR REPLACE INTO billing_daily VALUES(?,?,?,?)',(today,cost+PRICE_NANO,requests+1,uncertain+1))
        return today
    def settle(self,today,header):
        # Unknown cost: retain reservation and stop until UTC reset. No retry.
        try:
            n=Decimal(str(header))*1_000_000_000
            if not n.is_finite() or n<0 or n!=n.to_integral_value() or n>10**15:return
            actual=int(n)
        except (InvalidOperation,ValueError,TypeError):return
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE billing_daily SET cost=cost+?,uncertain=uncertain-1+? WHERE day=?',(actual-PRICE_NANO,int(actual>PRICE_NANO),today))
