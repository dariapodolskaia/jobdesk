#!/usr/bin/env python3
"""Import subscribed channels from a named Telegram folder; never joins or sends."""
import argparse, csv, json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core, integrations


async def read_folder(client,title):
    from telethon import utils
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon.tl.types import Channel
    result=await client(GetDialogFiltersRequest())
    folders=getattr(result,'filters',result)
    matches=[f for f in folders if str(getattr(getattr(f,'title',''),'text',getattr(f,'title',''))).strip().casefold()==title.strip().casefold()]
    if not matches:raise ValueError(f'Папка «{title}» не найдена')
    if len(matches)!=1:raise ValueError('Найдено несколько папок с этим названием. Укажите уникальное название.')
    folder=matches[0]
    if getattr(folder,'broadcasts',False) or getattr(folder,'groups',False):
        raise ValueError('Папка содержит автоматические правила отбора. Сначала добавьте нужные каналы в неё явно: импорт всех каналов аккаунта не выполняется.')
    peers={utils.get_peer_id(p):p for p in folder.pinned_peers+folder.include_peers}
    for peer in getattr(folder,'exclude_peers',[]):peers.pop(utils.get_peer_id(peer),None)
    entities=await client.get_entity(list(peers.values())) if peers else []
    rows=[];skipped=0
    for entity in entities:
        if not isinstance(entity,Channel):skipped+=1;continue
        if getattr(entity,'left',False) or getattr(entity,'deactivated',False):skipped+=1;continue
        url='https://t.me/'+entity.username if entity.username else f'https://t.me/c/{entity.id}'
        rows.append({'name':entity.title,'url':url,'private':not bool(entity.username)})
    return rows,skipped


def save_sources(rows):
    added=existing=0
    with core.db() as c:
        c.execute('BEGIN IMMEDIATE')
        known={integrations.channel_url(r['url']).casefold() for r in c.execute("SELECT url FROM sources WHERE kind='telegram'")}
        for row in rows:
            url=integrations.channel_url(row['url'])
            if url.casefold() in known:existing+=1;continue
            c.execute("INSERT INTO sources(name,kind,url,enabled) VALUES(?,'telegram',?,1)",(row['name'],url))
            known.add(url.casefold());added+=1
    return added,existing


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',nargs='?',default='вакансии');args=parser.parse_args()
    core.init()
    rows,skipped=integrations.telegram_run(lambda client:read_folder(client,args.folder))
    added,existing=save_sources(rows)
    path=core.DATA/'telegram_vacancies_sources.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as output:
        writer=csv.DictWriter(output,fieldnames=['name','url','private']);writer.writeheader();writer.writerows(rows)
    path.chmod(0o600)
    print(json.dumps({'folder':args.folder,'channels':len(rows),'added':added,'existing':existing,'private':sum(r['private'] for r in rows),'other_chats_skipped':skipped,'export':str(path)},ensure_ascii=False))


if __name__=='__main__':main()
