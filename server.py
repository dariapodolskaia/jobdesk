#!/usr/bin/env python3
"""Single-user loopback HTTP server. Same-origin + session CSRF, no public hosting."""
import argparse, base64, csv, datetime as dt, io, json, mimetypes, os, secrets, shutil, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import core
from core import DATA, ROOT, db, now
TOKEN=secrets.token_urlsafe(32)
RUN={'running':False,'results':[],'error':''}
RUN_LOCK=threading.Lock()
CONTACT_RUN={}
CONTACT_LOCK=threading.Lock()

def start_contact_search(jid):
    core.job(jid)
    with CONTACT_LOCK:
        if CONTACT_RUN.get(jid):raise ValueError('Поиск контакта уже выполняется')
        CONTACT_RUN[jid]=True
    def worker():
        try:
            from contact_search import search_contacts
            search_contacts(jid)
        except Exception as e:
            from contact_search import store
            store(jid,{'status':'error','candidates':[],'log':[{'step':'internal','ok':False,'message':str(e)[:500]}]})
        finally:CONTACT_RUN[jid]=False
    threading.Thread(target=worker,daemon=True).start()
    return {'started':True}


def start_sync(source_id=None):
    with RUN_LOCK:
        if RUN['running']: raise ValueError('Сбор уже выполняется')
        RUN.update(running=True,error='',results=[])
    def worker():
        try: RUN['results']=core.sync(source_id)
        except Exception as e: RUN['error']=str(e)
        finally: RUN['running']=False
    threading.Thread(target=worker,daemon=True).start()
    return {'started':True}

def scheduler():
    while True:
        try:
            s=core.settings()
            last=dt.datetime.fromisoformat(s['last_run']) if s.get('last_run') else None
            if s['agent_enabled'] and not RUN['running'] and (last is None or dt.datetime.now(dt.timezone.utc)-last>=dt.timedelta(days=1)): start_sync()
        except Exception: pass
        time.sleep(30)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): pass
    def respond(self,body,status=200,ctype='application/json; charset=utf-8',extra=None):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k,v in (extra or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def check_host(self):
        if self.headers.get('Host') not in (f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}'): raise PermissionError('Недопустимый Host')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://localhost:{self.server.server_port}',f'http://127.0.0.1:{self.server.server_port}'): raise PermissionError('Запрос другого сайта запрещён')
        if self.headers.get('Sec-Fetch-Site')=='cross-site': raise PermissionError('Запрос другого сайта запрещён')
    def body(self):
        if self.headers.get('X-JobDesk-Token')!=TOKEN: raise PermissionError('Обновите страницу: токен сессии изменился')
        n=int(self.headers.get('Content-Length',0))
        if n<0 or n>22_000_000: raise ValueError('Максимальный размер запроса — 22 МБ')
        if 'application/json' not in self.headers.get('Content-Type',''): raise ValueError('Ожидается JSON')
        obj=json.loads(self.rfile.read(n))
        if not isinstance(obj,dict): raise ValueError('Ожидается объект JSON')
        return obj
    def do_GET(self):
        try:
            self.check_host(); path=urlsplit(self.path).path
            if path=='/api/state':
                from integrations import connected
                with db() as c:
                    jobs=[core.decorate_job(dict(r)) for r in c.execute('SELECT * FROM jobs ORDER BY id DESC')]
                    sources=[dict(r) for r in c.execute('SELECT * FROM sources ORDER BY id')]
                    profile=dict(c.execute('SELECT * FROM profile WHERE id=1').fetchone())
                    events=[dict(r) for r in c.execute('SELECT e.*,j.company FROM events e LEFT JOIN jobs j ON e.job_id=j.id ORDER BY start')]
                self.respond({'token':TOKEN,'jobs':jobs,'sources':sources,'profile':profile,'events':events,'settings':core.settings(),'stages':core.STAGES,'sync':RUN,'telegram':connected(),'contact_running':[k for k,v in CONTACT_RUN.items() if v],'cutoff':core.month_cutoff().date().isoformat()}); return
            if path.startswith('/api/jobs/'):
                self.respond(core.job(int(path.rsplit('/',1)[1]))); return
            if path=='/api/cv.pdf':
                with db() as c: name=c.execute('SELECT pdf FROM profile WHERE id=1').fetchone()[0]
                if not name or not (DATA/name).exists(): raise ValueError('PDF ещё не загружен')
                self.respond((DATA/name).read_bytes(),ctype='application/pdf',extra={'Content-Disposition':'inline; filename="cv.pdf"'}); return
            if path=='/api/search-config':
                from contact_search import search_settings
                self.respond({'serper_configured':bool(search_settings().get('serper_key'))});return
            if path=='/api/export':
                with db() as c:
                    result={table:[dict(r) for r in c.execute('SELECT * FROM '+table)] for table in ('jobs','sources','profile','settings','events','history','messages')}
                self.respond(result,extra={'Content-Disposition':'attachment; filename="jobdesk-export.json"'}); return
            if path=='/api/export.csv':
                out=io.StringIO(); w=csv.writer(out); fields=['id','title','company','url','stage','salary','contact','created','notes']; w.writerow(fields)
                with db() as c:
                    for r in c.execute('SELECT * FROM jobs ORDER BY id DESC'):
                        values=[r[k] for k in fields]
                        w.writerow(["'"+str(x) if str(x).startswith(('=','+','-','@','\t','\r')) else x for x in values])
                self.respond(('\ufeff'+out.getvalue()).encode(),ctype='text/csv; charset=utf-8',extra={'Content-Disposition':'attachment; filename="applications.csv"'}); return
            name='index.html' if path=='/' else path.lstrip('/')
            if name not in ('index.html','app.js','style.css'): self.respond({'error':'Не найдено'},404); return
            self.respond((ROOT/'static'/name).read_bytes(),ctype=mimetypes.guess_type(name)[0]+'; charset=utf-8')
        except PermissionError as e: self.respond({'error':str(e)},403)
        except (ValueError,KeyError) as e: self.respond({'error':str(e)},400)
        except Exception: self.respond({'error':'Внутренняя ошибка. Перезапустите приложение и проверьте данные.'},500)
    def do_POST(self):
        try:
            self.check_host(); b=self.body(); path=urlsplit(self.path).path
            if path=='/api/jobs': result=core.add_job(b)
            elif path.startswith('/api/jobs/'):
                result=core.update_job(int(path.rsplit('/',1)[1]),b)
            elif path=='/api/import-url': result=core.import_url(str(b.get('url','')))
            elif path=='/api/settings': result=core.set_settings(b)
            elif path=='/api/reclassify':
                with db() as c: rows=[dict(r) for r in c.execute('SELECT * FROM jobs')]
                for j in rows: core.update_job(j['id'],{'title':j['title'],'description':j['description']})
                result={'updated':len(rows)}
            elif path=='/api/profile':
                text=str(b.get('body',''))[:100000]
                with db() as c: c.execute('UPDATE profile SET body=? WHERE id=1',(text,))
                result={'saved':True}
            elif path=='/api/profile/pdf':
                raw=base64.b64decode(b.get('content',''),validate=True)
                if len(raw)>15_000_000 or not raw.startswith(b'%PDF-'): raise ValueError('Нужен PDF размером до 15 МБ')
                from pypdf import PdfReader
                reader=PdfReader(io.BytesIO(raw))
                if reader.is_encrypted: raise ValueError('PDF защищён паролем. Загрузите незашифрованную копию.')
                if len(reader.pages)>50: raise ValueError('Для CV допускается до 50 страниц')
                text='\n'.join(p.extract_text() or '' for p in reader.pages)[:100000]
                # Unique filenames make send digests immutable if the active CV is replaced.
                name='cv_'+secrets.token_hex(8)+'.pdf'; (DATA/name).write_bytes(raw)
                with db() as c: c.execute('UPDATE profile SET pdf=?,body=? WHERE id=1',(name,text))
                result={'body':text,'pdf':name,'warning':'Если текст пустой или повреждён, вставьте его вручную. OCR не включён.'}
            elif path=='/api/sources':
                kind=str(b.get('kind','')); name=str(b.get('name','')).strip()[:200]; url=str(b.get('url','')).strip(); pattern=str(b.get('pattern',''))[:300]
                if kind not in ('hh','rss','html','browser','telegram','manual'): raise ValueError('Неизвестный тип источника')
                if not name: raise ValueError('Название обязательно')
                if kind=='telegram':
                    from integrations import username
                    url='https://t.me/'+username(url)
                else: url=core.canonical_url(url)
                if not url: raise ValueError('Укажите ссылку')
                if kind=='html' and pattern:
                    import re
                    re.compile(pattern)
                with db() as c:
                    if b.get('id'):
                        c.execute('UPDATE sources SET name=?,kind=?,url=?,pattern=?,enabled=? WHERE id=?',(name,kind,url,pattern,int(bool(b.get('enabled',True))),int(b['id'])))
                        sid=int(b['id'])
                    else: sid=c.execute('INSERT INTO sources(name,kind,url,pattern) VALUES(?,?,?,?)',(name,kind,url,pattern)).lastrowid
                result={'id':sid}
            elif path=='/api/sources/delete':
                with db() as c: c.execute('DELETE FROM sources WHERE id=?',(int(b['id']),))
                result={'deleted':True}
            elif path=='/api/hh/diagnose':result=core.diagnose_hh()
            elif path=='/api/search-config':
                from contact_search import save_search_key
                save_search_key(b.get('serper_key',''));result={'saved':True}
            elif path=='/api/contacts/search':result=start_contact_search(int(b['job_id']))
            elif path=='/api/contacts/select':
                jid=int(b['job_id']);j=core.job(jid);items=json.loads(j['contact_candidates']);index=int(b['index'])
                if index<0 or index>=len(items):raise ValueError('Контакт не найден')
                result=core.update_job(jid,{'contact':items[index]['value'],'contact_source':items[index]['source']})
            elif path=='/api/sync': result=start_sync(int(b['source_id']) if b.get('source_id') else None)
            elif path=='/api/events':
                title=str(b.get('title','')).strip()[:300]; start=str(b.get('start',''))
                if not title: raise ValueError('Название обязательно')
                if not dt.datetime.fromisoformat(start).tzinfo: raise ValueError('Дата должна включать часовой пояс')
                jid=int(b['job_id']) if b.get('job_id') else None
                with db() as c: eid=c.execute('INSERT INTO events(job_id,title,start,notes) VALUES(?,?,?,?)',(jid,title,start,str(b.get('notes',''))[:5000])).lastrowid
                result={'id':eid}
            elif path=='/api/events/delete':
                with db() as c: c.execute('DELETE FROM events WHERE id=?',(int(b['id']),))
                result={'deleted':True}
            elif path=='/api/telegram/prepare':
                from integrations import prepare
                result=prepare(int(b['job_id']))
            elif path=='/api/telegram/send':
                if b.get('confirmed') is not True: raise ValueError('Нужно явное подтверждение отправки')
                from integrations import send_confirmed
                result=send_confirmed(str(b.get('token','')))
            elif path=='/api/telegram/replies':
                from integrations import read_replies
                result=read_replies(int(b['job_id']))
            else: self.respond({'error':'Не найдено'},404); return
            self.respond(result)
        except PermissionError as e: self.respond({'error':str(e)},403)
        except Exception as e:
            # No tokens, phone, passwords or session paths in responses.
            self.respond({'error':str(e)[:700]},400)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--port',type=int,default=8765); args=p.parse_args()
    core.init()
    # After a crash do not retry a potentially delivered message.
    with db() as c: c.execute("UPDATE confirmations SET state='uncertain' WHERE state='sending'")
    threading.Thread(target=scheduler,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'JobDesk: http://localhost:{args.port}\nОстановка: Ctrl+C',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=='__main__': main()
