"""Public HTML listings: follow published pagination, isolate broken cards."""
import re
import urllib.parse
import core
MAX_PAGES=20
MAX_CARDS=400


class Collection(list):
    def __init__(self):
        super().__init__()
        self.warnings=[]


def collect_html(source):
    url=source['url'];pattern=source.get('pattern','')
    if len(pattern)>300:raise ValueError('Слишком длинный шаблон ссылок')
    try:rx=re.compile(pattern or r'/vacanc|/jobs?/')
    except re.error:raise ValueError('Некорректный шаблон ссылок') from None
    robot=core.robots_for(url)
    if not robot.can_fetch('JobDeskLocal',url):raise ValueError('robots.txt запрещает автоматическое чтение этой страницы')
    host=urllib.parse.urlsplit(url).netloc
    pending=[url];visited=set();seen=set();out=Collection();failures=[]
    # A safety bound, explicitly reported rather than advertised as a complete crawl.
    while pending and len(visited)<MAX_PAGES and len(seen)<MAX_CARDS:
        def page_number(u):
            parts=urllib.parse.urlsplit(u)
            value=urllib.parse.parse_qs(parts.query).get('page',['1'])[0]
            match=re.search(r'/page/(\d+)/?$',parts.path)
            return int(match[1]) if match else (int(value) if value.isdigit() else 1)
        pending.sort(key=page_number)
        listing=pending.pop(0)
        if listing in visited:continue
        visited.add(listing)
        try:
            raw=core.fetch(listing);page=core.Page();page.feed(raw)
        except Exception as e:
            failures.append(str(e));continue
        page_items=[]
        if host=='talent-move.ru':
            # This site's public category pagination is rendered as buttons.
            current=re.search(r'data-current-page="(\d+)"',raw)
            maximum=re.search(r'data-max-pages="(\d+)"',raw)
            if current and maximum and int(current[1])<int(maximum[1]):
                base=re.sub(r'/page/\d+/?$','/',urllib.parse.urlsplit(url).path).rstrip('/')
                next_url=urllib.parse.urlunsplit(('https',host,f'{base}/page/{int(current[1])+1}/','',''))
                if next_url not in visited and robot.can_fetch('JobDeskLocal',next_url):pending.append(next_url)
        for script in page.scripts:
            try:
                import json
                page_items.extend(core.from_posting(j,listing) for j in core.posting_objects(json.loads(script)))
            except (ValueError,TypeError):pass
        for href,label in page.links:
            candidate=urllib.parse.urljoin(listing,href)
            parts=urllib.parse.urlsplit(candidate)
            if parts.netloc!=host:continue
            if (re.search(r'/page/\d+/?$',parts.path) or 'page' in urllib.parse.parse_qs(parts.query)) and parts.path.rstrip('/')==urllib.parse.urlsplit(url).path.rstrip('/'):
                if candidate not in visited and candidate not in pending and robot.can_fetch('JobDeskLocal',candidate):pending.append(candidate)
            elif re.search(r'/page/\d+/?$',parts.path) and parts.path.startswith(urllib.parse.urlsplit(url).path.rstrip('/')+'/page/'):
                if candidate not in visited and candidate not in pending and robot.can_fetch('JobDeskLocal',candidate):pending.append(candidate)
            if re.search(r'/page/\d+/?$',parts.path) or 'page' in urllib.parse.parse_qs(parts.query):continue
            if not rx.search(candidate) or candidate==url or candidate in seen or not robot.can_fetch('JobDeskLocal',candidate):continue
            if len(seen)>=MAX_CARDS:break
            seen.add(candidate)
            try:page_items.append(core.import_url(candidate))
            except Exception as e:failures.append(str(e))
        out.extend(page_items)
        # Stop only if every parsed date on this listing is known and older.
        if page_items and all(core.publication_status(j.get('published_at'))=='old' for j in page_items):
            pending=[];break
    if pending or len(seen)>=MAX_CARDS:out.warnings.append(f'Достигнут лимит обхода: {MAX_PAGES} страниц / {MAX_CARDS} карточек; часть ленты не проверена.')
    if failures:out.warnings.append(f'Не прочитано страниц/карточек: {len(failures)}. '+failures[0][:180])
    if not out:raise ValueError('Карточки не прочитаны: '+('; '.join(failures[:2]) if failures else 'Ссылок по шаблону нет. Проверьте шаблон или выберите тип Браузер (JavaScript).'))
    return out
