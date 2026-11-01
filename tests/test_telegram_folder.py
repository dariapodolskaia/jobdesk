import asyncio, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import core, integrations
from scripts.import_telegram_folder import read_folder, save_sources
from telethon.tl.types import Channel, ChatPhotoEmpty, InputPeerChannel, User


class TelegramFolderTests(unittest.TestCase):
    def test_private_channel_sources_do_not_allow_private_recipients(self):
        self.assertEqual(integrations.channel_target('https://t.me/c/123456'),-1000000123456)
        self.assertEqual(integrations.channel_url('@qa_jobs'),'https://t.me/qa_jobs')
        for value in ('https://t.me/c/123456/42','https://evil.example/c/123456','https://t.me/+invite'):
            with self.assertRaises(ValueError):integrations.channel_url(value)
        with self.assertRaises(ValueError):integrations.username('https://t.me/c/123456')

    def test_reimport_preserves_disabled_existing_source(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(core,'DATA',Path(folder)):
            core.init()
            rows=[{'name':'Public','url':'https://t.me/qa_jobs','private':False},{'name':'Private','url':'https://t.me/c/123456','private':True}]
            self.assertEqual(save_sources(rows),(2,0))
            with core.db() as c:c.execute("UPDATE sources SET enabled=0 WHERE url='https://t.me/qa_jobs'")
            self.assertEqual(save_sources(rows),(0,2))
            with core.db() as c:self.assertEqual(c.execute("SELECT enabled FROM sources WHERE url='https://t.me/qa_jobs'").fetchone()[0],0)

    def test_folder_exclusions_and_pins_and_personal_chats(self):
        peers=[InputPeerChannel(i,10) for i in range(1,5)]
        folder=SimpleNamespace(title=SimpleNamespace(text='Вакансии'),pinned_peers=[peers[0]],include_peers=peers,exclude_peers=[peers[3]],broadcasts=False,groups=False)
        class Client:
            async def __call__(self,request):return SimpleNamespace(filters=[folder])
            async def get_entity(self,selected):
                self.selected=selected
                return [Channel(1,'Public',ChatPhotoEmpty(),None,username='qa_jobs'),Channel(2,'Private',ChatPhotoEmpty(),None),User(3)]
        client=Client();rows,skipped=asyncio.run(read_folder(client,'вакансии'))
        self.assertEqual([p.channel_id for p in client.selected],[1,2,3])
        self.assertEqual(len(rows),2);self.assertEqual(skipped,1);self.assertEqual(rows[1]['url'],'https://t.me/c/2')
