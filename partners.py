"""Consent-based partner requests. No bot credentials are collected here."""
import os, re, sqlite3, time
from pathlib import Path
from contextlib import contextmanager

PATH = Path(os.environ.get('PARTNER_DB_PATH', str(Path(__file__).resolve().parent / 'partners.sqlite3')))

@contextmanager
def db():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(PATH, timeout=10)
    try:
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, username TEXT UNIQUE COLLATE NOCASE NOT NULL, status TEXT NOT NULL DEFAULT \'pending\', created INTEGER NOT NULL)')
        with c:
            yield c
    finally:
        c.close()

def submit(uid, username):
    if type(uid) is not int or uid <= 0 or not isinstance(username, str):
        raise ValueError('Некорректная заявка.')
    name = username.strip().removeprefix('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', name) or not name.lower().endswith('bot'):
        raise ValueError('Укажи только @username бота, не токен.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM requests WHERE username=?', (name,)).fetchone()
        if old:
            if old['uid'] != uid:
                raise ValueError('Этот username уже зарегистрирован.')
            return dict(old)
        if c.execute('SELECT COUNT(*) FROM requests WHERE uid=? AND created>?', (uid, int(time.time())-86400)).fetchone()[0] >= 3:
            raise ValueError('Не более трёх новых заявок в сутки.')
        rid=c.execute('INSERT INTO requests(uid,username,created) VALUES(?,?,?)',(uid,name,int(time.time()))).lastrowid
        return dict(c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())

def mine(uid):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT id,username,status,created FROM requests WHERE uid=? ORDER BY id DESC LIMIT 20',(uid,))]

def recent():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM requests ORDER BY id DESC LIMIT 20')]

def review(rid, status):
    if status not in ('approved','rejected','active','disabled'):
        raise ValueError('Неизвестный статус.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not old: raise ValueError('Заявка не найдена.')
        transitions={'pending':{'approved','rejected'},'approved':{'active','rejected','disabled'},'active':{'disabled'},'disabled':set(),'rejected':set()}
        if status not in transitions[old['status']]: raise ValueError('Недопустимый переход статуса.')
        c.execute('UPDATE requests SET status=? WHERE id=?',(status,rid))
        return dict(c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())
