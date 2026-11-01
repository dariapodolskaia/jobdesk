import base64, contextlib, hashlib, http.client, io, json, os, sys, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core, integrations, server

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.patches=[patch.object(core,'DATA',self.root),patch.object(integrations,'DATA',self.root),patch.object(server,'DATA',self.root)]
        for p in self.patches: p.start()
        core.init()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def add(self,**kw):
        return core.add_job({'title':'Manual QA','company':'Пример','description':'Удалённая работа из любой страны. Проверка API и ручное тестирование веб-приложения. '+kw.pop('extra',''),**kw})['id']
    def test_title_filters_and_technologies(self):
        self.assertTrue(core.matches('Middle QA Engineer','qa'))
        self.assertFalse(core.matches('AQA Engineer','qa'))
        self.assertFalse(core.matches('JavaScript QA','java'))
        self.assertTrue(core.matches('Manual-QA','manual qa'))
        desc='Удалённая работа из любой страны. Тестирование API. Python как дополнительный плюс.'
        self.assertEqual(core.classify('Manual QA',desc)['filter_state'],'match')
        self.assertEqual(core.classify('QA Python',desc)['filter_state'],'excluded')
    def test_geography_is_conservative(self):
        for phrase in ['только РФ','удалёнка РФ','проживание в России']:
            result=core.classify('Тестировщик','Удалённая работа. Тестируем веб-приложение, API и интеграции. '+phrase)
            self.assertEqual(result['filter_state'],'excluded',phrase)
        self.assertEqual(core.classify('QA','Удалённая работа. Тестируем веб-приложения и REST API.')['filter_state'],'review')
        self.assertEqual(core.classify('QA','Гибрид. Тестируем API. Работа из любой страны.')['filter_state'],'excluded')
    def test_dedup_and_persistence(self):
        jid=self.add(url='https://example.com/job/1?utm_source=test')
        r=core.add_job({'title':'Другое название','url':'https://example.com/job/1','description':'Другое описание'})
        self.assertTrue(r['duplicate']);self.assertEqual(r['id'],jid)
        core.update_job(jid,{'stage':'interview','notes':'Сохранённая заметка'})
        core.init();j=core.job(jid)
        self.assertEqual(j['stage'],'interview');self.assertEqual(j['notes'],'Сохранённая заметка')
        self.assertGreaterEqual(len(j['history']),2)
    def test_no_duplicate_loss_for_manual_jobs(self):
        first=self.add(description='Первое описание');second=self.add(description='Второе описание')
        self.assertNotEqual(first,second)
    def test_import_jsonld(self):
        page='<html><script type="application/ld+json">'+json.dumps({'@graph':[{'@type':'JobPosting','title':'QA','hiringOrganization':{'name':'Company'},'description':'<p>Удалённая работа</p>'}]})+'</script></html>'
        with patch.object(core,'fetch',return_value=page):
            r=core.import_url('https://example.com/job/1')
        self.assertEqual(r['title'],'QA');self.assertEqual(r['company'],'Company');self.assertNotIn('<p>',r['description'])
    def test_rss_and_html_collectors(self):
        rss='<rss><channel><item><title>QA</title><description>Удалённая работа</description><link>https://example.com/job/1</link></item></channel></rss>'
        with patch.object(core,'fetch',return_value=rss):
            out=core.collect({'kind':'rss','url':'https://example.com/feed'})
        self.assertEqual(out[0]['url'],'https://example.com/job/1')
        responses=['User-agent: *\nAllow: /','<a href="/jobs/123">QA</a>','<title>QA</title><p>'+('Удалённая работа из любой страны. '*10)+'</p>']
        with patch.object(core,'fetch',side_effect=responses):
            out=core.collect({'kind':'html','url':'https://example.com/list','pattern':'/jobs/'})
        self.assertEqual(out[0]['title'],'QA')
        with patch.object(core,'fetch',return_value='User-agent: *\nDisallow: /'):
            with self.assertRaisesRegex(ValueError,'запрещает'):core.collect({'kind':'html','url':'https://example.com/list','pattern':'/jobs/'})
    def test_oversized_letter_rejected_before_transport(self):
        jid=self.add()
        core.update_job(jid,{'contact':'@recruiter','contact_source':'Публикация','letter':'Я'*1025})
        with patch.object(integrations,'telegram_run') as transport:
            with self.assertRaisesRegex(ValueError,'1024'):integrations.prepare(jid)
            transport.assert_not_called()
    def test_ssrf(self):
        for url in ['file:///etc/passwd','http://127.0.0.1','http://169.254.169.254','http://[::1]']:
            with self.assertRaises(ValueError,msg=url): core.check_url(url)
    def test_sync_reports_failure_and_preserves_existing(self):
        self.add()
        with patch.object(core,'collect',side_effect=ValueError('Источник недоступен')):
            result=core.sync(1)
        self.assertFalse(result[0]['ok'])
        with core.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            self.assertIn('недоступен',c.execute('SELECT status FROM sources WHERE id=1').fetchone()[0])
    def prepared(self):
        jid=self.add()
        (self.root/'test.pdf').write_bytes(b'%PDF-test')
        with core.db() as c:c.execute("UPDATE profile SET pdf='test.pdf' WHERE id=1")
        core.update_job(jid,{'contact':'@recruiter','contact_source':'https://example.com/hr','letter':'Здравствуйте! Прикладываю резюме.'})
        with patch.object(integrations,'connected',return_value=True): token=integrations.prepare(jid)['token']
        return jid,token
    def test_confirmation_binds_content_and_pdf(self):
        jid,token=self.prepared()
        core.update_job(jid,{'letter':'Новый текст'})
        with patch.object(integrations,'telegram_run') as transport:
            with self.assertRaisesRegex(ValueError,'изменились'): integrations.send_confirmed(token)
            transport.assert_not_called()
        core.update_job(jid,{'letter':'Здравствуйте! Прикладываю резюме.'})
        (self.root/'test.pdf').write_bytes(b'%PDF-new')
        with self.assertRaisesRegex(ValueError,'изменились'):integrations.send_confirmed(token)
    def test_confirmation_single_use_and_no_automatic_send(self):
        jid,token=self.prepared()
        with patch.object(integrations,'telegram_run',return_value={'peer_id':12,'message_id':34}) as transport:
            integrations.send_confirmed(token)
            with self.assertRaises(ValueError):integrations.send_confirmed(token)
            transport.assert_called_once()
        self.assertEqual(core.job(jid)['stage'],'sent')
    def test_uncertain_send_blocks_retry(self):
        jid,token=self.prepared()
        with patch.object(integrations,'telegram_run',side_effect=TimeoutError()):
            with self.assertRaisesRegex(ValueError,'могло уйти'):integrations.send_confirmed(token)
        with patch.object(integrations,'connected',return_value=True):
            with self.assertRaisesRegex(ValueError,'заблокирована'):integrations.prepare(jid)
    def test_http_security_and_crud(self):
        httpd=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port)
        try:
            conn.request('GET','/api/state');res=conn.getresponse();state=json.loads(res.read());self.assertEqual(res.status,200)
            conn.request('POST','/api/jobs',json.dumps({'title':'QA'}),{'Content-Type':'application/json'});res=conn.getresponse();res.read();self.assertEqual(res.status,403)
            headers={'Content-Type':'application/json','X-JobDesk-Token':state['token']}
            conn.request('POST','/api/jobs',json.dumps({'title':'QA','description':'Русское описание вакансии'}),headers);res=conn.getresponse();r=json.loads(res.read());self.assertEqual(res.status,200)
            conn.request('GET','/api/jobs/'+str(r['id']));res=conn.getresponse();j=json.loads(res.read());self.assertEqual(j['title'],'QA')
            conn.request('GET','/api/state',headers={'Origin':'https://evil.example'});res=conn.getresponse();res.read();self.assertEqual(res.status,403)
            conn.request('GET','/api/state',headers={'Host':'evil.example'});res=conn.getresponse();res.read();self.assertEqual(res.status,403)
            conn.request('POST','/api/events',json.dumps({'title':'Интервью','start':'2026-11-05T12:00:00+04:00','job_id':r['id']}),headers);res=conn.getresponse();res.read();self.assertEqual(res.status,200)
            from pypdf import PdfWriter
            writer=PdfWriter();writer.add_blank_page(width=200,height=200);buf=io.BytesIO();writer.write(buf)
            conn.request('POST','/api/profile/pdf',json.dumps({'content':base64.b64encode(buf.getvalue()).decode()}),headers);res=conn.getresponse();pdfresult=json.loads(res.read());self.assertEqual(res.status,200);self.assertTrue(pdfresult['pdf'].endswith('.pdf'))
            conn.request('GET','/api/cv.pdf');res=conn.getresponse();self.assertTrue(res.read().startswith(b'%PDF-'));self.assertEqual(res.status,200)
            conn.request('POST','/api/profile',json.dumps({'body':'Мой опыт'}),headers);res=conn.getresponse();res.read();self.assertEqual(res.status,200)
            conn.request('GET','/api/state');res=conn.getresponse();s=json.loads(res.read());self.assertEqual(s['profile']['body'],'Мой опыт');self.assertEqual(len(s['events']),1)
        finally:conn.close();httpd.shutdown();httpd.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
