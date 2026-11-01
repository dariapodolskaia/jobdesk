import asyncio, datetime as dt, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import core, contact_search, integrations


class ContactDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(module,'DATA',Path(self.temp.name)) for module in (core,contact_search)]
        self.patches.append(patch('career_discovery.resolve_career',return_value={'routes':[],'contacts':[],'log':[],'incomplete':False}))
        for p in self.patches:p.start()
        core.init()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def test_hr_profile_does_not_need_job_title_in_company_search(self):
        item={'title':'Contact @hr_plat4mtech','snippet':'HR Manager Plat4m.tech','link':'https://t.me/hr_plat4mtech'}
        self.assertFalse(contact_search.relevant('Middle QA Engineer','Plat4m.Tech',item))
        self.assertTrue(contact_search.relevant('Middle QA Engineer','Plat4m.Tech',item,require_role=False))
        self.assertTrue(contact_search.company_matches('Plat4m.Tech','Компания: Plat4m. tech'))
        self.assertFalse(contact_search.company_matches('Plat4m.Tech','Plat4mation ищет QA'))

    def test_company_hr_query_accepts_google_redirect_to_profile(self):
        html='<a href="https://www.google.com/url?q=https%3A%2F%2Ft.me%2Fhr_plat4mtech">HR Manager Plat4m.tech</a>'
        with patch.object(core,'fetch',return_value=html):query,links=contact_search.google_results('Middle QA Engineer','Plat4m.Tech',purpose='contact')
        self.assertEqual(list(links),['https://t.me/hr_plat4mtech']);self.assertIn('HR',query);self.assertNotIn('QA',query)

    def test_javascript_stub_reported_as_search_failure(self):
        with patch.object(core,'fetch',return_value='<a href="/httpservice/retry/enablejs">Enable JS</a>'):
            with self.assertRaisesRegex(ValueError,'JavaScript'):contact_search.google_results('QA','Acme')

    def test_hidden_telegram_links_and_normalized_company_query(self):
        captured=[]
        class Client:
            async def iter_messages(self,name,**kw):
                captured.append(kw['search'])
                yield SimpleNamespace(id=42,message='Plat4m. Tech ищет Middle QA Engineer. Контакт: HR',date=dt.datetime.now(dt.timezone.utc),entities=[SimpleNamespace(url='https://t.me/hr_plat4mtech')],buttons=None)
        with patch.object(integrations,'telegram_run',side_effect=lambda fn:asyncio.run(fn(Client()))):
            posts,errors=integrations.telegram_search_company('Plat4m.Tech',[{'name':'Jobs','url':'https://t.me/qa_jobs'}])
        self.assertEqual(captured,['Plat4m']);self.assertEqual(errors,[]);self.assertEqual(len(posts),1)
        found=contact_search.extract_contacts(posts[0]['text'],posts[0]['url'],'telegram')
        self.assertEqual(found[0]['value'],'@hr_plat4mtech')

    def test_telegram_runs_even_when_web_has_contact_and_evidence_merges(self):
        jid=core.add_job({'title':'Middle QA Engineer','company':'Plat4m.Tech','description':'Ручное тестирование','published_at':core.now()})['id']
        with core.db() as c:c.execute("INSERT INTO sources(name,kind,url) VALUES('Jobs','telegram','https://t.me/qa_jobs')")
        found=contact_search.extract_contacts('HR Manager Plat4m.tech @hr_plat4mtech','https://t.me/hr_plat4mtech','google')
        posts=[{'text':'Plat4m.Tech Middle QA Engineer: @hr_plat4mtech','url':'https://t.me/qa_jobs/42','date':core.now()}]
        with patch.object(contact_search,'google_results',return_value=('query',['https://t.me/hr_plat4mtech'])),patch.object(contact_search,'page_contacts',return_value=found),patch.object(integrations,'telegram_search_company',return_value=(posts,[])) as search:
            result=contact_search.search_contacts(jid)
        search.assert_called_once();self.assertEqual(result['status'],'found');self.assertEqual(len(result['candidates']),1)
        self.assertEqual(len(result['candidates'][0]['evidence']),2);self.assertEqual(core.job(jid)['contact'],'')

    def test_search_preview_retained_if_page_cannot_be_loaded(self):
        jid=core.add_job({'title':'QA','company':'Plat4m.Tech','description':'desc'})['id']
        links=contact_search.SearchLinks();links.append('https://t.me/hr_plat4mtech');links.results=[{'title':'Contact @hr_plat4mtech','snippet':'HR Manager Plat4m.tech','link':links[0]}]
        with patch.object(contact_search,'google_results',return_value=('query',links)),patch.object(contact_search,'page_contacts',side_effect=ValueError('HTTP 403')):
            result=contact_search.search_contacts(jid)
        self.assertEqual(result['status'],'found');self.assertEqual(result['candidates'][0]['value'],'@hr_plat4mtech');self.assertFalse(result['candidates'][0]['verified'])

    def test_support_address_is_not_hr_contact(self):
        self.assertEqual(contact_search.extract_contacts('Contact support (helpdesk@example.com)','https://example.com','google'),[])
        self.assertTrue(contact_search.extract_contacts('Рекрутер HR: recruiter@example.com','https://example.com','google'))

    def test_manual_title_extracted_from_vacancy_heading(self):
        for text in ['Middle QA Engineer\nКомпания: Plat4m.Tech\nУдалённая работа', 'Вакансия: Middle QA Engineer\nОписание', '# Middle QA Engineer\nТребования']:
            self.assertEqual(core.infer_job_title(text),'Middle QA Engineer')
        result=core.add_job({'description':'Middle QA Engineer\nКомпания: Plat4m.Tech\nУдалённая работа'})
        self.assertEqual(core.job(result['id'])['title'],'Middle QA Engineer')
        self.assertEqual(core.infer_job_title('Описание вакансии\nТребования: знание QA и SQL'), '')

    def test_career_resolution_runs_after_telegram_and_prioritizes_verified_form(self):
        jid=core.add_job({'title':'Middle QA Engineer','company':'Acme Labs','url':'https://hh.ru/vacancy/123','description':'desc'})['id']
        with core.db() as c:c.execute("INSERT INTO sources(name,kind,url) VALUES('Jobs','telegram','https://t.me/qa_jobs')")
        order=[]
        def telegram(*args):order.append('telegram');return [],[]
        def career(job,seeds,**kw):
            order.append('career')
            return {'routes':[{'kind':'career','url':'https://acme.example/jobs/qa#form','verified':True,'application_type':'form','label':'Форма отклика'}],'contacts':[],'log':[],'incomplete':False}
        with patch.object(contact_search,'google_results',return_value=('query',[])),patch.object(integrations,'telegram_search_company',side_effect=telegram),patch('career_discovery.resolve_career',side_effect=career):result=contact_search.search_contacts(jid)
        self.assertEqual(order,['telegram','career']);self.assertEqual(result['status'],'application_found');self.assertEqual(result['routes'][0]['application_type'],'form');self.assertEqual(result['routes'][1]['kind'],'board')


if __name__=='__main__':unittest.main()
