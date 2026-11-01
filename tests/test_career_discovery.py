import json, unittest
from types import SimpleNamespace
from unittest.mock import patch
import core, career_discovery


def posting(title='Middle QA Engineer',company='Acme Labs',**extra):
    return '<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':title,'hiringOrganization':{'name':company,'url':'https://acme.example'},**extra})+'</script>'


FORM='<form id="apply-form" action="/api/applications" method="post"><input name="full_name"><input type="email" name="email"><input type="file" name="resume"><button>Откликнуться</button></form>'


class CareerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.job={'title':'Middle QA Engineer','company':'Acme Labs','url':'https://hh.ru/vacancy/123'}
        self.robot=patch.object(core,'robots_for',return_value=SimpleNamespace(can_fetch=lambda *a:True));self.robot.start()

    def tearDown(self):self.robot.stop()

    def resolve(self,pages,seeds=None,**kw):
        def fetch(url):
            if url not in pages:raise ValueError('HTTP 404')
            return pages[url]
        with patch.object(core,'fetch',side_effect=fetch) as get:
            result=career_discovery.resolve_career(self.job,seeds or [],**kw)
        return result,get

    def test_follow_company_careers_vacancy_and_form_and_link_to_page_not_post_endpoint(self):
        pages={self.job['url']:posting(), 'https://acme.example':'<h1>Acme Labs</h1><a href="/careers">Карьера</a>', 'https://acme.example/careers':'<h1>Карьера</h1><a href="/jobs/qa">Middle QA Engineer</a>', 'https://acme.example/jobs/qa':'<h1>Middle QA Engineer</h1><a href="/jobs/qa/apply">Откликнуться</a>', 'https://acme.example/jobs/qa/apply':'<h1>Отклик на вакансию</h1>'+FORM}
        result,get=self.resolve(pages)
        routes=[r for r in result['routes'] if r.get('application_type')=='form']
        self.assertEqual(len(routes),1);self.assertTrue(routes[0]['verified']);self.assertEqual(routes[0]['url'],'https://acme.example/jobs/qa/apply#apply-form')
        self.assertNotIn('https://acme.example/api/applications',[call.args[0] for call in get.call_args_list])

    def test_general_career_page_and_newsletter_are_not_application_form(self):
        pages={self.job['url']:posting(),'https://acme.example':'<h1>Careers at Acme Labs</h1><form><input type="email"><button>Подписаться</button></form>'}
        result,_=self.resolve(pages)
        self.assertEqual(result['routes'],[]);self.assertEqual(result['contacts'],[])

    def test_wrong_role_and_seniority_not_promoted_even_if_link_label_matched(self):
        pages={self.job['url']:posting(),'https://acme.example':'<a href="/jobs/senior">Middle QA Engineer</a>','https://acme.example/jobs/senior':'<h1>Senior QA Engineer</h1>'+FORM}
        result,_=self.resolve(pages)
        self.assertFalse(any(r.get('verified') for r in result['routes']))

    def test_closed_vacancy_does_not_offer_form(self):
        pages={self.job['url']:posting(),'https://acme.example':'<h1>Middle QA Engineer</h1>Вакансия закрыта'+FORM}
        result,_=self.resolve(pages)
        self.assertEqual(result['routes'],[])

    def test_expired_schema_not_offered(self):
        pages={self.job['url']:posting(),'https://acme.example':posting(validThrough='2020-01-01')+FORM}
        result,_=self.resolve(pages)
        self.assertEqual(result['routes'],[])

    def test_office_or_hybrid_career_vacancy_is_not_promoted(self):
        pages={self.job['url']:posting(),'https://acme.example':posting(description='Work format: office / hybrid.')+FORM}
        result,_=self.resolve(pages)
        self.assertEqual(result['routes'],[]);self.assertIn('неподходящие условия',result['log'][-1]['message'])

    def test_email_outside_jsonld_is_extracted_with_application_context(self):
        pages={self.job['url']:posting(),'https://acme.example':posting(description='Удалённая работа')+'<a href="mailto:apply@acme.example?subject=QA">Отправить резюме</a><a href="mailto:support@acme.example">Поддержка</a>'}
        result,_=self.resolve(pages)
        self.assertEqual([c['value'] for c in result['contacts']],['apply@acme.example'])
        self.assertTrue(any(r.get('application_type')=='email' and r['verified'] for r in result['routes']))

    def test_ats_form_verified_only_via_company_link(self):
        ats='https://jobs.lever.co/acme/qa/apply'
        pages={self.job['url']:posting(),'https://acme.example':'<a href="'+ats+'">Middle QA Engineer</a>',ats:'<h1>Apply</h1>'+FORM}
        result,_=self.resolve(pages,[ats])
        self.assertTrue(any(r.get('application_type')=='form' and r['verified'] for r in result['routes']))
        self.assertEqual(next(r for r in result['routes'] if r.get('application_type')=='form')['evidence']['referrer'],'https://acme.example')

    def test_unsupported_third_party_cannot_self_confirm_company(self):
        url='https://unknown.example/jobs/qa'
        pages={self.job['url']:'<h1>QA</h1>',url:'<title>Middle QA Engineer | Acme Labs</title><h1>Middle QA Engineer</h1>'+FORM}
        result,_=self.resolve(pages,[url])
        self.assertTrue(result['routes']);self.assertFalse(any(r['verified'] for r in result['routes']))

    def test_company_name_in_foreign_subdomain_not_employer_proof(self):
        self.assertFalse(career_discovery.company_host('Acme Labs','https://acme.other.example/jobs'))
        self.assertTrue(career_discovery.company_host('Acme Labs','https://careers.acme.example/jobs'))

    def test_unknown_search_result_cannot_confirm_itself_with_jsonld(self):
        url='https://unknown.example/jobs/qa'
        pages={self.job['url']:'<h1>QA</h1>',url:posting(hiringOrganization={'name':'Acme Labs','url':'https://unknown.example'})+FORM}
        result,_=self.resolve(pages,[url])
        self.assertFalse(any(r['verified'] for r in result['routes']))

    def test_login_and_search_forms_not_application(self):
        page=career_discovery.ApplicationPage();page.feed('<form><input type="email"><input type="password"><button>Apply</button></form><form><input name="q"><button>Search jobs</button></form>')
        self.assertFalse(any(career_discovery.has_application_form(f) for f in page.forms))

    def test_iframe_application_can_be_followed_from_matched_company_job(self):
        ats='https://boards.greenhouse.io/embed/job_app?token=123'
        pages={self.job['url']:posting(),'https://acme.example':'<h1>Middle QA Engineer</h1><iframe src="'+ats+'"></iframe>',ats:'<h1>Application form</h1>'+FORM}
        result,_=self.resolve(pages)
        self.assertTrue(any(r.get('application_type')=='form' and r['verified'] for r in result['routes']))

    def test_javascript_application_form_can_be_rendered_without_submission(self):
        page=career_discovery.ApplicationPage();page.feed('<h1>Middle QA Engineer</h1>'+FORM)
        pages={self.job['url']:posting(),'https://acme.example':'<h1>Middle QA Engineer</h1><div id="app"></div>'}
        with patch.object(career_discovery,'render_page',return_value=page) as render:
            result,_=self.resolve(pages)
        render.assert_called_once_with('https://acme.example');self.assertTrue(any(r.get('application_type')=='form' for r in result['routes']))

    def test_page_limit_reported_as_incomplete(self):
        result,_=self.resolve({self.job['url']:posting()},max_pages=1)
        self.assertTrue(result['incomplete']);self.assertFalse(result['log'][-1]['ok'])


if __name__=='__main__':unittest.main()
