"""Read-only contact discovery: Google top 3 relevant results, then Telegram top 10 posts."""
import json, os, re, threading, urllib.parse, urllib.request
import core
from core import DATA, Page, db, now
LOCK=threading.Lock()

def search_settings():
    p=DATA/'search.json'
    return json.loads(p.read_text()) if p.exists() else {}

def save_search_key(key,field='serper_key'):
    if not isinstance(key,str) or len(key)>500 or any(c in key for c in '\r\n'): raise ValueError('Некорректный ключ')
    if field not in ('serper_key','hh_token'):raise ValueError('Неизвестная настройка')
    p=DATA/'search.json';config=search_settings();config[field]=key.strip()
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(config,f)
    os.chmod(p,0o600)


def route_kind(url):
    p=urllib.parse.urlsplit(url);host=p.hostname or ''
    if p.scheme not in ('http','https') or not host:return None
    boards=('hh.ru','hh.kz','hh.uz','headhunter.ge','rabota.by','hirehi.ru','talent-move.ru','jobtoo.ru','dreamoffer.app','linkedin.com','indeed.com','glassdoor.com','t.me','telegram.me')
    if any(host==b or host.endswith('.'+b) for b in boards):return 'board'
    if re.search(r'careers?|jobs?|vacanc|recruit|работа|карьер',host+' '+p.path,re.I):return 'career'
    return None


def application_routes(page,base):
    routes=[]
    for href,label in page.links:
        url=urllib.parse.urljoin(base,href);parts=urllib.parse.urlsplit(url)
        if parts.scheme not in ('http','https') or parts.hostname==urllib.parse.urlsplit(base).hostname:continue
        if not re.search(r'отклик|подать заявку|перейти к вакансии|apply(?: now)?|view (?:job|vacancy)',label,re.I):continue
        kind=route_kind(url) or 'career'
        if kind=='board' and parts.hostname in ('t.me','telegram.me'):continue
        if not any(r['url']==url for r in routes):routes.append({'kind':kind,'url':url,'verified':False,'label':'Ссылка отклика из вакансии'})
    return routes

def relevant(title,company,item):
    text=(item.get('title','')+' '+item.get('snippet','')+' '+urllib.parse.unquote(item.get('link',''))).casefold()
    companies=[x for x in re.findall(r'[\w]+',company.casefold()) if len(x)>2 and x not in ('ооо','company','group','группа')]
    role=[x for x in re.findall(r'[\w]+',title.casefold()) if len(x)>1 and x not in ('middle','senior','junior','engineer')]
    return bool(companies) and all(x in text for x in companies) and any(x in text for x in role)

def google_results(title,company,purpose=None):
    query=f'вакансия {title} {company}'
    if purpose=='career':query+=' карьера официальный сайт'
    key=search_settings().get('serper_key')
    if key:
        req=urllib.request.Request('https://google.serper.dev/search',data=json.dumps({'q':query,'num':10,'hl':'ru'}).encode(),headers={'X-API-KEY':key,'Content-Type':'application/json'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=25) as response: result=json.loads(response.read(1_000_000))
        except Exception: raise ValueError('Google через Serper недоступен: проверьте ключ, лимит и сеть. Ключ не включается в журнал.') from None
        candidates=result.get('organic',[])
    else:
        html=core.fetch('https://www.google.com/search?'+urllib.parse.urlencode({'q':query,'num':10,'hl':'ru'}))
        page=Page();page.feed(html)
        if re.search(r'unusual traffic|необычн\w* трафик|g-recaptcha|consent.google|/sorry/',html,re.I): raise ValueError('Google требует CAPTCHA/согласие. Автообход не выполняется. Для стабильного поиска добавьте ключ Serper в настройках или откройте Google вручную.')
        candidates=[]
        for href,label in page.links:
            if href.startswith('/url?'): href=urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get('q',[None])[0] or urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get('url',[''])[0]
            p=urllib.parse.urlsplit(href)
            if p.scheme not in ('http','https') or not p.hostname or p.hostname.endswith(('google.com','google.ru','gstatic.com')):continue
            candidates.append({'link':href,'title':label,'snippet':''})
    out=[]
    candidates.sort(key=lambda item:route_kind(item.get('link',''))!='career')
    for item in candidates:
        url=item.get('link','')
        if url and relevant(title,company,item) and url not in out:
            out.append(url)
        if len(out)==3:break
    if not out: raise ValueError('В выдаче Google не найдено релевантных ссылок компании и должности. Результат не считается подтверждением отсутствия контактов; можно попробовать ключ Serper или ручной поиск.')
    return query,out

def extract_contacts(text,source,method):
    found=[];seen=set()
    pattern=r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z][A-Za-z0-9_]{3,31}|(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,31}'
    for match in re.finditer(pattern,text):
        value=match.group().rstrip('.,;:')
        if '@' in value[1:] and not value.startswith('@'): kind='email';value=value.casefold()
        else:
            kind='telegram';value='@'+value.rsplit('/',1)[-1].lstrip('@')
            if value.casefold().endswith('bot'):continue
        key=(kind,value.casefold())
        if key in seen:continue
        if re.search(r'support|поддержк|служба помощи',value,re.I):continue
        seen.add(key);found.append({'value':value,'kind':kind,'source':source,'method':method,'context':text[max(0,match.start()-140):match.end()+140].strip(),'verified':False})
    return found[:30]

def page_contacts(url):
    page=Page();page.feed(core.fetch(url))
    for script in page.scripts:
        try:
            postings=list(core.posting_objects(json.loads(script)))
            if postings:return extract_contacts(core.plain(postings[0].get('description','')),url,'google')
        except (ValueError,TypeError):pass
    # Preserve mailto/tg hrefs while ignoring scripts, CSS and unrelated embedded strings.
    links='\n'.join(href.removeprefix('mailto:').split('?')[0] for href,label in page.links if href.startswith('mailto:') or re.search(r'(?:t\.me|telegram\.me)/',href))
    return extract_contacts(page.plain()+'\n'+links,url,'google')

def search_contacts(jid):
    j=core.job(jid);log=[];errors=False
    candidates=extract_contacts(j['description'],j['url'],'vacancy')
    routes=json.loads(j.get('application_routes','[]'))
    if j['url'] and not any(r['url']==j['url'] for r in routes):routes.append({'kind':route_kind(j['url']) or 'board','url':j['url'],'verified':True,'label':'Исходная вакансия'})
    log.append({'step':'vacancy','ok':True,'found':len(candidates)})
    if not j['company'].strip():
        result={'status':'found' if candidates else 'partial','candidates':candidates,'routes':routes,'log':log+[{'step':'input','ok':False,'message':'Компания не извлечена: доступен исходный отклик, поиск прямого контакта требует компании.'}]}
        store(jid,result);return result
    try:
        query,urls=google_results(j['title'],j['company'])
        log.append({'step':'google','ok':True,'message':query,'urls':urls})
        for url in urls:
            kind=route_kind(url)
            if kind:routes.append({'kind':kind,'url':url,'verified':False,'label':'Карьерная страница — проверьте вакансию' if kind=='career' else 'Вакансия на job board'})
            try:
                found=page_contacts(url);candidates.extend(found);log.append({'step':'page','ok':True,'url':url,'found':len(found)})
            except Exception as e: errors=True;log.append({'step':'page','ok':False,'url':url,'message':str(e)[:400]})
        if not candidates and not any(r['kind']=='career' for r in routes):
            try:
                career_query,career_urls=google_results(j['title'],j['company'],purpose='career')
                log.append({'step':'career_search','ok':True,'message':career_query,'urls':career_urls})
                for url in career_urls:
                    if url in urls:continue
                    if route_kind(url)=='career':routes.append({'kind':'career','url':url,'verified':False,'label':'Карьерная страница — проверьте вакансию'})
                    try:candidates.extend(page_contacts(url))
                    except Exception as e:errors=True;log.append({'step':'career_page','ok':False,'url':url,'message':str(e)[:400]})
            except Exception as e:errors=True;log.append({'step':'career_search','ok':False,'message':str(e)[:400]})
    except Exception as e: errors=True;log.append({'step':'google','ok':False,'message':str(e)[:500]})
    # Fall back only when web pages did not yield a contact.
    if not candidates:
        with db() as c: channels=[dict(r) for r in c.execute("SELECT * FROM sources WHERE kind='telegram'")]
        if channels:
            try:
                from integrations import telegram_search_company
                posts,failures=telegram_search_company(j['company'],channels)
                for post in posts: candidates.extend(extract_contacts(post['text'],post['url'],'telegram'))
                log.append({'step':'telegram','ok':not failures,'posts_checked':len(posts),'message':'Проверены первые 10 найденных постов по дате среди каналов из списка источников.','errors':failures})
                if failures:errors=True
            except Exception as e:errors=True;log.append({'step':'telegram','ok':False,'message':str(e)[:500]})
        else:
            errors=True;log.append({'step':'telegram','ok':False,'message':'Telegram-каналы ещё не добавлены в источники.'})
    unique=[];seen=set()
    for item in candidates:
        key=(item['kind'],item['value'].casefold())
        if key not in seen:seen.add(key);unique.append(item)
    result={'status':'found' if unique else ('partial' if errors else 'not_found'),'candidates':unique,'routes':sorted({r['url']:r for r in routes}.values(),key=lambda r:r['kind']!='career'),'log':log}
    store(jid,result);return result

def store(jid,result):
    with db() as c:
        c.execute('UPDATE jobs SET contact_status=?,contact_candidates=?,contact_log=?,contact_searched_at=?,application_routes=? WHERE id=?',(result['status'],json.dumps(result['candidates'],ensure_ascii=False),json.dumps(result['log'],ensure_ascii=False),now(),json.dumps(result.get('routes',[]),ensure_ascii=False),jid))
        c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(jid,now(),'Поиск контакта: '+result['status']))
