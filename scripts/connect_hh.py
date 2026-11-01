#!/usr/bin/env python3
"""Get an HH application token using credentials entered locally."""
import getpass, json, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core, contact_search


def request_token(client_id,client_secret,email):
    if not client_id.strip() or not client_secret.strip():raise ValueError('Client ID и Client Secret обязательны')
    data=urllib.parse.urlencode({'grant_type':'client_credentials','client_id':client_id.strip(),'client_secret':client_secret.strip()}).encode()
    request=urllib.request.Request('https://api.hh.ru/token',data=data,headers={'Content-Type':'application/x-www-form-urlencoded','User-Agent':f'JobDesk/0.3 ({email})'},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=25) as response:result=json.load(response)
    except urllib.error.HTTPError as e:
        e.close()
        raise ValueError(f'HH не выдал токен: HTTP {e.code}. Проверьте реквизиты и статус приложения в dev.hh.ru.') from None
    except (OSError,ValueError):raise ValueError('Не удалось получить токен HH: проверьте интернет, сертификаты и доступ к API.') from None
    token=result.get('access_token') if isinstance(result,dict) else None
    if not isinstance(token,str) or not token:raise ValueError('HH не вернул access_token')
    return token


def main():
    core.init()
    if contact_search.search_settings().get('hh_token'):
        print('Токен HH уже сохранён. Проверка доступа:')
    else:
        email=core.settings().get('hh_email','')
        if not email:
            email=input('Контактный email для HH: ').strip();core.set_settings({'hh_email':email})
            if not email:raise ValueError('Укажите контактный email')
        print('Введите реквизиты зарегистрированного приложения из dev.hh.ru. Client Secret вводится скрыто.')
        token=request_token(input('Client ID: '),getpass.getpass('Client Secret: '),email)
        contact_search.save_search_key(token,'hh_token')
        print('Токен сохранён локально. Client Secret не сохраняется.')
    result=core.diagnose_hh()
    print(result['message'])
    if not result['ok']:sys.exit(1)


if __name__=='__main__':
    try:main()
    except (ValueError,KeyboardInterrupt,EOFError) as e:
        print(str(e) if isinstance(e,ValueError) else 'Подключение отменено.',file=sys.stderr);sys.exit(1)
