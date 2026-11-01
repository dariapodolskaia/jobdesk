"""Telegram transport. Authorization is interactive CLI; sending is only called by confirmed API."""
import asyncio, hashlib, json, os, re, secrets, threading
from datetime import datetime, timezone, timedelta
from core import DATA, db, job, now, update_job, month_cutoff, settings
TG_LOCK=threading.Lock()

def config():
    path=DATA/'telegram.json'
    if not path.exists(): raise ValueError('Telegram не подключён. Остановите приложение и запустите scripts/connect_telegram.py по инструкции.')
    return json.loads(path.read_text())

def connected(): return (DATA/'telegram.json').exists() and (DATA/'telegram.session').exists()

def telegram_run(fn):
    if not TG_LOCK.acquire(blocking=False): raise ValueError('Telegram занят другой операцией. Повторите позже.')
    async def run():
        from telethon import TelegramClient
        cfg=config(); client=TelegramClient(str(DATA/'telegram'),int(cfg['api_id']),cfg['api_hash'],flood_sleep_threshold=0)
        try:
            await client.connect()
            if not await client.is_user_authorized(): raise ValueError('Сессия Telegram недействительна. Подключите аккаунт заново.')
            return await asyncio.wait_for(fn(client),timeout=90)
        finally: await client.disconnect()
    try: return asyncio.run(run())
    except ImportError: raise ValueError('Не установлены зависимости Telegram. Запустите setup.command.') from None
    finally: TG_LOCK.release()

def username(value):
    value=value.strip(); value=re.sub(r'^https?://t\.me/','',value).strip('/').lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}',value): raise ValueError('Укажите публичный username Telegram: @recruiter или https://t.me/recruiter')
    return value

def telegram_collect(url):
    name=username(url)
    async def work(client):
        out=[]
        async for msg in client.iter_messages(name,limit=2000):
            if msg.date and msg.date<month_cutoff(): break
            text=msg.message or ''
            if not text.strip(): continue
            lines=[x.strip() for x in text.splitlines() if x.strip()]
            out.append({'published_at':msg.date.isoformat() if msg.date else '', 'title':next((line for line in lines[:10] if re.search(r'(?<!\w)qa(?!\w)|тестировщик',line,re.I)),lines[0])[:300], 'description':text,'url':f'https://t.me/{name}/{msg.id}'})
        return out
    return telegram_run(work)

def telegram_search_company(company,channels):
    async def work(client):
        posts=[];errors=[]
        for channel in channels:
            try:
                name=username(channel['url'])
                async for msg in client.iter_messages(name,search=company,limit=10):
                    if msg.message: posts.append({'text':msg.message,'url':f'https://t.me/{name}/{msg.id}','date':msg.date.isoformat() if msg.date else ''})
            except Exception as e:errors.append({'channel':channel['name'],'message':str(e)[:250]})
        posts.sort(key=lambda x:x['date'],reverse=True)
        return posts[:10],errors
    return telegram_run(work)

def digest(j):
    with db() as c: pdf=c.execute('SELECT pdf FROM profile WHERE id=1').fetchone()[0]
    path=DATA/pdf if pdf else None
    pdfhash=hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else ''
    body={'recipient':username(j['contact']),'message':j['letter'],'pdf_sha256':pdfhash,'job_id':j['id']}
    return hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(), pdf

def prepare(jid):
    j=job(jid)
    if not j['letter'].strip(): raise ValueError('Сначала сохраните текст письма')
    if not j['contact_source'].strip(): raise ValueError('Добавьте источник контакта и проверьте получателя')
    if len(j['letter'])>1024: raise ValueError('Письмо с PDF в Telegram должно быть не длиннее 1024 знаков')
    d,pdf=digest(j)
    if not pdf: raise ValueError('Загрузите PDF резюме в профиле')
    if not connected(): raise ValueError('Сначала подключите Telegram по инструкции в настройках')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM confirmations WHERE job_id=? AND state IN ('sending','sent','uncertain')",(jid,)).fetchone(): raise ValueError('По этой карточке уже была отправка или неопределённый результат. Проверьте историю в Telegram; повторная отправка заблокирована.')
        c.execute("UPDATE confirmations SET state='expired' WHERE job_id=? AND state='pending'",(jid,))
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO confirmations(token,job_id,digest,created) VALUES(?,?,?,?)',(token,jid,d,now()))
    return {'token':token,'recipient':'@'+username(j['contact']),'message':j['letter'],'pdf':pdf,'job_id':jid}

def send_confirmed(token):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM confirmations WHERE token=?',(token,)).fetchone()
        if not row or row['state']!='pending': raise ValueError('Подтверждение уже использовано или недействительно')
        if datetime.fromisoformat(row['created'])<datetime.now(timezone.utc)-timedelta(minutes=10): raise ValueError('Подтверждение истекло. Откройте предпросмотр заново.')
        j=job(row['job_id']); d,pdf=digest(j)
        if d!=row['digest']: raise ValueError('Текст, контакт или PDF изменились. Откройте предпросмотр заново.')
        c.execute("UPDATE confirmations SET state='sending' WHERE token=?",(token,))
    # Any transport error is ambiguous: never automatically retry.
    async def work(client):
        from telethon.tl.types import User
        entity=await client.get_entity(username(j['contact']))
        if not isinstance(entity,User) or entity.bot or entity.deleted: raise ValueError('Получатель должен быть личным аккаунтом, не каналом/группой/ботом')
        # One document message avoids partial text/PDF delivery. Caption limit is 1024.
        if len(j['letter'])>1024: raise ValueError('Письмо с PDF в Telegram ограничено 1024 знаками. Сократите письмо.')
        result=await client.send_file(entity,str(DATA/pdf),caption=j['letter'],parse_mode=None)
        return {'message_id':result.id,'peer_id':entity.id}
    try: result=telegram_run(work)
    except Exception:
        with db() as c:
            c.execute("UPDATE confirmations SET state='uncertain' WHERE token=?",(token,))
            c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(j['id'],now(),'Отправка не подтверждена транспортом. Проверьте Telegram вручную. Повтор заблокирован.'))
        raise ValueError('Отправка не подтверждена. Проверьте Telegram вручную: сообщение могло уйти. Автоповтор заблокирован.') from None
    with db() as c:
        c.execute("UPDATE confirmations SET state='sent' WHERE token=?",(token,))
        c.execute('INSERT INTO messages(job_id,external_id,direction,body,at) VALUES(?,?,?,?,?)',(j['id'],f"tg:{result['peer_id']}:{result['message_id']}",'out',j['letter'],now()))
    update_job(j['id'],{'stage':'sent'})
    return result

def read_replies(jid):
    j=job(jid)
    name=username(j['contact'])
    with db() as c:
        sent=c.execute("SELECT * FROM messages WHERE job_id=? AND direction='out' ORDER BY id DESC LIMIT 1",(jid,)).fetchone()
    if not sent: raise ValueError('Сначала нужна подтверждённая отправка из этой карточки')
    ext=sent['external_id'].split(':'); peer_id=int(ext[1]); after=int(ext[2])
    async def work(client):
        entity=await client.get_entity(name)
        if entity.id!=peer_id: raise ValueError('Контакт изменился. История не загружена.')
        rows=[]
        async for m in client.iter_messages(entity,limit=100,min_id=after):
            if m.out or not m.message: continue
            rows.append((m.id,m.message,m.date.isoformat()))
        return rows
    rows=telegram_run(work); added=0
    with db() as c:
        for mid,body,date in rows:
            count=c.execute('INSERT OR IGNORE INTO messages(job_id,external_id,direction,body,at) VALUES(?,?,?,?,?)',(jid,f'tg:{peer_id}:{mid}','in',body,date)).rowcount
            added+=count
    return {'added':added,'warning':'Сообщения из диалога после отклика. Если у рекрутера несколько вакансий, проверьте, к какой относится ответ. Этап не изменён.'}
