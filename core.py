"""JobDesk: local data, filtering, ingestion. No AI or outbound messages here."""
import calendar, email.utils, ssl, time, difflib, contextvars
import contextlib, datetime as dt, hashlib, ipaddress, json, os, re, socket, sqlite3, threading, urllib.parse, urllib.request, urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('JOBDESK_DATA', ROOT / 'data')).resolve()
STAGES = {'draft':'Черновики','ready':'Готово','sent':'Отправлено','reply':'Ответ','interview':'Интервью','test':'Тестовое','offer':'Оффер','rejected':'Отказ','archive':'Архив'}
DEFAULTS = {'include':['manual qa','тестировщик','ручной тестировщик','qa','qa middle'], 'exclude':['aqa','automation','python','java'], 'remote_only':True,'exclude_rf_only':True,'russian_only':True,'agent_enabled':False,'auto_contacts':True,'hh_email':'','last_run':None,'collection_period':'month'}
COLLECTION_PERIODS={'day':'1 день','week':'1 неделя','two_weeks':'2 недели','three_weeks':'3 недели','month':'Месяц'}
COLLECTION_WINDOW=contextvars.ContextVar('collection_window',default=None)
SOURCES = [
 ('HeadHunter','hh','https://hh.ru/',''),
 ('Talent Move','html','https://talent-move.ru/job-category/qa/',r'/jobs?/|/vacanc'),
 ('HireHi','html','https://hirehi.ru/vacancies/manual-qa?format=удалённо&format=remote_europe&format=remote_global',r'^https://hirehi\.ru/[^/?]+/[^/?]*-\d+(?:[/?]|$)'),
 ('Dream Offer','browser','https://find.dreamoffer.app/',''),
 ('Jobtoo','browser','https://jobtoo.ru/feed?abroad=1&city=all&mode=from_russia%2Cglobal','')]

def now(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
@contextlib.contextmanager
def db():
    c=sqlite3.connect(DATA/'jobdesk.sqlite3',timeout=30); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        with c: yield c
    finally: c.close()

def init():
    DATA.mkdir(parents=True,exist_ok=True); os.chmod(DATA,0o700)
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, pdf TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, pattern TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT, status TEXT NOT NULL DEFAULT 'Не проверен');
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'Вручную', fingerprint TEXT UNIQUE NOT NULL, stage TEXT NOT NULL DEFAULT 'draft', salary TEXT NOT NULL DEFAULT '', geo TEXT NOT NULL DEFAULT 'review', filter_state TEXT NOT NULL DEFAULT 'review', reasons TEXT NOT NULL DEFAULT '[]', letter TEXT NOT NULL DEFAULT '', cv_notes TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', contact TEXT NOT NULL DEFAULT '', contact_source TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE, at TEXT NOT NULL, action TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE, title TEXT NOT NULL, start TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS confirmations (token TEXT PRIMARY KEY, job_id INTEGER NOT NULL, digest TEXT NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE, external_id TEXT UNIQUE, direction TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL);
        ''')
        cols={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        for name,definition in {'published_at':"TEXT NOT NULL DEFAULT ''",'date_status':"TEXT NOT NULL DEFAULT 'unknown'",'contact_status':"TEXT NOT NULL DEFAULT 'not_searched'",'contact_candidates':"TEXT NOT NULL DEFAULT '[]'",'contact_log':"TEXT NOT NULL DEFAULT '[]'",'contact_searched_at':"TEXT NOT NULL DEFAULT ''",'application_routes':"TEXT NOT NULL DEFAULT '[]'"}.items():
            if name not in cols: c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        c.execute('CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS job_sources (url TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, source TEXT NOT NULL, published_at TEXT NOT NULL DEFAULT \'\')')
        if not c.execute("SELECT 1 FROM migrations WHERE name='0.2'").fetchone():
            c.execute("UPDATE sources SET pattern=? WHERE kind='html' AND name='HireHi' AND url LIKE 'https://hirehi.ru/%'",(r'^https://hirehi\.ru/[^/?]+/[^/?]*-\d+(?:[/?]|$)',))
            c.execute("UPDATE sources SET kind='browser',status='Нужен браузер: bash setup_browser.command' WHERE kind='manual' AND (url LIKE 'https://jobtoo.ru/%' OR url LIKE 'https://find.dreamoffer.app/%')")
            c.execute("INSERT INTO migrations VALUES('0.2')")
        c.execute('INSERT OR IGNORE INTO settings VALUES (1,?)',(json.dumps(DEFAULTS),))
        if not c.execute("SELECT 1 FROM migrations WHERE name='0.3-sources'").fetchone():
            c.execute("UPDATE sources SET url=? WHERE kind='html' AND url=?",(SOURCES[1][2],'https://talent-move.ru/?s=qa&format=fully-remote'))
            c.execute("UPDATE sources SET url=? WHERE kind='html' AND url=?",(SOURCES[2][2],'https://hirehi.ru/vacancies/manual-qa?format=удалённо&format=remote_europe&format=remote_global&search=qa'))
            current=json.loads(c.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])
            current['auto_contacts']=True
            c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(current),))
            c.execute("INSERT INTO migrations VALUES('0.3-sources')")
        seed=ROOT/'seed_profile.txt'
        c.execute('INSERT OR IGNORE INTO profile VALUES (1,?,?)',(seed.read_text() if seed.exists() else '', 'base_cv.pdf' if (DATA/'base_cv.pdf').exists() else ''))
        if not c.execute('SELECT 1 FROM sources LIMIT 1').fetchone():
            c.executemany('INSERT INTO sources(name,kind,url,pattern) VALUES (?,?,?,?)',SOURCES)

def settings():
    with db() as c: return {**DEFAULTS, **json.loads(c.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])}
def set_settings(s):
    old=settings()
    for k in DEFAULTS:
        if k in s and k!='last_run': old[k]=s[k]
    for k in ('include','exclude'):
        if not isinstance(old[k],list) or len(old[k])>100 or any(not isinstance(x,str) or len(x)>100 for x in old[k]): raise ValueError('Некорректный список фильтров')
        old[k]=[x.strip() for x in old[k] if x.strip()]
    for k in ('remote_only','exclude_rf_only','russian_only','agent_enabled','auto_contacts'): old[k]=bool(old[k])
    old['hh_email']=str(old.get('hh_email','')).strip()[:200]
    validate_collection_period(old['collection_period'])
    if old['hh_email'] and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',old['hh_email']): raise ValueError('Некорректный email для HH')
    with db() as c: c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(old),))
    return old

def matches(text,term):
    # Phrase separators include spaces, dash, slash, underscore; short words never match JavaScript/AQA.
    parts=[re.escape(x) for x in re.split(r'[\s_\-/]+',term.casefold()) if x]
    return bool(parts) and bool(re.search(r'(?<!\w)'+r'[\s_\-/]+'.join(parts)+r'(?!\w)',text.casefold()))

def classify(title,description,s=None):
    s=s or settings(); text=(title+'\n'+description).casefold(); reasons=[]; rejected=False; uncertain=False
    if s['include'] and not any(matches(title,x) for x in s['include']): reasons.append('Название не соответствует поисковым фразам'); rejected=True
    bad=[x for x in s['exclude'] if matches(title,x)]
    if bad: reasons.append('Исключения в названии: '+', '.join(bad)); rejected=True
    language_text=re.sub(r'\b(?:описание|задачи|требования|условия)\s*:', '',description.casefold())
    russian=len(re.findall('[а-яё]',language_text));letters=len(re.findall('[a-zа-яё]',language_text))
    if s['russian_only'] and (russian<20 or russian/max(letters,1)<.15):
        if len(description)<100: reasons.append('Недостаточно текста для определения языка'); uncertain=True
        else: reasons.append('Описание не распознано как русскоязычное'); rejected=True
    rf=bool(re.search(r'(?:только|исключительно)\s+(?:из\s+|в\s+|по\s+)?(?:рф|росси[ия])\b|удал[её]н\w*\s+(?:по\s+|из\s+)?(?:рф|росси[ия])\b|(?:нахождени[ея]|проживани[ея]|территории)\s+(?:на\s+|в\s+)?(?:рф|россии)\b',text))
    global_ok=bool(re.search(r'из любой (?:страны|точки)|по всему миру|worldwide|работ\w* из грузии|грузия|global remote',text))
    remote=bool(re.search(r'удал[её]н|remote',text))
    hybrid=bool(re.search(r'(?<!\w)(?:гибрид\w*|hybrid)(?!\w)',text))
    format_text=re.sub(r'\b(?:microsoft\s+office|ms\s+office|office\s*365)\b','',text)
    office=bool(re.search(r'(?<!\w)(?:офис\w*|office|on[\s_-]?site)(?!\w)',format_text))
    if s['remote_only']:
        if hybrid or office: reasons.append('Гибридный или офисный формат'); rejected=True
        elif not remote: reasons.append('Удалённый формат не подтверждён'); uncertain=True
    geo='rf_only' if rf else ('global' if global_ok else 'review')
    if rf and s['exclude_rf_only']: reasons.append('Есть ограничение на работу из РФ'); rejected=True
    elif not global_ok: reasons.append('Возможность работы из Грузии нужно уточнить'); uncertain=True
    return {'filter_state':'excluded' if rejected else ('review' if uncertain else 'match'),'geo':geo,'reasons':reasons}

def canonical_url(url):
    if not url: return ''
    p=urllib.parse.urlsplit(url.strip())
    if p.scheme not in ('http','https') or not p.hostname or p.username: raise ValueError('Нужна ссылка http/https без пароля')
    q=urllib.parse.parse_qsl(p.query,keep_blank_values=True)
    if re.fullmatch(r'(?:[\w-]+\.)?(?:hh\.ru|hh\.kz|hh\.uz|headhunter\.ge|rabota\.by)',p.hostname) and re.fullmatch(r'/vacancy/\d+/?',p.path):
        return 'https://hh.ru'+p.path.rstrip('/')
    return urllib.parse.urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/'),urllib.parse.urlencode(sorted((k,v) for k,v in q if not k.startswith('utm_') and k not in ('from','ref'))),''))

def contacts(text):
    return list(dict.fromkeys(re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|https?://t\.me/[A-Za-z0-9_]+|(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,}',text)))[:20]

def month_cutoff(reference=None):
    reference=reference or dt.datetime.now().astimezone()
    year,month=(reference.year-1,12) if reference.month==1 else (reference.year,reference.month-1)
    return reference.replace(year=year,month=month,day=min(reference.day,calendar.monthrange(year,month)[1]),hour=0,minute=0,second=0,microsecond=0)

def validate_collection_period(period):
    if not isinstance(period,str) or period not in COLLECTION_PERIODS:raise ValueError('Выберите период: 1 день, 1, 2, 3 недели или месяц')
    return period

def collection_reference():
    window=COLLECTION_WINDOW.get()
    return window[0] if window else dt.datetime.now().astimezone()

def collection_cutoff(reference=None,period=None):
    window=COLLECTION_WINDOW.get()
    if window and reference is None and period is None:return window[1]
    reference=reference or dt.datetime.now().astimezone()
    period=validate_collection_period(period if period is not None else settings()['collection_period'])
    return month_cutoff(reference) if period=='month' else reference-dt.timedelta(days={'day':1,'week':7,'two_weeks':14,'three_weeks':21}[period])

def collection_publication_status(value):
    return publication_status(value,collection_reference(),cutoff=collection_cutoff())

def parse_date(value):
    if not value: return None
    if isinstance(value,dt.datetime): result=value
    else:
        try: result=dt.datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
        except ValueError:
            try: result=email.utils.parsedate_to_datetime(str(value))
            except (ValueError,TypeError,IndexError): return None
    if result.tzinfo is None: result=result.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return result

def publication_status(value,reference=None,cutoff=None):
    reference=reference or dt.datetime.now().astimezone(); published=parse_date(value)
    if published is None: return 'unknown'
    if published>reference+dt.timedelta(minutes=5): return 'future'
    return 'recent' if published>=(cutoff if cutoff is not None else month_cutoff(reference)) else 'old'

def with_publication(state,published_at):
    status=publication_status(published_at)
    if status in ('old','future'):
        state['filter_state']='excluded';state['reasons'].append('Публикация старше одного календарного месяца' if status=='old' else 'Дата публикации находится в будущем')
    elif status=='unknown':
        if state['filter_state']=='match':state['filter_state']='review'
        state['reasons'].append('Дата публикации не подтверждена')
    state['date_status']=status
    return state

def hh_get(path):
    contact=settings().get('hh_email','')
    if not contact: raise ValueError('HH: укажите контактный email в Настройках поиска → HeadHunter. Это заголовок API, а не доступ к почте.')
    headers={'HH-User-Agent':f'JobDesk/0.2 ({contact})','User-Agent':f'JobDesk/0.2 ({contact})'}
    from contact_search import search_settings
    token=search_settings().get('hh_token','')
    if token: headers['Authorization']='Bearer '+token
    return json.loads(fetch('https://api.hh.ru/'+path,extra_headers=headers))

def diagnose_hh():
    started=time.monotonic()
    try:
        d=hh_get('vacancies?'+urllib.parse.urlencode({'text':'qa','search_field':'name','per_page':1,'date_from':month_cutoff().isoformat(timespec='seconds')}))
        return {'ok':True,'found':d.get('found',0),'milliseconds':round((time.monotonic()-started)*1000),'message':'HH API доступен. Поиск ограничен последним календарным месяцем.'}
    except Exception as e: return {'ok':False,'message':str(e),'milliseconds':round((time.monotonic()-started)*1000)}

def infer_job_title(text):
    lines=[re.sub(r'^[\s#*•—–-]+|[\s*]+$','',line).strip() for line in str(text or '').splitlines() if line.strip()]
    for line in lines[:12]:
        label=re.match(r'^(?:название вакансии|должность|вакансия|позиция|job title|position)\s*[:：–—-]\s*(.+)',line,re.I)
        if label and len(label[1])<=300:return label[1].strip()
    for line in lines[:6]:
        if len(line)>150 or re.match(r'^(?:компания|требования|обязанности|опыт|навыки|ищем|мы ищем)\b',line,re.I):continue
        if re.search(r'(?<!\w)(?:qa|tester|engineer|developer|analyst|designer|manager|тестировщик\w*|разработчик\w*|аналитик\w*|дизайнер\w*|менеджер\w*)(?!\w)',line,re.I):
            return re.sub(r'^(?:вакансия|должность|позиция)\s+','',line,flags=re.I).strip()
    return ''

def add_job(v):
    description=str(v.get('description','')).strip()[:100000]
    title=(str(v.get('title','')).strip() or infer_job_title(description))[:300]
    if not title: raise ValueError('Укажите название вакансии')
    company=str(v.get('company','')).strip()[:300]; url=canonical_url(str(v.get('url','')).strip())
    fingerprint=hashlib.sha256((url or (title.casefold()+'|'+company.casefold()+'|'+description)).encode()).hexdigest()
    published=parse_date(v.get('published_at')); published_at=published.isoformat() if published else ''
    state=with_publication(classify(title,description),published_at)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute('SELECT id FROM jobs WHERE fingerprint=?',(fingerprint,)).fetchone()
        if not prior and url: prior=c.execute('SELECT job_id FROM job_sources WHERE url=?',(url,)).fetchone()
        if not prior and company:
            prior=c.execute('SELECT id FROM jobs WHERE lower(title)=lower(?) AND lower(company)=lower(?) AND description=?',(title,company,description)).fetchone()
        if not prior and company and url:
            normalize=lambda x:' '.join(re.findall(r'\w+',x.casefold()))
            for row in c.execute("SELECT id,title,company,description,published_at,url FROM jobs WHERE url<>''"):
                if normalize(company)!=normalize(row['company']) or normalize(title)!=normalize(row['title']):continue
                previous=parse_date(row['published_at'])
                if not previous or not published or abs((published-previous).days)>31:continue
                # Same role alone may describe different openings; require matching content too.
                left,right=normalize(description),normalize(row['description'])
                if min(len(left),len(right))>=100 and difflib.SequenceMatcher(None,left[:12000],right[:12000],autojunk=False).ratio()>=.85:
                    prior=(row['id'],);break
        if prior:
            previous=c.execute('SELECT published_at FROM jobs WHERE id=?',(prior[0],)).fetchone()[0]
            if published_at and not parse_date(previous): c.execute('UPDATE jobs SET published_at=?,date_status=? WHERE id=?',(published_at,state['date_status'],prior[0]))
            if url:c.execute('INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?)',(url,prior[0],str(v.get('source','Вручную')),published_at))
            return {'id':prior[0],'duplicate':True}
        cur=c.execute('INSERT INTO jobs(title,company,url,description,source,fingerprint,salary,geo,filter_state,reasons,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(title,company,url,description,str(v.get('source','Вручную'))[:200],fingerprint,str(v.get('salary',''))[:300],state['geo'],state['filter_state'],json.dumps(state['reasons']),now(),now()))
        c.execute('UPDATE jobs SET published_at=?,date_status=? WHERE id=?',(published_at,state['date_status'],cur.lastrowid))
        if v.get('application_routes'):
            c.execute('UPDATE jobs SET application_routes=? WHERE id=?',(json.dumps(v['application_routes'],ensure_ascii=False),cur.lastrowid))
        if url:c.execute('INSERT OR IGNORE INTO job_sources VALUES(?,?,?,?)',(url,cur.lastrowid,str(v.get('source','Вручную')),published_at))
        c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(cur.lastrowid,now(),'Вакансия добавлена'))
        return {'id':cur.lastrowid,'duplicate':False}

def decorate_job(j):
    state=with_publication(classify(j['title'],j['description']),j.get('published_at',''))
    return {**j,'filter_state':state['filter_state'],'geo':state['geo'],'reasons':json.dumps(state['reasons'],ensure_ascii=False),'date_status':state['date_status']}

def job(jid):
    with db() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not row: raise ValueError('Карточка не найдена')
        result=dict(row)
        for table in ('history','events','messages'): result[table]=[dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE job_id=? ORDER BY id DESC',(jid,))]
        result['sources']=[dict(r) for r in c.execute('SELECT url,source,published_at FROM job_sources WHERE job_id=?',(jid,))]
    result=decorate_job(result)
    result['extracted_contacts']=contacts(result['description']); return result

def update_job(jid,v):
    before=job(jid); allowed=('title','company','description','stage','salary','letter','cv_notes','notes','contact','contact_source','published_at')
    data={k:str(v[k]).strip()[:100000] for k in allowed if k in v}
    if 'stage' in data and data['stage'] not in STAGES: raise ValueError('Неизвестный этап')
    if 'title' in data and not data['title']: raise ValueError('Название обязательно')
    if not data: return before
    with db() as c:
        if 'published_at' in data:
            parsed=parse_date(data['published_at'])
            if data['published_at'] and not parsed: raise ValueError('Некорректная дата публикации')
            data['published_at']=parsed.isoformat() if parsed else ''
        if any(k in data for k in ('title','description','published_at')):
            state=with_publication(classify(data.get('title',before['title']),data.get('description',before['description'])),data.get('published_at',before['published_at']))
            data.update(geo=state['geo'],filter_state=state['filter_state'],reasons=json.dumps(state['reasons']),date_status=state['date_status'])
        if 'contact' in data and data['contact']!=before['contact']: data['contact_status']='selected' if data['contact'] else 'not_searched'
        data['updated']=now()
        c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in data)+' WHERE id=?',(*data.values(),jid))
        action='Этап: '+STAGES[data['stage']] if 'stage' in data and data['stage']!=before['stage'] else 'Карточка обновлена'
        c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(jid,now(),action))
    return job(jid)

class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self.links=[]; self.scripts=[]; self.skip=0; self.in_json=False; self.buf=[]; self.href=None; self.label=[]; self.title=''; self.in_title=False; self.meta={}; self.times=[]; self.h1=''; self.in_h1=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta': self.meta[a.get('property') or a.get('name','')]=a.get('content','')
        if tag=='time' and a.get('datetime'): self.times.append(a['datetime'])
        if tag in ('script','style','noscript'):
            self.skip+=1
            if tag=='script' and a.get('type')=='application/ld+json': self.in_json=True; self.buf=[]
        if tag=='a': self.href=a.get('href'); self.label=[]
        if tag=='title': self.in_title=True
        if tag=='h1' and not self.h1: self.in_h1=True
        if tag in ('p','div','br','li','h1','h2','section'): self.text.append('\n')
    def handle_endtag(self,tag):
        if tag=='script' and self.in_json:
            self.scripts.append(''.join(self.buf)); self.in_json=False
        if tag in ('script','style','noscript'): self.skip=max(0,self.skip-1)
        if tag=='a' and self.href: self.links.append((self.href,''.join(self.label).strip())); self.href=None
        if tag=='title': self.in_title=False
        if tag=='h1': self.in_h1=False
    def handle_data(self,data):
        if self.in_json: self.buf.append(data)
        if self.skip: return
        self.text.append(data)
        if self.href: self.label.append(data)
        if self.in_title: self.title+=data
        if self.in_h1: self.h1+=data
    def plain(self): return re.sub(r'\n[ \t]*\n+','\n', ''.join(self.text)).strip()

def plain(html):
    p=Page(); p.feed(html or ''); return p.plain()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def check_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.port not in (None,80,443): raise ValueError('Допустимы только публичные HTTP/HTTPS ссылки на стандартных портах')
    try: addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError: raise ValueError('Не удалось определить адрес сайта')
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise ValueError('Доступ к локальным и служебным адресам запрещён')
    return url

def fetch(url,limit=4_000_000,extra_headers=None):
    # Honour configured system proxies and CA trust; redirects are revalidated.
    for _ in range(5):
        check_url(url)
        url=urllib.parse.quote(url,safe=":/?&=%#@+;,!$'()*[]~-._")
        headers={'User-Agent':'JobDeskLocal/1.0 (personal vacancy reader)','Accept':'application/json,text/html,application/xml;q=0.9'}
        headers.update(extra_headers or {})
        req=urllib.request.Request(url,headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler(),NoRedirect()).open(req,timeout=20) as r:
                b=r.read(limit+1)
                if len(b)>limit: raise ValueError('Ответ сайта слишком большой')
                return b.decode(r.headers.get_content_charset() or 'utf-8',errors='replace')
        except urllib.error.HTTPError as e:
            if e.code in (301,302,303,307,308) and e.headers.get('Location'): url=urllib.parse.urljoin(url,e.headers['Location']); continue
            raw=e.read(20000).decode('utf-8',errors='replace')
            if urllib.parse.urlsplit(url).hostname=='api.hh.ru':
                try:
                    payload=json.loads(raw); detail=json.dumps(payload.get('errors',payload.get('description','')),ensure_ascii=False)[:450]; rid=payload.get('request_id','')
                except ValueError: detail='Ответ не JSON';rid=''
                helptext={400:'Проверьте параметры и контактный email.',401:'Проверьте OAuth-токен HH в настройках.',403:'HH отклонил запрос. Email не даёт права доступа: добавьте действующий OAuth-токен HH в настройках; при captcha_required нужна проверка на стороне HH. Другие источники продолжают работать.',429:'Превышен лимит. Подождите и повторите диагностику.'}.get(e.code,'Повторите позже.')
                raise ValueError(f'HH HTTP {e.code}: {detail}. {helptext} request_id={rid}') from None
            raise ValueError(f'Источник вернул HTTP {e.code}. Доступ ограничен или страница недоступна.') from None
        except urllib.error.URLError as e:
            reason=str(e.reason)
            if 'CERTIFICATE' in reason.upper(): raise ValueError('Ошибка TLS-сертификата. На macOS Python.org запустите Install Certificates.command. Проверка TLS не отключается.') from None
            raise ValueError('Сетевая ошибка: '+reason[:250]+'. Проверьте интернет, VPN/прокси и доступность адреса на вашем устройстве.') from None
    raise ValueError('Слишком много перенаправлений')

def posting_objects(value):
    if isinstance(value,list):
        for item in value: yield from posting_objects(item)
    elif isinstance(value,dict):
        typ=value.get('@type',[])
        if typ=='JobPosting' or isinstance(typ,list) and 'JobPosting' in typ: yield value
        for k in ('@graph','mainEntity','itemListElement','item'): yield from posting_objects(value.get(k))

def from_posting(j,url):
    org=j.get('hiringOrganization') or {}; org=org if isinstance(org,dict) else {}
    sal=j.get('baseSalary') or {}; sal=sal if isinstance(sal,dict) else {}
    work_type=str(j.get('jobLocationType',''))
    if work_type=='TELECOMMUTE':work_type='Удалённо'
    return {'published_at':j.get('datePosted',''),'title':j.get('title') or '', 'company':org.get('name',''),'description':plain(j.get('description',''))+'\n'+work_type+'\n'+json.dumps(j.get('applicantLocationRequirements',''),ensure_ascii=False),'url':j.get('url') or url, 'salary':json.dumps(sal,ensure_ascii=False) if sal else ''}

def import_url(url):
    url=canonical_url(url)
    hh=re.search(r'(?:^|\.)hh\.ru$',urllib.parse.urlsplit(url).hostname or '')
    vid=re.search(r'/vacancy/(\d+)',urllib.parse.urlsplit(url).path)
    if hh and vid:
        d=hh_get('vacancies/'+vid[1]); return {'published_at':d.get('published_at',''),'title':d['name'],'company':(d.get('employer') or {}).get('name',''),'description':plain(d.get('description','')),'url':url,'salary':json.dumps(d.get('salary'),ensure_ascii=False) if d.get('salary') else ''}
    raw=fetch(url);page=Page(); page.feed(raw)
    for script in page.scripts:
        try:
            for item in posting_objects(json.loads(script)):
                result=from_posting(item,url)
                from contact_search import application_routes
                result['application_routes']=application_routes(page,url)
                if urllib.parse.urlsplit(url).hostname=='talent-move.ru':
                    # JSON-LD contains only a marketing summary; use the actual vacancy body.
                    match=re.search(r'<h1\b[^>]*>.*?</header>(.*?)(?:<div\b[^>]*class="job-quality|</article>)',raw,re.S|re.I)
                    if match:
                        header=re.search(r'<h1\b.*?</header>',raw,re.S|re.I)
                        result['description']=plain(header[0]+'\n'+match[1])
                        result['title']=page.h1.strip()
                return result
        except (ValueError,TypeError): pass
    return page_posting(page,url)

def page_posting(page,url):
    text=page.plain();host=urllib.parse.urlsplit(url).hostname or '';company=''
    title=page.h1.strip() or page.title[:300]
    pub=page.meta.get('article:published_time') or page.meta.get('datePublished') or (page.times[0] if page.times else '')
    if not pub:
        date=re.search(r'Опубликовано\s*:\s*(\d{2}\.\d{2}\.\d{4})',text,re.I)
        if not date and host=='hirehi.ru':date=re.search(r'\((\d{2}\.\d{2}\.\d{4})\)',page.title)
        if date:
            try:pub=dt.datetime.strptime(date[1],'%d.%m.%Y').date().isoformat()
            except ValueError:pass
    if host=='hirehi.ru':
        match=re.search(r'работа в компании (.+?)\s*\(',page.title,re.I)
        if match:company=match[1].strip()
        # Do not classify navigation in Russian as the language of an English vacancy.
        lower=text.casefold();begin=lower.find('описание',lower.find(title.casefold()) if title.casefold() in lower else 0);finish=lower.find('похожие вакансии',max(0,begin))
        if begin>=0:text=text[begin:finish if finish>=0 else None]
    elif host=='talent-move.ru':
        segments=page.title.split('|')
        if len(segments)>1:company=segments[1].strip()
        pos=text.find(title)
        if pos>=0:text=text[pos:]
    if len(text)<80:raise ValueError('Сайт не отдал описание. Вставьте текст вакансии вручную.')
    return {'published_at':pub,'title':title,'description':text[:100000],'company':company,'url':url,'warning':'Проверьте извлечённые название, компанию, дату публикации и описание.'}

def robots_for(url):
    p=urllib.parse.urlsplit(url);robot=urllib.robotparser.RobotFileParser()
    try:raw=fetch(p.scheme+'://'+p.netloc+'/robots.txt')
    except ValueError as e:
        if 'HTTP 404' in str(e):raw='User-agent: *\nAllow: /'
        else:raise ValueError('Не удалось проверить robots.txt: '+str(e)) from None
    robot.parse(raw.splitlines());return robot

SYNC_LOCK=threading.Lock()

def collect(source):
    kind=source['kind']; url=source['url']; out=[]
    if kind=='manual': raise ValueError('Источник сохранён как закладка: автоматический адаптер ещё не настроен. Добавьте вакансию ссылкой/текстом или смените тип на RSS/HTML.')
    if kind=='hh':
        s=settings(); terms=s['include'] or ['qa']; seen=set()
        for term in terms:
            for page in range(2):
                query=urllib.parse.urlencode({'text':term,'search_field':'name','per_page':30,'page':page,'order_by':'publication_time','date_from':collection_cutoff().isoformat(timespec='seconds'),'date_to':collection_reference().isoformat(timespec='seconds')})
                d=hh_get('vacancies?'+query)
                for j in d.get('items',[]):
                    if collection_publication_status(j.get('published_at'))!='recent':continue
                    if j['id'] in seen: continue
                    seen.add(j['id'])
                    if any(matches(j['name'],term) for term in s['exclude']): continue
                    if len(out)>=60: return out
                    sn=j.get('snippet') or {}; fmt=' '.join(x.get('name','') for x in j.get('work_format',[]))
                    sntext=plain((sn.get('requirement') or '')+'\n'+(sn.get('responsibility') or ''))
                    desc=sntext+'\n'+fmt
                    # Full text is required for geographic restrictions. Retain review if details unavailable.
                    try:
                        time.sleep(.25)
                        desc=plain(hh_get('vacancies/'+j['id']).get('description',''))+'\n'+fmt
                    except Exception: desc+='\n[Полное описание недоступно: проверьте географию вручную]'
                    out.append({'published_at':j.get('published_at',''),'title':j['name'],'company':(j.get('employer') or {}).get('name',''),'url':j['alternate_url'],'description':desc,'salary':json.dumps(j.get('salary'),ensure_ascii=False) if j.get('salary') else ''})
                if page+1>=d.get('pages',0): break
        return out
    if kind=='browser':
        from browser_sources import collect_browser
        return collect_browser(source)
    if kind=='telegram':
        from integrations import telegram_collect
        return telegram_collect(url)
    if kind=='rss':
        import xml.etree.ElementTree as ET
        raw=fetch(url)
        if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper(): raise ValueError('Небезопасный XML')
        root=ET.fromstring(raw)
        for item in list(root.findall('.//item'))+list(root.findall('.//{http://www.w3.org/2005/Atom}entry')):
            def text(name):
                x=item.find(name)
                if x is None: x=item.find('{http://www.w3.org/2005/Atom}'+name)
                return (x.text or '') if x is not None else ''
            link=text('link'); a=item.find('{http://www.w3.org/2005/Atom}link')
            if a is not None: link=a.get('href','')
            out.append({'published_at':text('pubDate') or text('published'),'title':text('title'),'description':plain(text('description') or text('content') or text('summary')),'url':urllib.parse.urljoin(url,link) if link else ''})
        return out[:200]
    if kind=='html':
        from source_readers import collect_html
        return collect_html(source)
    raise ValueError('Неизвестный тип источника')

def sync(source_id=None,period=None):
    period=validate_collection_period(period if period is not None else settings()['collection_period'])
    if not SYNC_LOCK.acquire(blocking=False): raise ValueError('Сбор уже выполняется')
    reference=dt.datetime.now().astimezone()
    window_token=COLLECTION_WINDOW.set((reference,collection_cutoff(reference,period)))
    try:
        set_settings({'collection_period':period})
        with db() as c:
            sources=[dict(r) for r in c.execute('SELECT * FROM sources WHERE id=?',(source_id,))] if source_id else [dict(r) for r in c.execute('SELECT * FROM sources WHERE enabled=1')]
        results=[]
        for source in sources:
            new=duplicates=old=undated=future=excluded=0
            try:
                items=collect(source);item_errors=[]
                for item in items:
                    try:
                        if not isinstance(item,dict) or not str(item.get('title','')).strip():raise ValueError('Источник передал карточку без названия')
                        pub=collection_publication_status(item.get('published_at'))
                        if pub=='old': old+=1;continue
                        if pub=='unknown': undated+=1;continue
                        if pub=='future': future+=1;continue
                        if classify(item.get('title',''),item.get('description',''))['filter_state']=='excluded':
                            excluded+=1;continue
                        item['source']=source['name']; result=add_job(item)
                        if result['duplicate']: duplicates+=1
                        else: new+=1
                    except Exception as e:item_errors.append(str(e)[:200])
                message=f'Найдено {len(items)}; добавлено {new}; дубли {duplicates}; вне периода {old}; без даты {undated}; будущие {future}; исключены фильтрами {excluded}. Период: {COLLECTION_PERIODS[period]}, с {collection_cutoff().isoformat(timespec="seconds")}.'
                warnings=list(getattr(items,'warnings',[]))
                if item_errors:warnings.append(f'Карточки с ошибками: {len(item_errors)}. '+item_errors[0])
                if warnings:message+=' Неполный сбор: '+'; '.join(warnings)
                ok=not warnings
            except Exception as e: message=str(e)[:500]; ok=False
            with db() as c: c.execute('UPDATE sources SET last_run=?,status=? WHERE id=?',(now(),message,source['id']))
            results.append({'source':source['name'],'ok':ok,'new':new,'duplicates':duplicates,'old':old,'undated':undated,'excluded':excluded,'future':future,'message':message})
        # Enrichment failures must never abort collecting the remaining sources.
        if settings().get('auto_contacts'):
            from contact_search import search_contacts, store
            with db() as c:
                pending=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE contact_status='not_searched'")]
            for candidate in pending:
                if decorate_job(candidate)['filter_state']=='excluded' or collection_publication_status(candidate['published_at'])!='recent':continue
                try:search_contacts(candidate['id'])
                except Exception:
                    store(candidate['id'],{'status':'error','candidates':[],'log':[{'step':'internal','ok':False,'message':'Поиск контакта не завершён. Повторите поиск в карточке.'}]})
        with db() as c:
            s=settings(); s['last_run']=now(); c.execute('UPDATE settings SET body=? WHERE id=1',(json.dumps(s),))
        return results
    finally:
        COLLECTION_WINDOW.reset(window_token)
        SYNC_LOCK.release()
