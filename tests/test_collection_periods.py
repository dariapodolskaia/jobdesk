import datetime as dt, json, tempfile, unittest, urllib.parse
from pathlib import Path
from unittest.mock import patch
import core, integrations, server


class CollectionPeriodTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patch=patch.object(core,'DATA',Path(self.temp.name));self.patch.start()
        core.init();core.set_settings({'auto_contacts':False})

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def test_all_period_boundaries_and_calendar_month(self):
        ref=dt.datetime(2026,3,31,17,20,tzinfo=dt.timezone(dt.timedelta(hours=4)))
        for period,days in [('day',1),('week',7),('two_weeks',14),('three_weeks',21)]:
            cutoff=core.collection_cutoff(ref,period)
            self.assertEqual(cutoff,ref-dt.timedelta(days=days))
            self.assertEqual(core.publication_status(cutoff,ref,cutoff),'recent')
            self.assertEqual(core.publication_status(cutoff-dt.timedelta(microseconds=1),ref,cutoff),'old')
        self.assertEqual(core.collection_cutoff(ref,'month'),ref.replace(month=2,day=28,hour=0,minute=0))

    def test_sync_filters_by_frozen_selected_window_and_preserves_filters(self):
        before=core.settings();observed=[]
        def collect(source):
            cutoff=core.collection_cutoff();observed.append(cutoff)
            return [{'title':'QA','description':'Удалённая работа из любой страны. Тестирование API и веб-приложений.','published_at':(cutoff+dt.timedelta(seconds=1)).isoformat()}, {'title':'QA old','published_at':(cutoff-dt.timedelta(seconds=1)).isoformat()}]
        with patch.object(core,'collect',side_effect=collect):r=core.sync(1,'week')[0]
        self.assertEqual(r['new'],1);self.assertEqual(r['old'],1);self.assertIn('1 неделя',r['message'])
        self.assertEqual(core.settings()['collection_period'],'week');self.assertIsNone(core.COLLECTION_WINDOW.get())
        for k in ('include','exclude','remote_only','russian_only','exclude_rf_only'):self.assertEqual(core.settings()[k],before[k])

    def test_invalid_period_rejected_before_start(self):
        for invalid in ('year',7,[],False):
            with self.assertRaises(ValueError):server.start_sync(period=invalid)
        self.assertFalse(server.RUN['running']);self.assertFalse(core.SYNC_LOCK.locked())

    def test_hh_uses_selected_period_bounds(self):
        core.set_settings({'include':['qa'],'hh_email':'qa@example.com'})
        ref=dt.datetime.now().astimezone();cutoff=core.collection_cutoff(ref,'two_weeks')
        token=core.COLLECTION_WINDOW.set((ref,cutoff))
        try:
            with patch.object(core,'fetch',return_value=json.dumps({'items':[],'pages':0})) as fetch:core.collect({'kind':'hh','url':'https://hh.ru'})
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args.args[0]).query)
            self.assertEqual(query['date_from'],[cutoff.isoformat(timespec='seconds')]);self.assertEqual(query['date_to'],[ref.isoformat(timespec='seconds')])
        finally:core.COLLECTION_WINDOW.reset(token)

    def test_telegram_stops_at_selected_cutoff(self):
        ref=dt.datetime.now().astimezone();cutoff=core.collection_cutoff(ref,'day')
        from types import SimpleNamespace
        class Client:
            async def iter_messages(self,*args,**kw):
                for i,date in enumerate([ref-dt.timedelta(hours=1),ref-dt.timedelta(days=2)],1):
                    yield SimpleNamespace(id=i,date=date,message='QA: удалённая работа')
        token=core.COLLECTION_WINDOW.set((ref,cutoff))
        try:
            import asyncio
            with patch.object(integrations,'telegram_run',side_effect=lambda fn:asyncio.run(fn(Client()))):items=integrations.telegram_collect('@qa_jobs')
            self.assertEqual(len(items),1)
        finally:core.COLLECTION_WINDOW.reset(token)


if __name__=='__main__':unittest.main()
