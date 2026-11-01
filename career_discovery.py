"""Bounded, read-only verification of a company's vacancy and application route."""
import datetime as dt
import json
import re
import time
import urllib.parse
import core

APPLY=re.compile(r'отклик|отправить\s+резюме|подать\s+заявку|apply|submit application|send (?:cv|resume)',re.I)
CAREER=re.compile(r'карьер|ваканси|careers?|jobs?|open positions|join (?:us|the team)',re.I)
ATS=('greenhouse.io','greenhouse.com','lever.co','ashbyhq.com','workable.com','smartrecruiters.com','teamtailor.com','bamboohr.com','huntflow.ru','talantix.ru','potok.io','careers-page.com')


class ApplicationPage(core.Page):
    def __init__(self):
        super().__init__();self.forms=[];self.form=None;self.iframes=[]

    def handle_starttag(self,tag,attrs):
        super().handle_starttag(tag,attrs);a=dict(attrs)
        if tag=='iframe' and a.get('src'):self.iframes.append(a['src'])
        if tag=='form':
            self.form={'id':a.get('id',''),'fields':[],'text':[]};self.forms.append(self.form)
        if self.form is not None and tag in ('input','textarea','select','button'):
            self.form['fields'].append({'tag':tag,'name':a.get('name','')+' '+a.get('id',''),'type':a.get('type','text'),'value':a.get('value','')})

    def handle_data(self,data):
        super().handle_data(data)
        if self.form is not None and not self.skip:self.form['text'].append(data)

    def handle_endtag(self,tag):
        super().handle_endtag(tag)
        if tag=='form':self.form=None


def matches_title(wanted,actual,company=''):
    normalize=lambda s:re.findall(r'\w+',s.casefold())
    company_words=set(normalize(company))
    wanted_words=[w for w in normalize(wanted) if w not in company_words and w not in ('вакансия','vacancy','job')]
    actual_words=normalize(actual)
    if not wanted_words or not all(w in actual_words for w in wanted_words):return False
    for grade in ('junior','middle','senior','lead','intern'):
        if grade in actual_words and grade not in wanted_words and any(g in wanted_words for g in ('junior','middle','senior','lead','intern')):return False
    if any(w in actual_words and w not in wanted_words for w in ('automation','aqa','автоматизации')):return False
    return True


def has_application_form(form):
    fields=form['fields']
    if any(f['type'].casefold()=='password' for f in fields):return False
    names=' '.join(f['name'] for f in fields).casefold()
    labels=' '.join(form['text'])+' '+' '.join(f['value'] for f in fields)
    identity=any(f['type'].casefold() in ('email','tel') for f in fields) or bool(re.search(r'email|phone|first.?name|full.?name|имя|телефон|почта',names))
    resume=any(f['type'].casefold()=='file' for f in fields) or bool(re.search(r'resume|cv|cover.?letter|резюме',names))
    return identity and (resume or bool(APPLY.search(labels)))


def safe_url(value,base):
    try:return core.canonical_url(urllib.parse.urljoin(base,value))
    except (ValueError,TypeError):return ''


def company_host(company,url):
    from contact_search import company_search_term
    host=urllib.parse.urlsplit(url).hostname or ''
    key=re.sub(r'\W','',company_search_term(company).casefold())
    labels=host.casefold().rstrip('.').split('.')
    suffix='.'.join(labels[-2:])
    index=-3 if suffix in ('co.uk','com.au','co.nz','co.jp','co.in','com.br','com.cn') else -2
    return len(labels)>=abs(index) and len(key)>=4 and key==re.sub(r'\W','',labels[index])


def render_page(url):
    """Read the rendered public page; never fill or submit anything."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:browser=p.chromium.launch(channel='chrome',headless=True)
        except Exception:browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context(service_workers='block')
            def route(request):
                try:
                    if not request.request.url.startswith(('data:','blob:')):core.check_url(request.request.url)
                    request.continue_()
                except Exception:request.abort()
            context.route('**/*',route);page=context.new_page()
            response=page.goto(url,wait_until='domcontentloaded',timeout=15000);page.wait_for_timeout(1500)
            if response and response.status>=400:raise ValueError(f'Страница формы вернула HTTP {response.status}')
            if re.search(r'captcha|verify you are human|just a moment',page.title(),re.I):raise ValueError('Форма требует проверки/капчи')
            result=ApplicationPage();result.feed(page.content());return result
        finally:browser.close()


def resolve_career(job,seeds,max_pages=8,filters=None):
    from contact_search import route_kind,company_matches,extract_contacts
    result={'routes':[],'contacts':[],'log':[],'incomplete':False}
    company=job['company'];wanted=job['title'];trusted=set();robots={};visited=set();queue=[];rendered=False;deadline=time.monotonic()+75
    for url in [job.get('url',''),*seeds]:
        url=safe_url(url,'')
        if url and not any(q[0]==url for q in queue):queue.append((url,0,False,''))
    def enqueue(url,depth,inherited=False,referrer=''):
        if not url or url in visited:return
        for i,old in enumerate(queue):
            if old[0]==url:
                if inherited:queue[i]=(url,min(depth,old[1]),True,referrer)
                return
        queue.append((url,depth,inherited,referrer))
    while queue and len(visited)<max_pages:
        if time.monotonic()>deadline:break
        # Establish employer provenance before visiting search-result ATS links.
        queue.sort(key=lambda item:0 if item[0]==job.get('url') else (1 if urllib.parse.urlsplit(item[0]).hostname in trusted or company_host(company,item[0]) else 2))
        url,depth,inherited,referrer=queue.pop(0)
        if url in visited:continue
        visited.add(url);host=urllib.parse.urlsplit(url).hostname or '';kind=route_kind(url)
        try:
            origin=urllib.parse.urlunsplit((*urllib.parse.urlsplit(url)[:2],'','',''))
            if origin not in robots:robots[origin]=core.robots_for(url)
            if not robots[origin].can_fetch('JobDeskLocal',url):raise ValueError('robots.txt запрещает чтение страницы')
            raw=core.fetch(url);page=ApplicationPage();page.feed(raw)
        except Exception as e:
            result['incomplete']=True;result['log'].append({'step':'career_page','ok':False,'url':url,'message':str(e)[:250]});continue
        postings=[]
        for script in page.scripts:
            try:postings.extend(core.posting_objects(json.loads(script)))
            except (ValueError,TypeError):pass
        # A company's own hostname or a website explicitly supplied by the vacancy
        # establishes a link to the employer; a search snippet alone does not.
        if kind!='board' and company_host(company,url):trusted.add(host)
        for posting in postings:
            org=posting.get('hiringOrganization') or {}
            if isinstance(org,dict) and company_matches(company,org.get('name','')):
                website=safe_url(org.get('url',''),url) if org.get('url') else ''
                if website and route_kind(website)!='board' and (url==job.get('url') or host in trusted or kind=='board'):
                    trusted.add(urllib.parse.urlsplit(website).hostname);enqueue(website,depth+1,False,url)
        matched=[]
        for posting in postings:
            org=posting.get('hiringOrganization') or {}
            org_name=org.get('name','') if isinstance(org,dict) else ''
            if not matches_title(wanted,posting.get('title',''),company) or not (company_matches(company,org_name) or host in trusted):continue
            target=safe_url(posting.get('url') or url,url)
            if target!=url:
                if depth<4:enqueue(target,depth+1,host in trusted,url)
                continue
            matched.append(posting)
        heading_match=matches_title(wanted,page.h1 or page.title.split('|')[0],company)
        company_evidence=host in trusted or company_matches(company,page.title+' '+page.meta.get('og:site_name','')+' '+page.meta.get('description',''))
        display_heading=page.h1 or page.title
        conflicting_heading=bool(re.search(r'(?<!\w)(?:qa|engineer|developer|analyst|manager|тестировщик\w*|разработчик\w*)(?!\w)',display_heading,re.I)) and not heading_match
        vacancy_match=bool(matched) or (heading_match and company_evidence) or (inherited and not conflicting_heading)
        employer_verified=host in trusted
        closed=bool(re.search(r'вакансия\s+(?:закрыта|в архиве)|position (?:has been )?filled|no longer accepting applications|job (?:is )?closed',page.plain(),re.I))
        for posting in matched:
            expiry=core.parse_date(posting.get('validThrough'))
            if expiry and expiry<dt.datetime.now(dt.timezone.utc):closed=True
        if closed:
            result['log'].append({'step':'career_page','ok':True,'url':url,'message':'Вакансия закрыта или срок отклика истёк.'});continue
        if vacancy_match and not inherited:
            detail='\n'.join(core.plain(p.get('description',''))+'\n'+str(p.get('jobLocationType','')) for p in matched) if matched else page.plain()
            constraints=core.classify(wanted,detail,{**core.DEFAULTS,**(filters or {}),'include':[],'exclude':[],'russian_only':False})
            if constraints['filter_state']=='excluded':
                result['log'].append({'step':'career_page','ok':True,'url':url,'message':'На карьерной странице обнаружены неподходящие условия: '+', '.join(constraints['reasons'])});continue
        if vacancy_match and kind!='board':
            if employer_verified and not any(has_application_form(f) for f in page.forms) and not rendered and (any(host==a or host.endswith('.'+a) for a in ATS) or re.search(r'enable javascript|включите javascript|id=[\"\x27](?:root|app)[\"\x27]',raw,re.I)):
                rendered=True
                try:
                    rendered_page=render_page(url)
                    if re.search(r'(?<!\w)(?:qa|engineer|developer|тестировщик\w*)(?!\w)',rendered_page.h1,re.I) and not matches_title(wanted,rendered_page.h1,company):raise ValueError('После отрисовки название вакансии не совпало')
                    page=rendered_page
                except Exception as e:
                    result['incomplete']=True;result['log'].append({'step':'career_render','ok':False,'url':url,'message':str(e)[:250]})
            evidence={'company':company,'title':wanted,'company_verified':employer_verified,'vacancy_match':True,'referrer':referrer,'checked_at':core.now()}
            for form in page.forms:
                if not has_application_form(form):continue
                link=url+('#'+urllib.parse.quote(form['id'],safe='') if form['id'] else '')
                result['routes'].append({'kind':'career','url':link,'verified':employer_verified,'application_type':'form','label':'Форма отклика на вакансию' if employer_verified else 'Форма найдена — сайт компании не подтверждён','evidence':evidence})
            text='\n'.join(core.plain(p.get('description','')) for p in matched)
            contacts=extract_contacts(text,url,'career') if text else []
            for href,label in page.links:
                if href.casefold().startswith('mailto:') and (APPLY.search(label) or re.search(r'(?:^mailto:)(?:hr|jobs|careers|recruit\w*|talent|cv|resume)[+@._-]',href,re.I)):
                    contacts.extend(extract_contacts(label+' '+href[7:].split('?')[0],url,'career'))
            if employer_verified:
                for contact in contacts:
                    contact['evidence_level']='matching_company_vacancy';result['contacts'].append(contact)
                if contacts:result['routes'].append({'kind':'career','url':url,'verified':True,'application_type':'email','label':'Email для отклика: '+contacts[0]['value'],'evidence':evidence})
            if not any(r['url'].split('#')[0]==url for r in result['routes']):
                result['routes'].append({'kind':'career','url':url,'verified':False,'application_type':'vacancy','label':'Вакансия найдена — форма отклика не подтверждена','evidence':evidence})
        result['log'].append({'step':'career_page','ok':True,'url':url,'company_verified':employer_verified,'vacancy_match':vacancy_match,'application_forms':sum(has_application_form(f) for f in page.forms)})
        if any(r.get('verified') and r.get('application_type') in ('form','email') for r in result['routes']):
            queue=[];break
        if depth>=4:continue
        if vacancy_match and employer_verified:
            for frame in page.iframes:
                target=safe_url(frame,url);target_host=urllib.parse.urlsplit(target).hostname or ''
                if any(target_host==a or target_host.endswith('.'+a) for a in ATS):
                    trusted.add(target_host);enqueue(target,depth+1,True,url)
        for href,label in page.links:
            target=safe_url(href,url)
            if not target or target==url:continue
            target_host=urllib.parse.urlsplit(target).hostname or ''
            same_host=target_host==host
            website_label=bool(re.search(r'сайт компании|company website|employer website|официальный сайт',label,re.I))
            if website_label and route_kind(target)!='board':trusted.add(target_host);enqueue(target,depth+1,False,url)
            elif APPLY.search(label) and vacancy_match:
                linked_employer=employer_verified or (url==job.get('url') and bool(matched))
                if linked_employer and (same_host and kind!='board' or any(target_host==a or target_host.endswith('.'+a) for a in ATS)):trusted.add(target_host)
                enqueue(target,depth+1,linked_employer,url)
            elif same_host and kind!='board' and (CAREER.search(label) or matches_title(wanted,label,company)):
                enqueue(target,depth+1,employer_verified and matches_title(wanted,label,company),url)
            elif employer_verified and any(target_host==a or target_host.endswith('.'+a) for a in ATS) and (CAREER.search(label) or matches_title(wanted,label,company)):
                trusted.add(target_host);enqueue(target,depth+1,matches_title(wanted,label,company),url)
    if queue:
        result['incomplete']=True;result['log'].append({'step':'career_limit','ok':False,'message':f'Проверено {max_pages} страниц; часть ссылок не проверена.'})
    result['routes']=list({r['url']:r for r in result['routes']}.values())
    return result
