import argparse
import json
import logging
import os
import signal
import time
import zipfile
from datetime import datetime,timedelta
from pathlib import Path
from .store import Store,JST,now,instant,dump
from .collect import collect,official_capture
from .ingest import ingest_api,ingest_csv,CSV_KINDS
from .monitor import monitor
from .profiles import build_profiles
from .dashboard import export_dashboard

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')

def publish(store,day,as_of):
    profile = build_profiles(store,as_of)
    report = monitor(store,day,as_of)
    path = export_dashboard(store,report,profile)
    logging.info('dashboard=%s races=%s issues=%s',path,report['race_count'],report['issue_count'])
    return report

def backup(store,destination):
    dest = Path(destination).resolve()
    dest.parent.mkdir(parents=True,exist_ok=True)
    snapshot = store.root/'backup.sqlite'
    store.backup(snapshot)
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(snapshot,'boatrace.sqlite')
        for p in (store.root/'raw').rglob('*'):
            if p.is_file():
                z.write(p,str(p.relative_to(store.root)))
        z.writestr('restore.txt','解凍先を --data で指定してください。RAWとSQLiteが両方必要です。\n')
    snapshot.unlink()
    return str(dest)

def main():
    parser = argparse.ArgumentParser(description='ボートレース収集・カルテ基盤（予測AI搭載前）')
    parser.add_argument('--data',default='data',help='永続データディレクトリ')
    sub = parser.add_subparsers(dest='command',required=True)
    p = sub.add_parser('collect');p.add_argument('--date',default=datetime.now(JST).date().isoformat())
    p = sub.add_parser('import');p.add_argument('--source',choices=['openapi']+CSV_KINDS,required=True);p.add_argument('--file',required=True)
    p = sub.add_parser('report');p.add_argument('--date',default=datetime.now(JST).date().isoformat());p.add_argument('--as-of',default=now())
    p = sub.add_parser('daemon');p.add_argument('--interval',type=int,default=180)
    p = sub.add_parser('backup');p.add_argument('--output',required=True)
    p = sub.add_parser('official');p.add_argument('--race',required=True)
    p = sub.add_parser('replay');p.add_argument('--to',required=True)
    p = sub.add_parser('serve');p.add_argument('--port',type=int,default=8080);p.add_argument('--bind',default='127.0.0.1')
    args = parser.parse_args()
    store = Store(args.data)
    if args.command=='collect':
        print(json.dumps(collect(store,args.date),ensure_ascii=False,indent=2))
        publish(store,args.date,now())
    elif args.command=='import':
        body = Path(args.file).read_bytes();observed=now()
        fid = store.record_fetch(args.source,'local:'+str(Path(args.file).resolve()),observed,200,body)
        with store.db:
            count = ingest_api(store,json.loads(body),observed,fid) if args.source=='openapi' else ingest_csv(store,args.source,body,observed,fid)
            store.db.execute('UPDATE fetches SET normalized=1 WHERE id=?',(fid,))
        print(count)
    elif args.command=='report':
        print(json.dumps(publish(store,args.date,args.as_of),ensure_ascii=False,indent=2))
    elif args.command=='official':
        print(json.dumps(official_capture(store,args.race),ensure_ascii=False,indent=2))
    elif args.command=='backup':
        print(backup(store,args.output))
    elif args.command=='replay':
        target = Store(args.to)
        if target.root==store.root or target.db.execute('SELECT count(*) FROM fetches').fetchone()[0]:
            raise ValueError('Replay destination must be a separate empty directory')
        for row in store.db.execute('SELECT * FROM fetches WHERE sha IS NOT NULL AND normalized=1 ORDER BY id'):
            obj = store.db.execute('SELECT path FROM raw_objects WHERE sha=?',(row['sha'],)).fetchone()
            body = (store.root/obj['path']).read_bytes()
            fid = target.record_fetch(row['source'],row['url'],row['observed_at'],row['status'],body)
            with target.db:
                if row['source']=='openapi':ingest_api(target,json.loads(body),row['observed_at'],fid)
                elif row['source'] in CSV_KINDS:ingest_csv(target,row['source'],body,row['observed_at'],fid)
                target.db.execute('UPDATE fetches SET normalized=1 WHERE id=?',(fid,))
        print(str(target.root))
    elif args.command=='serve':
        from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
        from functools import partial
        directory = store.root/'dashboard';directory.mkdir(exist_ok=True)
        server = ThreadingHTTPServer((args.bind,args.port),partial(SimpleHTTPRequestHandler,directory=str(directory)))
        logging.info('http://%s:%s',args.bind,args.port)
        server.serve_forever()
    elif args.command=='daemon':
        if args.interval<180:
            parser.error('interval must be at least 180 seconds')
        # Linux/container singleton lock. Windows users use the one-shot collector.
        import fcntl
        lock = (store.root/'collector.lock').open('w')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('collector already running')
        running = True
        def stop(*unused):
            nonlocal running
            running=False
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        last_full=None;last_backup=None;last_yesterday=None
        while running:
            started=time.monotonic()
            current=datetime.now(JST);day=current.date().isoformat()
            try:
                # One hourly full refresh, preview/results every 3 min during operating hours.
                hour=current.strftime('%Y-%m-%d-%H')
                if 7<=current.hour<=23 or last_full is None:
                    full=hour!=last_full
                    results=collect(store,day,full)
                    logging.info('sources=%s',dump(results))
                    if full:last_full=hour
                if last_yesterday!=hour:
                    yesterday=(current.date()-timedelta(days=1)).isoformat()
                    collect(store,yesterday,True);last_yesterday=hour
                publish(store,day,now())
                # A complete on-disk backup once daily. Off-host copies still require an external destination.
                if last_backup!=day:
                    backup(store,store.root/'backups'/f'{day}.zip');last_backup=day
                (store.root/'heartbeat.json').write_text(dump({'at':now(),'day':day,'status':'running'}),encoding='utf-8')
            except Exception:
                logging.exception('cycle failed; next cycle will retry')
                (store.root/'heartbeat.json').write_text(dump({'at':now(),'day':day,'status':'error'}),encoding='utf-8')
            while running and time.monotonic()-started<args.interval:
                time.sleep(min(1,args.interval-(time.monotonic()-started)))

if __name__=='__main__':
    main()
