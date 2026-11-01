#!/usr/bin/env python3
"""Run with server stopped. Never sends any messages."""
import sys, os, json, getpass, asyncio
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import DATA
from telethon import TelegramClient
async def main():
    DATA.mkdir(parents=True,exist_ok=True); os.chmod(DATA,0o700)
    print('Остановите JobDesk перед подключением. Получите api_id/api_hash на https://my.telegram.org → API development tools.\nКоды и пароль вводятся только здесь. Сообщения отправляться не будут.')
    api_id=int(input('API ID: ').strip()); api_hash=getpass.getpass('API hash (скрыт): ').strip()
    client=TelegramClient(str(DATA/'telegram'),api_id,api_hash)
    try:
        await client.start(phone=lambda:input('Телефон с кодом страны: '),code_callback=lambda:getpass.getpass('Код Telegram (скрыт): '),password=lambda:getpass.getpass('Пароль 2FA (скрыт): '))
        me=await client.get_me()
        path=DATA/'telegram.json'; path.write_text(json.dumps({'api_id':api_id,'api_hash':api_hash})); os.chmod(path,0o600)
        session=DATA/'telegram.session'
        if session.exists(): os.chmod(session,0o600)
        print('Telegram подключён. Можно запускать JobDesk.')
    finally: await client.disconnect()
if __name__=='__main__': asyncio.run(main())
