import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import core, contact_search, source_readers


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(module,'DATA',Path(self.temp.name)) for module in (core,contact_search)]
        self.patches.append(patch('career_discovery.resolve_career',return_value={'routes':[],'contacts':[],'log':[],'incomplete':False}))
        for p in self.patches:p.start()
        core.init();core.set_settings({'auto_contacts':False})

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def vacancy(self,**kw):
        return {'title':'Manual QA','company':'Acme Labs','description':'Удалённая работа из любой страны. Ручное тестирование API и веб-приложений. Проверка требований, оформление баг-репортов и регрессионное тестирование.','published_at':core.now(),'url':'https://one.example/jobs/1',**kw}

    def test_cross_site_duplicates_keep_work_and_all_sources(self):
        first=core.add_job(self.vacancy())['id']
        core.update_job(first,{'stage':'interview','letter':'Сохранённое письмо'})
        result=core.add_job(self.vacancy(url='https://two.example/jobs/2',description=self.vacancy()['description']+' Дополнительные сведения.'))
        self.assertTrue(result['duplicate']);self.assertEqual(result['id'],first)
        j=core.job(first);self.assertEqual(j['letter'],'Сохранённое письмо');self.assertEqual(j['stage'],'interview');self.assertEqual(len(j['sources']),2)
        self.assertTrue(core.add_job(self.vacancy(url='https://two.example/jobs/2',title='QA renamed'))['duplicate'])

    def test_different_openings_not_merged_by_role_alone(self):
        a=core.add_job(self.vacancy())
        b=core.add_job(self.vacancy(url='https://two.example/jobs/2',description='Совсем другой продукт и обязанности. '*10))
        self.assertNotEqual(a['id'],b['id'])

    def test_duplicate_does_not_resurrect_old_job(self):
        first=core.add_job(self.vacancy(published_at='2026-06-01'))['id']
        core.add_job(self.vacancy())
        self.assertEqual(core.job(first)['date_status'],'old')

    def test_hh_regional_urls_are_one_vacancy(self):
        self.assertEqual(core.canonical_url('https://spb.hh.ru/vacancy/123?from=search&hhtmFrom=x'),'https://hh.ru/vacancy/123')
        self.assertEqual(core.canonical_url('https://headhunter.ge/vacancy/123'),'https://hh.ru/vacancy/123')

    def test_source_migration_preserves_filters_and_custom_sources(self):
        core.set_settings({'include':['qa special'],'exclude':['lead']})
        with core.db() as c:
            c.execute("DELETE FROM migrations WHERE name='0.3-sources'")
            c.execute("UPDATE sources SET url=? WHERE name='Talent Move'",('https://talent-move.ru/?s=qa&format=fully-remote',))
            c.execute("UPDATE sources SET url='https://custom.example/jobs' WHERE name='HireHi'")
        core.init()
        self.assertEqual(core.settings()['include'],['qa special']);self.assertEqual(core.settings()['exclude'],['lead'])
        with core.db() as c:
            self.assertEqual(c.execute("SELECT url FROM sources WHERE name='Talent Move'").fetchone()[0],core.SOURCES[1][2])
            self.assertEqual(c.execute("SELECT url FROM sources WHERE name='HireHi'").fetchone()[0],'https://custom.example/jobs')

    def test_follow_pagination_and_report_broken_card(self):
        replies=['User-agent: *\nAllow: /','<a href="/jobs/a">QA</a><a href="/list?page=2">2</a>','<a href="/jobs/b">QA</a><a href="/jobs/c">QA</a>']
        with patch.object(core,'fetch',side_effect=replies),patch.object(core,'import_url',side_effect=[self.vacancy(),ValueError('HTTP 503'),self.vacancy(url='https://example.com/jobs/c')]):
            out=core.collect({'kind':'html','url':'https://example.com/list','pattern':'/jobs/'})
        self.assertEqual(len(out),2);self.assertIn('503',out.warnings[0])

    def test_talent_button_pagination_reads_second_page(self):
        pages=['User-agent: *\nAllow: /','<nav data-max-pages="2" data-current-page="1"></nav><a href="/jobs/a">QA</a>','<nav data-max-pages="2" data-current-page="2"></nav><a href="/jobs/b">QA</a>']
        with patch.object(core,'fetch',side_effect=pages) as fetch,patch.object(core,'import_url',side_effect=[self.vacancy(),self.vacancy(url='https://talent-move.ru/jobs/b')]):
            items=core.collect({'kind':'html','url':'https://talent-move.ru/job-category/qa/','pattern':'/jobs/'})
        self.assertEqual(len(items),2);self.assertEqual(fetch.call_args.args[0],'https://talent-move.ru/job-category/qa/page/2/')

    def test_broken_item_does_not_discard_remaining_source_items(self):
        with patch.object(core,'collect',return_value=[{'title':'' ,'published_at':core.now()},self.vacancy()]):r=core.sync(1)[0]
        self.assertEqual(r['new'],1);self.assertFalse(r['ok']);self.assertIn('Карточки с ошибками: 1',r['message'])

    def test_contact_error_does_not_lose_next_source_or_new_count(self):
        core.set_settings({'auto_contacts':True})
        with patch.object(core,'collect',return_value=[self.vacancy()]),patch.object(contact_search,'search_contacts',side_effect=RuntimeError('discovery failed')):
            results=core.sync()
        self.assertEqual(len(results),5);self.assertTrue(all(r['ok'] for r in results));self.assertEqual(sum(r['new'] for r in results),1)

    def test_existing_unsearched_jobs_are_enriched(self):
        jid=core.add_job(self.vacancy())['id'];core.set_settings({'auto_contacts':True})
        with patch.object(core,'collect',return_value=[]),patch.object(contact_search,'search_contacts') as search:
            core.sync(1)
        search.assert_called_once_with(jid)

    def test_filter_excluded_job_not_added_or_enriched(self):
        with patch.object(core,'collect',return_value=[self.vacancy(title='QA Java')]):r=core.sync(1)[0]
        self.assertEqual(r['new'],0);self.assertEqual(r['excluded'],1)

    def test_english_description_with_russian_section_labels_is_excluded(self):
        text='Описание: Описания нет. Задачи: '+('Testing APIs, gathering requirements, reporting defects. '*15)+' Требования: Strong knowledge. Условия: Remote.'
        self.assertEqual(core.classify('Manual QA',text)['filter_state'],'excluded')

    def test_hh_token_kept_local_and_keys_do_not_overwrite(self):
        contact_search.save_search_key('test-token','hh_token');contact_search.save_search_key('test-serper')
        core.set_settings({'hh_email':'qa@example.com'})
        with patch.object(core,'fetch',return_value='{}') as fetch:core.hh_get('vacancies')
        self.assertEqual(fetch.call_args.kwargs['extra_headers']['Authorization'],'Bearer test-token')
        self.assertNotIn('test-token',json.dumps(core.settings()))
        self.assertEqual(contact_search.search_settings()['serper_key'],'test-serper')

    def test_contact_in_description_survives_google_failure_and_has_fallback(self):
        jid=core.add_job(self.vacancy(description=self.vacancy()['description']+' Пишите @acme_hr.'))['id']
        with patch.object(contact_search,'google_results',side_effect=ValueError('CAPTCHA')):r=contact_search.search_contacts(jid)
        self.assertEqual(r['status'],'found');self.assertEqual(r['candidates'][0]['value'],'@acme_hr')
        self.assertEqual(r['routes'][0]['url'],self.vacancy()['url'])

    def test_unverified_career_search_result_does_not_replace_board(self):
        jid=core.add_job(self.vacancy(url='https://hh.ru/vacancy/123'))['id']
        with patch.object(contact_search,'google_results',return_value=('query',['https://careers.acme.example/jobs/qa'])),patch.object(contact_search,'page_contacts',return_value=[]):r=contact_search.search_contacts(jid)
        self.assertEqual(r['routes'][0]['kind'],'board');self.assertEqual(core.job(jid)['contact'],'')

    def test_first_heading_and_full_talent_body(self):
        raw='<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'QA | Acme','description':'short','datePosted':core.now(),'hiringOrganization':{'name':'Acme'}})+'</script><header><h1>Manual QA</h1><p>Удалённо</p></header><h3>Обязанности</h3><p>Тестирование API. '*20+'</p><div class="job-quality">Оценка</div><h1>TalentMove</h1>'
        with patch.object(core,'fetch',return_value=raw):j=core.import_url('https://talent-move.ru/jobs/qa/')
        self.assertEqual(j['title'],'Manual QA');self.assertIn('Тестирование API',j['description']);self.assertNotIn('Оценка',j['description'])


if __name__=='__main__':unittest.main()
