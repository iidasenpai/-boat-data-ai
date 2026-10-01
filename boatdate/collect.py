import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from .ingest import ingest_api, ingest_csv, CSV_KINDS
from .store import now

BASE = 'https://boatracecsv.github.io/'

def endpoints(day, full=True):
    d = date.fromisoformat(day)
    ymd = d.strftime('%Y/%m/%d')
    sources = [('openapi',f'https://boatraceopenapi.github.io/api/v1/{d.year}/{d:%Y%m%d}.json')]
    for kind in CSV_KINDS:
        if not full and kind in ('title','race_cards','waku10','recent_national','recent_local','motor_stats','motor_history'):
            continue
        area = 'programs' if kind in ('title','race_cards','waku10','recent_national','recent_local','motor_stats','motor_history') else 'results' if kind in ('realtime','payouts') else 'previews'
        sources.append((kind,BASE+f'data/{area}/{kind}/{ymd}.csv'))
    return sources

def fetch(item):
    kind,url = item
    error = None
    status = None
    for attempt in range(3):
        observed = now()
        try:
            request = urllib.request.Request(url,headers={'User-Agent':'BoatDataFoundation/0.1 (personal research; 3-minute interval)'})
            with urllib.request.urlopen(request,timeout=25) as response:
                body = response.read(25*1024*1024+1)
                if len(body)>25*1024*1024:
                    raise ValueError('response exceeds 25 MiB')
                return kind,url,now(),response.status,body,None
        except urllib.error.HTTPError as e:
            status,error = e.code,str(e)
            if e.code in (403,404):
                break
        except (OSError, ValueError) as e:
            error = str(e)
        if attempt<2:
            time.sleep(1+attempt)
    return kind,url,now(),status,None,error

def collect(store, day, full=True):
    report = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = pool.map(fetch,endpoints(day,full))
        for kind,url,observed,status,body,error in responses:
            fid = store.record_fetch(kind,url,observed,status,body,error)
            count = 0
            if body is not None:
                try:
                    with store.db:
                        count = ingest_api(store,json.loads(body),observed,fid) if kind=='openapi' else ingest_csv(store,kind,body,observed,fid)
                        store.db.execute('UPDATE fetches SET normalized=1 WHERE id=?',(fid,))
                except Exception as e:
                    error = f'normalization: {type(e).__name__}: {e}'
                    store.db.execute('UPDATE fetches SET error=? WHERE id=?',(error,fid));store.db.commit()
            report.append({'source':kind,'status':status,'rows':count,'error':error})
    return report

def official_capture(store, rid):
    from .store import raceid
    rid = raceid(rid)
    report = []
    # Explicit selected-race audit. Raw HTML is evidence, not an undocumented parser.
    for page in ('racelist','beforeinfo','odds3t','raceresult'):
        url = f'https://www.boatrace.jp/owpc/pc/race/{page}?hd={rid[:8]}&jcd={rid[8:10]}&rno={int(rid[10:])}'
        kind,url,observed,status,body,error = fetch(('official_'+page,url))
        fid = store.record_fetch(kind,url,observed,status,body,error)
        report.append({'source':kind,'fetch_id':fid,'status':status,'error':error})
    return report
