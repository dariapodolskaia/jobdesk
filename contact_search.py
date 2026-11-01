"""Read-only discovery: vacancy, company HR web searches, and Telegram company posts."""
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

def company_matches(company,text):
    tokens=[t for t in re.findall(r'\w+',company.casefold()) if t not in ('ооо','company','группа')]
    compact=lambda value:re.sub(r'[^\w]','',value.casefold()).replace('_','')
    key=compact(''.join(tokens))
    if len(key)>=4 and key in compact(text):return True
    significant=[t for t in tokens if len(t)>2]
    return bool(significant) and all(re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)',text,re.I) for t in significant)

def company_search_term(company):
    tokens=re.findall(r'\w+',company)
    distinctive=[t for t in tokens if len(t)>=4 and t.casefold() not in ('tech','group','labs','company','группа','компания')]
    return max(distinctive,key=len) if distinctive else company.strip()

def relevant(title,company,item,require_role=True):
    text=(item.get('title','')+' '+item.get('snippet','')+' '+urllib.parse.unquote(item.get('link',''))).casefold()
    role=[x for x in re.findall(r'[\w]+',title.casefold()) if len(x)>1 and x not in ('middle','senior','junior','engineer')]
    return company_matches(company,text) and (not require_role or any(re.search(r'(?<!\w)'+re.escape(x)+r'(?!\w)',text) for x in role))

class SearchLinks(list):
    def __init__(self):super().__init__();self.results=[]

def google_results(title,company,purpose=None):
    query=f'вакансия {title} {company}'
    if purpose=='career':query+=' карьера официальный сайт'
    if purpose=='contact':query=f'{company} HR рекрутер telegram контакт'
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
        if '/httpservice/retry/enablejs' in html:raise ValueError('Google отдал заглушку с требованием JavaScript, а не результаты поиска. Для устойчивого веб-поиска нужен ключ Serper в настройках; поиск в Telegram продолжится.')
        candidates=[]
        for href,label in page.links:
            parsed=urllib.parse.urlsplit(href)
            if parsed.path=='/url' and (not parsed.hostname or parsed.hostname in ('www.google.com','google.com','www.google.ru','google.ru')): href=urllib.parse.parse_qs(parsed.query).get('q',[None])[0] or urllib.parse.parse_qs(parsed.query).get('url',[''])[0]
            p=urllib.parse.urlsplit(href)
            if p.scheme not in ('http','https') or not p.hostname or p.hostname.endswith(('google.com','google.ru','gstatic.com')):continue
            candidates.append({'link':href,'title':label,'snippet':''})
    out=SearchLinks()
    candidates.sort(key=lambda item:route_kind(item.get('link',''))!='career')
    for item in candidates:
        url=item.get('link','')
        if url and relevant(title,company,item,require_role=purpose not in ('contact','career')) and url not in out:
            out.append(url);out.results.append(item)
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
        line=text[text.rfind('\n',0,match.start())+1:text.find('\n',match.end()) if '\n' in text[match.end():] else len(text)]
        if re.search(r'поддержк|техподдерж|support|служба помощи',line,re.I) and not re.search(r'рекрутер|recruiter|(?<!\w)hr(?!\w)',line,re.I):continue
        seen.add(key);found.append({'value':value,'kind':kind,'source':source,'method':method,'context':text[max(0,match.start()-140):match.end()+140].strip(),'verified':False})
    return found[:30]

def telegram_post_url(url):
    p=urllib.parse.urlsplit(url)
    return p.hostname in ('t.me','telegram.me') and bool(re.fullmatch(r'/(?:s/)?(?:[A-Za-z][\w]*|c/\d+)/\d+/?',p.path))

def filter_contact_dates(candidates):
    cutoff=core.months_cutoff(3);result=[]
    for original in candidates:
        item=dict(original)
        def allowed(evidence):
            if evidence.get('method')!='telegram' and not telegram_post_url(evidence.get('source','')):return True
            return core.publication_status(evidence.get('published_at'),cutoff=cutoff)=='recent'
        evidence=[dict(e) for e in item.get('evidence',[item]) if allowed(e)]
        if not evidence:continue
        if not allowed(item):
            for key in ('source','method','context','published_at'):
                if key in evidence[0]:item[key]=evidence[0][key]
        if 'evidence' in item:item['evidence']=evidence
        result.append(item)
    return result

def page_contacts(url):
    page=Page();page.feed(core.fetch(url))
    post_date=''
    if telegram_post_url(url):
        post_date=page.times[0] if page.times else page.meta.get('article:published_time','')
        if not post_date:raise ValueError('Дата Telegram-поста не подтверждена; контакт из него не используется')
        if core.publication_status(post_date,cutoff=core.months_cutoff(3))!='recent':return []
    for script in page.scripts:
        try:
            postings=list(core.posting_objects(json.loads(script)))
            if postings:return extract_contacts(core.plain(postings[0].get('description','')),url,'google')
        except (ValueError,TypeError):pass
    # Preserve mailto/tg hrefs while ignoring scripts, CSS and unrelated embedded strings.
    links='\n'.join(href.removeprefix('mailto:').split('?')[0] for href,label in page.links if href.startswith('mailto:') or re.search(r'(?:t\.me|telegram\.me)/',href))
    found=extract_contacts(page.plain()+'\n'+links,url,'google')
    for contact in found:
        if post_date:contact['published_at']=post_date
    return found

def search_contacts(jid):
    j=core.job(jid);log=[];errors=False
    if not j['company'].strip():
        inferred=core.infer_company(j['title'],j['description'])
        if inferred:
            with db() as c:
                changed=c.execute("UPDATE jobs SET company=?,updated=? WHERE id=? AND trim(company)=''",(inferred,now(),jid)).rowcount
                if changed:c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(jid,now(),'Компания определена из названия/текста вакансии: '+inferred))
            j=core.job(jid);log.append({'step':'company','ok':True,'message':'Компания определена: '+j['company']})
    candidates=extract_contacts(j['description'],j['url'],'vacancy')
    if telegram_post_url(j['url']):
        for contact in candidates:contact['published_at']=j.get('published_at','')
        candidates=filter_contact_dates(candidates)
    routes=json.loads(j.get('application_routes','[]'))
    if j['url'] and not any(r['url']==j['url'] for r in routes):routes.append({'kind':'board','url':j['url'],'verified':True,'label':'Исходная вакансия'})
    log.append({'step':'vacancy','ok':True,'found':len(candidates)})
    if not j['company'].strip():
        result={'status':'found' if candidates else 'partial','candidates':candidates,'routes':routes,'log':log+[{'step':'input','ok':False,'message':'Компания не извлечена: доступен исходный отклик, поиск прямого контакта требует компании.'}]}
        store(jid,result);return result
    visited=set();web_unavailable=False
    for purpose in (None,'contact'):
        step='google' if purpose is None else 'company_hr'
        if web_unavailable:
            log.append({'step':step,'ok':False,'message':'Пропущен: веб-поисковик недоступен на предыдущем шаге; поиск в Telegram продолжается.'});continue
        try:
            query,urls=google_results(j['title'],j['company'],**({'purpose':purpose} if purpose else {}))
            log.append({'step':step,'ok':True,'message':query,'urls':list(urls)})
            for item in getattr(urls,'results',[]):
                if telegram_post_url(item.get('link','')):continue
                preview=item.get('title','')+' '+item.get('snippet','')+' '+item.get('link','')
                for contact in extract_contacts(preview,item['link'],'search_preview'):
                    contact['evidence_level']='search_preview';candidates.append(contact)
            for url in urls:
                if url in visited:continue
                visited.add(url)
                kind=route_kind(url)
                if kind:routes.append({'kind':kind,'url':url,'verified':False,'label':'Карьерная страница — проверьте вакансию' if kind=='career' else 'Вакансия на job board'})
                try:
                    found=page_contacts(url);candidates.extend(found)
                    log.append({'step':'page','ok':True,'url':url,'found':len(found)})
                except Exception as e:errors=True;log.append({'step':'page','ok':False,'url':url,'message':str(e)[:400]})
        except Exception as e:
            errors=True;message=str(e)[:500]
            web_unavailable=bool(re.search(r'JavaScript|CAPTCHA|Serper недоступен|HTTP (?:403|429)',message,re.I))
            log.append({'step':step,'ok':False,'message':message})
    # Company posts are an independent evidence source, including when web search succeeds.
    with db() as c: channels=[dict(r) for r in c.execute("SELECT * FROM sources WHERE kind='telegram'")]
    source_usernames={'@'+c['url'].rstrip('/').rsplit('/',1)[-1].casefold() for c in channels if '/c/' not in c['url']}
    candidates=[candidate for candidate in candidates if candidate['kind']!='telegram' or candidate['value'].casefold() not in source_usernames]
    if channels:
        try:
            from integrations import telegram_search_company
            posts,failures=telegram_search_company(j['company'],channels)
            for post in posts:
                if core.publication_status(post.get('date'),cutoff=core.months_cutoff(3))!='recent':continue
                for contact in extract_contacts(post['text'],post['url'],'telegram'):
                    if contact['kind']=='telegram' and contact['value'].casefold() in source_usernames:continue
                    contact['published_at']=post.get('date','');candidates.append(contact)
            log.append({'step':'telegram','ok':not failures,'posts_checked':len(posts),'channels_checked':getattr(posts,'channels_checked',len(channels)),'channels_total':len(channels),'channels_attempted':getattr(posts,'channels_attempted',len(channels)),'cutoff':getattr(posts,'cutoff',core.months_cutoff(3).isoformat()),'query':company_search_term(j['company']),'urls':[p['url'] for p in posts],'message':'Поиск компании по всем Telegram-источникам за последние 3 календарных месяца; проверены текст и скрытые ссылки.','errors':failures})
            if failures:errors=True
        except Exception as e:errors=True;log.append({'step':'telegram','ok':False,'message':str(e)[:500]})
    else:
        errors=True;log.append({'step':'telegram','ok':False,'message':'Telegram-каналы ещё не добавлены в источники.'})
    if not candidates and not j.get('contact'):
        career_seeds=[r['url'] for r in routes if r.get('kind')=='career']
        if not web_unavailable:
            try:
                career_query,career_urls=google_results(j['title'],j['company'],purpose='career')
                career_seeds.extend(career_urls)
                log.append({'step':'career_search','ok':True,'message':career_query,'urls':list(career_urls)})
            except Exception as e:errors=True;log.append({'step':'career_search','ok':False,'message':str(e)[:400]})
        else:log.append({'step':'career_search','ok':False,'message':'Веб-поиск недоступен; проверяем ссылки вакансии и уже найденные карьерные страницы.'})
        try:
            from career_discovery import resolve_career
            career=resolve_career(j,career_seeds,filters=core.settings())
            # Raw search links are not confirmed application routes.
            routes=[r for r in routes if r.get('kind')!='career' or (r.get('verified') and r.get('application_type') in ('form','email'))]
            routes.extend(career['routes']);candidates.extend(career['contacts']);log.extend(career['log'])
            if career['incomplete']:errors=True
        except Exception as e:errors=True;log.append({'step':'career_resolve','ok':False,'message':str(e)[:400]})
    if errors:
        candidates.extend(filter_contact_dates(json.loads(j.get('contact_candidates','[]'))))
    unique=[];seen={}
    for item in candidates:
        key=(item['kind'],item['value'].casefold())
        evidence={'source':item['source'],'method':item['method'],'context':item.get('context',''),'published_at':item.get('published_at','')}
        if key not in seen:
            item['evidence']=[evidence];seen[key]=item;unique.append(item)
        elif evidence not in seen[key]['evidence']:seen[key]['evidence'].append(evidence)
    # Contacts mentioned in several independent places, and explicit HR names, come first.
    unique.sort(key=lambda c:(-len(c['evidence']),not bool(re.search(r'hr|recruit|рекрут',c['value'],re.I))))
    application_found=any(r.get('verified') and r.get('application_type') in ('form','email') for r in routes)
    result={'status':'found' if unique else ('application_found' if application_found else ('partial' if errors else 'not_found')),'candidates':unique,'routes':sorted({r['url']:r for r in routes}.values(),key=lambda r:0 if r['kind']=='career' and r.get('verified') else (1 if r['kind']=='board' else 2)),'log':log}
    store(jid,result);return result

def store(jid,result):
    with db() as c:
        c.execute('UPDATE jobs SET contact_status=?,contact_candidates=?,contact_log=?,contact_searched_at=?,application_routes=? WHERE id=?',(result['status'],json.dumps(result['candidates'],ensure_ascii=False),json.dumps(result['log'],ensure_ascii=False),now(),json.dumps(result.get('routes',[]),ensure_ascii=False),jid))
        c.execute('INSERT INTO history(job_id,at,action) VALUES(?,?,?)',(jid,now(),'Поиск контакта: '+result['status']))
