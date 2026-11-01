import datetime as dt, json, os, sqlite3, sys, tempfile, unittest, urllib.parse
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core, contact_search, integrations, browser_sources
UTC=dt.timezone.utc

class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        self.patches=[patch.object(core,'DATA',self.data),patch.object(contact_search,'DATA',self.data),patch.object(integrations,'DATA',self.data)]
        self.patches.append(patch('career_discovery.resolve_career',return_value={'routes':[],'contacts':[],'log':[],'incomplete':False}))
        for p in self.patches:p.start()
        core.init()
    def tearDown(self):
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def add(self,**extra):
        return core.add_job({'title':'Manual QA','company':'Acme Labs','description':'Удалённая работа из любой страны. Тестирование веб-приложений и API.','published_at':core.now(),**extra})['id']
    def test_calendar_month_not_thirty_days(self):
        self.assertEqual(core.month_cutoff(dt.datetime(2026,3,31,17,tzinfo=UTC)),dt.datetime(2026,2,28,tzinfo=UTC))
        self.assertEqual(core.month_cutoff(dt.datetime(2024,3,31,17,tzinfo=UTC)),dt.datetime(2024,2,29,tzinfo=UTC))
        self.assertEqual(core.month_cutoff(dt.datetime(2026,1,5,tzinfo=UTC)),dt.datetime(2025,12,5,tzinfo=UTC))
    def test_date_boundary_unknown_future(self):
        ref=dt.datetime(2026,11,1,17,tzinfo=UTC)
        self.assertEqual(core.publication_status('2026-10-01T00:00:00+00:00',ref),'recent')
        self.assertEqual(core.publication_status('2026-09-30T23:59:59+00:00',ref),'old')
        self.assertEqual(core.publication_status('2026-11-02',ref),'future')
        self.assertEqual(core.publication_status('',ref),'unknown')
        self.assertEqual(core.publication_status('not-a-date',ref),'unknown')
    def test_sync_only_accepts_confirmed_recent_publications(self):
        items=[{'title':'QA','description':'Проверяем API и веб','published_at':core.now()}, {'title':'QA old','published_at':'2020-01-01'}, {'title':'QA no date'}, {'title':'QA future','published_at':'2099-01-01'}]
        with patch.object(core,'collect',return_value=items):result=core.sync(1)[0]
        self.assertEqual(result['new'],1);self.assertEqual(result['old'],1);self.assertEqual(result['undated'],1)
        self.assertIn('будущие 1',result['message'])
        with core.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
    def test_manual_unknown_date_flag_and_existing_dates_expire(self):
        jid=self.add(published_at='');j=core.job(jid)
        self.assertEqual(j['date_status'],'unknown');self.assertIn('Дата публикации не подтверждена',j['reasons'])
        core.update_job(jid,{'published_at':'2020-01-01'})
        self.assertEqual(core.job(jid)['filter_state'],'excluded')
        core.update_job(jid,{'published_at':core.now()})
        self.assertEqual(core.job(jid)['date_status'],'recent')
    def test_hh_public_search_has_email_header_and_date_bounds(self):
        core.set_settings({'include':['qa'],'hh_email':'qa@example.com'})
        calls=[]
        def fake(url,**kw):
            calls.append((url,kw));return json.dumps({'items':[],'pages':0,'found':0})
        with patch.object(core,'fetch',side_effect=fake):out=core.collect({'kind':'hh','url':'https://hh.ru'})
        self.assertEqual(out,[]);url,kw=calls[0];query=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertIn('date_from',query);self.assertIn('date_to',query);self.assertEqual(query['search_field'],['name'])
        self.assertEqual(kw['extra_headers']['HH-User-Agent'],'JobDesk/0.2 (qa@example.com)')
        self.assertNotIn('Authorization',kw['extra_headers'])
        with patch.object(core,'fetch',side_effect=ValueError('HH HTTP 403: captcha')):r=core.diagnose_hh()
        self.assertFalse(r['ok']);self.assertIn('403',r['message'])
    def test_hh_requires_only_feedback_email(self):
        with self.assertRaisesRegex(ValueError,'email'):core.hh_get('vacancies')
    def test_hirehi_link_pattern_matches_real_path(self):
        with core.db() as c:s=dict(c.execute("SELECT * FROM sources WHERE name='HireHi'").fetchone())
        import re
        self.assertTrue(re.search(s['pattern'],'https://hirehi.ru/qa/qa-engineer-manual-200717'))
        self.assertFalse(re.search(s['pattern'],'https://hirehi.ru/vacancies/manual-qa'))
    def test_hirehi_page_extracts_date_company_and_main_text(self):
        page=core.Page();page.feed('<title>Вакансия QA engineer (manual), работа в компании OneDome (01.11.2026) | HireHi</title><nav>Русский текст меню</nav><h1>QA engineer (manual)</h1><h2>Описание</h2><p>'+('Testing APIs and requirements. '*8)+'</p><h2>Похожие вакансии</h2><p>посторонний текст</p>')
        r=core.page_posting(page,'https://hirehi.ru/qa/qa-engineer-manual-200717')
        self.assertEqual(r['published_at'],'2026-11-01');self.assertEqual(r['company'],'OneDome');self.assertEqual(r['title'],'QA engineer (manual)');self.assertNotIn('Русский текст меню',r['description']);self.assertNotIn('посторонний',r['description'])
    def test_rss_date_and_robots_404(self):
        raw='<rss><channel><item><title>QA</title><description>desc</description><pubDate>Sun, 01 Nov 2026 12:00:00 +0300</pubDate></item></channel></rss>'
        with patch.object(core,'fetch',return_value=raw):out=core.collect({'kind':'rss','url':'https://example.com/rss'})
        self.assertIsNotNone(core.parse_date(out[0]['published_at']))
        with patch.object(core,'fetch',side_effect=ValueError('Источник вернул HTTP 404')):self.assertTrue(core.robots_for('https://example.com').can_fetch('JobDeskLocal','https://example.com/jobs'))
    def test_google_relevance_top_three_order(self):
        html=''.join(f'<a href="https://company{i}.example/jobs/qa">Acme Labs Manual QA {i}</a>' for i in range(5))
        html='<a href="https://noise.example">Other company QA</a>'+html
        with patch.object(core,'fetch',return_value=html):q,links=contact_search.google_results('Manual QA','Acme Labs')
        self.assertEqual(q,'вакансия Manual QA Acme Labs');self.assertEqual(len(links),3);self.assertIn('company0',links[0]);self.assertIn('company2',links[-1])
    def test_extraction_email_tg_and_no_bots(self):
        text='Рекрутер: @alex_hr, telegram t.me/recruiter1; email hr@acme.ru. Не контакт @applybot.'
        found=contact_search.extract_contacts(text,'https://example.com/job','google')
        values=[x['value'] for x in found]
        self.assertIn('@alex_hr',values);self.assertIn('@recruiter1',values);self.assertIn('hr@acme.ru',values);self.assertNotIn('@applybot',values)
        self.assertTrue(all(x['source']=='https://example.com/job' and not x['verified'] for x in found))
    def test_google_pages_then_telegram_fallback_and_no_automatic_selection(self):
        jid=self.add()
        with core.db() as c:c.execute("INSERT INTO sources(name,kind,url) VALUES('Канал','telegram','https://t.me/qa_jobs')")
        pages=['https://one.example/job','https://two.example/job','https://three.example/job'];order=[]
        def page(url):order.append(url);return []
        def tg(company,channels):order.append('telegram');self.assertEqual(company,'Acme Labs');return ([{'text':'Acme Labs, пишите @recruiter','url':'https://t.me/qa_jobs/1'}],[])
        with patch.object(contact_search,'google_results',return_value=('query',pages)),patch.object(contact_search,'page_contacts',side_effect=page),patch.object(integrations,'telegram_search_company',side_effect=tg):r=contact_search.search_contacts(jid)
        self.assertEqual(order,pages+['telegram']);self.assertEqual(r['status'],'found');self.assertEqual(core.job(jid)['contact'],'')
        self.assertIn('@recruiter',core.job(jid)['contact_candidates'])
    def test_incomplete_search_is_not_false_not_found(self):
        jid=self.add()
        with patch.object(contact_search,'google_results',side_effect=ValueError('CAPTCHA')):r=contact_search.search_contacts(jid)
        self.assertEqual(r['status'],'partial');self.assertIn('CAPTCHA',json.dumps(r['log']))
    def test_migration_keeps_user_data(self):
        jid=self.add();core.update_job(jid,{'letter':'Моё письмо','notes':'Заметка','stage':'interview'})
        core.init();j=core.job(jid)
        self.assertEqual(j['letter'],'Моё письмо');self.assertEqual(j['stage'],'interview')
    def test_browser_payload_uses_publication_not_created_timestamp(self):
        data={'items':[{'title':'QA','description':'Тестирование '*20,'company':{'name':'Acme'},'url':'/jobs/1','published_at':'2026-11-01'}, {'title':'QA2','description':'Тестирование '*20,'url':'/jobs/2','created_at':'2026-11-01'}]}
        out=list(browser_sources.objects_from_payload(data,'https://example.com'))
        self.assertEqual(out[0]['published_at'],'2026-11-01');self.assertEqual(out[1]['published_at'],'')

if __name__=='__main__':unittest.main(verbosity=2)
