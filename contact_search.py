"""Read-only contact discovery: Google top 3 relevant results, then Telegram top 10 posts."""
import json, os, re, threading, urllib.parse, urllib.request
import core
from core import DATA, Page, db, now
LOCK=threading.Lock()

def search_settings():
    p=DATA/'search.json'
    return json.loads(p.read_text()) if p.exists() else {}

def save_search_key(key):
    if not isinstance(key,str) or len(key)>500 or any(c in key for c in '\r\n'): raise ValueError('Некорректный ключ')
    p=DATA/'search.json';p.write_text(json.dumps({'serper_key':key.strip()}));os.chmod(p,0o600)

def relevant(title,company,item):
    text=(item.get('title','')+' '+item.get('snippet','')+' '+urllib.parse.unquote(item.get('link',''))).casefold()
    companies=[x for x in re.findall(r'[\w]+',company.casefold()) if len(x)>2 and x not in ('ооо','company','group','группа')]
    role=[x for x in re.findall(r'[\w]+',title.casefold()) if len(x)>1 and x not in ('middle','senior','junior','engineer')]
    return bool(companies) and all(x in text for x in companies) and any(x in text for x in role)

def google_results(title,company):
    query=f'вакансия {title} {company}'
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
        seen.add(key);found.append({'value':value,'kind':kind,'source':source,'method':method,'context':text[max(0,match.start()-140):match.end()+140].strip(),'verified':False})
    return found[:30]

def page_contacts(url):
    page=Page();page.feed(core.fetch(url))
    # Preserve mailto/tg hrefs while ignoring scripts, CSS and unrelated embedded strings.
    links='\n'.join(href.removeprefix('mailto:').split('?')[0] for href,label in page.links if href.startswith('mailto:') or re.search(r'(?:t\.me|telegram\.me)/',href))
    return extract_contacts(page.plain()+'\n'+links,url,'google')

def search_contacts(jid):
    j=core.job(jid);log=[];candidates=[];errors=False
    if not j['company'].strip():
        result={'status':'error','candidates':[],'log':[{'step':'input','ok':False,'message':'Укажите компанию в карточке и сохраните изменения.'}]}
        store(jid,result);return result
    try:
        query,urls=google_results(j['title'],j['company'])
        log.append({'step':'google','ok':True,'message':query,'urls':urls})
        for url in urls:
            try:
                found=page_contacts(url);candidates.extend(found);log.append({'step':'page','ok':True,'url':url,'found':len(found)})
            except Exception as e: errors=True;log.append({'step':'page','ok':False,'url':url,'message':str(e)[:400]})
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
    result={'status':'found' if unique else ('partial' if errors else 'not_found'),'candidates':unique,'log':log}
    store(jid,result);return result

def store(jid,result):
    with db() as c:
        c.execute('UPDATE jobs SET contact_status=?,contact_candidates=?,contact_log=?,contact_searched_at=? WHERE id=?',(result['status'],json.dumps(result['candidates'],ensure_ascii=False),json.dumps(result['log'],ensure_ascii=False),now(),jid))
        c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(jid,now(),'Поиск контакта: '+result['status']))
