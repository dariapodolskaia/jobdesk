import asyncio, datetime as dt, json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import core, integrations, contact_search


class CompanyAndContactWindowTests(unittest.TestCase):
    def test_calendar_three_month_boundary(self):
        utc=dt.timezone.utc
        self.assertEqual(core.months_cutoff(3,dt.datetime(2026,5,31,17,tzinfo=utc)),dt.datetime(2026,2,28,tzinfo=utc))
        self.assertEqual(core.months_cutoff(3,dt.datetime(2024,5,31,17,tzinfo=utc)),dt.datetime(2024,2,29,tzinfo=utc))
        self.assertEqual(core.months_cutoff(3,dt.datetime(2026,1,31,17,tzinfo=utc)),dt.datetime(2025,10,31,tzinfo=utc))

    def test_company_from_selected_title_in_multi_company_digest(self):
        title='- Тестировщик ПО QA Junior в GedoTab'
        digest='Подборка для джунов\n- Junior Project Manager в JetBrains\n- Тестировщик ПО QA Junior в GedoTab\n- Аналитик в Ozon'
        self.assertEqual(core.infer_company(title,digest),'GedoTab')
        self.assertEqual(core.infer_company('Middle QA','Компания: Plat4m.Tech\nТребования'),'Plat4m.Tech')
        self.assertEqual(core.infer_company('QA в Москве'),'')
        self.assertEqual(core.infer_company('QA','Компания: One\nКомпания: Two'),'')

    def test_company_recovered_before_search_for_existing_empty_card(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'DATA',Path(folder)),patch.object(contact_search,'DATA',Path(folder)):
            core.init();jid=core.add_job({'title':'Тестировщик ПО QA Junior в GedoTab','description':'описание'})['id']
            with core.db() as c:c.execute("UPDATE jobs SET company='' WHERE id=?",(jid,))
            with patch.object(contact_search,'google_results',side_effect=ValueError('CAPTCHA')),patch('career_discovery.resolve_career',return_value={'routes':[],'contacts':[],'log':[],'incomplete':False}):result=contact_search.search_contacts(jid)
            self.assertEqual(core.job(jid)['company'],'GedoTab');self.assertFalse(any(s['step']=='input' for s in result['log']))

    def test_telegram_window_includes_boundary_and_stops_before_old_posts(self):
        reference=dt.datetime(2026,11,1,17,tzinfo=dt.timezone.utc).astimezone();cutoff=core.months_cutoff(3,reference);seen=[]
        dates=[reference+dt.timedelta(days=1),None,reference-dt.timedelta(days=1),cutoff,cutoff-dt.timedelta(seconds=1),cutoff-dt.timedelta(days=5)]
        class Client:
            async def iter_messages(self,*args,**kwargs):
                for i,date in enumerate(dates):
                    seen.append(i);yield SimpleNamespace(id=i,date=date,message='GedoTab ищет QA. @recruiter',entities=[],buttons=None)
        with patch.object(integrations,'datetime') as clock,patch.object(integrations,'telegram_run',side_effect=lambda fn:asyncio.run(fn(Client()))):
            clock.now.return_value=reference;posts,errors=integrations.telegram_search_company('GedoTab',[{'name':'Jobs','url':'https://t.me/qa_jobs'}])
        self.assertEqual([p['url'] for p in posts],['https://t.me/qa_jobs/2','https://t.me/qa_jobs/3']);self.assertEqual(seen,[0,1,2,3,4]);self.assertEqual(errors,[])

    def test_cached_old_telegram_evidence_does_not_reappear(self):
        old={'value':'@old_hr','kind':'telegram','method':'telegram','source':'https://t.me/qa_jobs/1','published_at':'2020-01-01','context':'old'}
        recent={**old,'value':'@recent_hr','source':'https://t.me/qa_jobs/2','published_at':core.now()}
        unknown={**old,'published_at':''}
        self.assertEqual([c['value'] for c in contact_search.filter_contact_dates([old,recent,unknown])],['@recent_hr'])
        combined={**recent,'method':'google','source':'https://example.com/contact','evidence':[old,{**recent,'method':'google','source':'https://example.com/contact'}]}
        self.assertEqual(len(contact_search.filter_contact_dates([combined])[0]['evidence']),1)
