"""Optional normal-browser reader for public JS vacancy feeds; no protection bypass."""
import datetime as dt, json, re, urllib.parse
import core
from core import Page, from_posting, posting_objects

def objects_from_payload(value,base,depth=0):
    if depth>15:return
    if isinstance(value,list):
        for item in value:yield from objects_from_payload(item,base,depth+1)
    elif isinstance(value,dict):
        # Public JSON responses from the page itself, not invented private API routes.
        title=value.get('title') or value.get('position') or value.get('vacancy_name') or value.get('name')
        description=value.get('description') or value.get('text') or value.get('body') or value.get('content')
        url=value.get('url') or value.get('link') or value.get('source_url') or value.get('vacancy_url')
        pub=value.get('published_at') or value.get('publishedAt') or value.get('datePosted') or value.get('publication_date') or value.get('date_published')
        org=value.get('company') or value.get('employer') or value.get('hiringOrganization') or ''
        if isinstance(org,dict):org=org.get('name','')
        if isinstance(title,str) and isinstance(description,str) and len(description)>80 and (url or pub):
            yield {'title':title,'description':core.plain(description),'url':urllib.parse.urljoin(base,url) if isinstance(url,str) else '', 'company':org if isinstance(org,str) else '', 'published_at':pub or ''}
        for v in value.values():
            if isinstance(v,(list,dict)):yield from objects_from_payload(v,base,depth+1)

def collect_browser(source):
    try:from playwright.sync_api import sync_playwright
    except ImportError:raise ValueError('Для JavaScript-источников выполните: bash setup_browser.command') from None
    url=source['url'];core.check_url(url)
    robot=core.robots_for(url)
    if not robot.can_fetch('JobDeskLocal',url):raise ValueError('robots.txt запрещает чтение страницы')
    with sync_playwright() as p:
        browser=None
        try:
            try:browser=p.chromium.launch(channel='chrome',headless=True)
            except Exception:
                try:browser=p.chromium.launch(headless=True)
                except Exception:raise ValueError('Браузер не установлен. Выполните bash setup_browser.command (или установите Google Chrome).') from None
            context=browser.new_context();page=context.new_page();payloads=[]
            def route(reqroute):
                try:
                    u=reqroute.request.url
                    if u.startswith(('data:','blob:')):reqroute.continue_();return
                    core.check_url(u);reqroute.continue_()
                except Exception:reqroute.abort()
            context.route('**/*',route)
            def capture(response):
                try:
                    if len(payloads)>=40 or 'application/json' not in response.headers.get('content-type',''):return
                    if urllib.parse.urlsplit(response.url).hostname!=urllib.parse.urlsplit(url).hostname:return
                    text=response.text()
                    if len(text)<=4_000_000:payloads.append(json.loads(text))
                except Exception:pass
            page.on('response',capture)
            response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
            if response and response.status>=400:raise ValueError(f'Сайт вернул HTTP {response.status}. Откройте источник вручную, проверьте доступ.')
            page.wait_for_timeout(3500)
            # A few ordinary scrolls allow lazy-loaded lists; this is a bounded scan, not full crawling.
            for _ in range(3):page.evaluate('window.scrollBy(0, window.innerHeight)');page.wait_for_timeout(700)
            html=page.content()
            body=page.locator('body').inner_text()
            if re.search(r'Что-то пошло не так\s*Попробуйте ещё раз',body,re.I):
                raise ValueError('Источник сам показывает «Что-то пошло не так». Страница вакансий не загрузилась; это ошибка сайта, а не отсутствие вакансий.')
            if re.search(r'captcha|проверка, что вы не робот|verify you are human|just a moment',page.title(),re.I):raise ValueError('Сайт требует проверку/капчу. Автообход отсутствует; используйте ручной импорт.')
            parsed=Page();parsed.feed(html);out=[]
            for script in parsed.scripts:
                try:
                    for j in posting_objects(json.loads(script)):out.append(from_posting(j,url))
                except (ValueError,TypeError):pass
            for payload in payloads:out.extend(objects_from_payload(payload,url))
            if not out and 'find.dreamoffer.app' in url and 'дополнительной защитой от автоматических запросов' in body:
                raise ValueError('Dream Offer не загрузил вакансии: сайт работает в ограниченном режиме и запрашивает согласия / подтверждение действия. Проверьте доступ вручную; другие источники продолжают работать.')
            pattern=source.get('pattern') or r'/(?:vacanc(?:y|ies)|jobs?)/[^/?]+|/[^/?]+/[^/?]*-\d+(?:[/?]|$)'
            rx=re.compile(pattern);links=[]
            for href,label in parsed.links:
                candidate=urllib.parse.urljoin(url,href)
                if urllib.parse.urlsplit(candidate).netloc!=urllib.parse.urlsplit(url).netloc or not rx.search(candidate):continue
                if candidate not in links and robot.can_fetch('JobDeskLocal',candidate):links.append(candidate)
            for candidate in links[:25]:
                try:
                    page.goto(candidate,wait_until='domcontentloaded',timeout=20000);page.wait_for_timeout(800)
                    detail=Page();detail.feed(page.content());found=False
                    for script in detail.scripts:
                        try:
                            for j in posting_objects(json.loads(script)):out.append(from_posting(j,candidate));found=True
                        except (ValueError,TypeError):pass
                    if not found:out.append(core.page_posting(detail,candidate))
                except Exception:continue
            if not out:
                if 'find.dreamoffer.app' in url and 'дополнительной защитой от автоматических запросов' in body:
                    raise ValueError('Dream Offer не загрузил вакансии: сайт работает в ограниченном режиме и запрашивает согласия / подтверждение действия. Проверьте доступ вручную; другие источники продолжают работать.')
                raise ValueError('Страница отрисована, но карточки не распознаны. Нужен адаптер структуры этого сайта; это не результат «0 вакансий». Пришлите текст этой ошибки и адрес источника.')
            return out
        finally:
            if browser:browser.close()
