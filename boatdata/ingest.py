import csv
import io
import json
from .store import raceid, dump, number, st

CSV_KINDS = ['title','race_cards','waku10','recent_national','recent_local','motor_stats','motor_history',
             'tkz','stt','sui','original_exhibition','tokuten_hayami','od3','realtime','payouts']

def ingest_api(store, data, observed, fetch_id=None):
    if not isinstance(data.get('programs', {}).get('stadiums'), dict):
        raise ValueError('OpenAPI schema changed: programs.stadiums missing')
    count = 0
    for venue, stadium in data['programs']['stadiums'].items():
        for rn, race in stadium.get('races', {}).items():
            rid = raceid(race['date'].replace('-', '') + f'{int(venue):02}{int(rn):02}')
            p = {k:v for k,v in race.items() if k not in ('preview','result','racers')}
            p['entries'] = race.get('racers', {})
            store.put(rid,'program','openapi',p,observed,fetch_id=fetch_id)
            if race.get('preview'):
                store.put(rid,'preview','openapi',race['preview'],observed,fetch_id=fetch_id)
            if race.get('result') and any(e.get('place_number_source') for e in race['result'].get('racers',{}).values()):
                result = race['result']
                store.put(rid,'result','openapi',result,observed,fetch_id=fetch_id)
                if any(result.get('payouts',{}).values()) or result.get('refunds'):
                    store.put(rid,'payout','openapi',{'payouts':result['payouts'],'refunds':result.get('refunds',[])},observed,fetch_id=fetch_id)
            count += 1
    return count

def ingest_csv(store, kind, body, observed, fetch_id=None):
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    fields = reader.fieldnames or []
    auxiliary = kind in ('motor_stats','motor_history')
    if not fields or (not auxiliary and 'レースコード' not in fields):
        raise ValueError(f'{kind}: unexpected CSV header')
    count = 0
    for row in reader:
        source_at = row.get('取得日時') or None
        if auxiliary:
            venue = row.get('場コード') or row.get('レース場コード')
            motor = row.get('モーター番号')
            if not venue or not motor:
                raise ValueError('motor CSV missing venue/motor')
            epoch = row.get('モーター期起算日') or 'unknown'
            key = f'{int(venue):02}:{motor}:{epoch}'
            store.aux(kind,'csv',key,row,observed,source_at,fetch_id)
        else:
            rid = raceid(row['レースコード'])
            mapped = {'realtime':'result','payouts':'payout','od3':'odds'}.get(kind,kind)
            store.put(rid,mapped,'csv',row,observed,source_at,fetch_id)
        count += 1
    return count

def entries(store, rid, as_of):
    p = store.payload(rid,'program',as_of,'openapi') or {}
    cards = store.payload(rid,'race_cards',as_of,'csv') or {}
    result = {}
    for lane in range(1,7):
        e = dict(p.get('entries',{}).get(str(lane),{}))
        prefix = f'艇{lane}_'
        e['card'] = {k[len(prefix):]:v for k,v in cards.items() if k.startswith(prefix)}
        e['number'] = e.get('number') or number(e['card'].get('登録番号'))
        e['name'] = e.get('name') or e['card'].get('選手名')
        if e['number']:
            e['number'] = int(e['number'])
            result[lane] = e
    return result

def normalized_result(store, rid, as_of):
    r = store.payload(rid,'result',as_of,'openapi')
    if r and any(e.get('place_number_source') for e in r.get('racers',{}).values()):
        racers = {int(k):dict(v) for k,v in r.get('racers',{}).items()}
        for e in racers.values():
            e['st'] = st(e.get('start_timing_source'))
            e['place'] = e.get('place_number') if e.get('place_number') in range(1,7) else None
            e['status'] = e.get('place_number_source')
            e['course'] = e.get('course_number')
        return {'racers':racers,'technique':r.get('technique_number_source'), 'wind':r.get('wind_speed'),
                'direction':r.get('wind_direction_number_source'), 'wave':r.get('wave_height'),
                'refunds':r.get('refunds',[])}
    r = store.payload(rid,'result',as_of,'csv')
    if not r:
        return None
    racers = {}
    for course in range(1,7):
        lane = number(r.get(f'{course}コース_艇番'))
        if lane in range(1,7):
            racers[int(lane)] = {'course':course,'st':st(r.get(f'{course}コース_スタートタイミング')),
                                 'status':r.get(f'{course}コース_F') or None,'place':None}
    for place in range(1,7):
        lane = number(r.get(f'{place}着_艇番'))
        if lane in range(1,7):
            racers.setdefault(int(lane),{'course':None,'st':None,'status':None})['place'] = place
    return {'racers':racers,'technique':r.get('決まり手'),'wind':number(r.get('風速(m)')),
            'direction':r.get('風向'),'wave':number(r.get('波の高さ(cm)')),'refunds':[]}

def normalized_preview(store, rid, as_of):
    p = store.payload(rid,'preview',as_of,'openapi') or {}
    racers = {int(k):dict(v) for k,v in p.get('racers',{}).items()}
    stt = store.payload(rid,'stt',as_of,'csv') or {}
    tkz = store.payload(rid,'tkz',as_of,'csv') or {}
    for lane in range(1,7):
        e = racers.setdefault(lane,{})
        # Source-specific CSV is preferred only where a value is actually present.
        val = stt.get(f'艇{lane}_スタート展示')
        e['st'] = st(val) if val not in (None,'') else st(e.get('start_timing_source'))
        e['course'] = number(stt.get(f'艇{lane}_コース')) or e.get('course_number')
        e['time'] = number(tkz.get(f'艇{lane}_展示タイム')) or e.get('exhibition_time')
    return racers

def normalized_odds(store, rid, as_of):
    row = store.latest(rid,'odds',as_of,'csv')
    if not row:
        return None
    payload = json.loads(row['payload'])
    values = {k.removeprefix('3連単_'):number(v) for k,v in payload.items() if k.startswith('3連単_')}
    return {'values':values,'source_at':row['source_at'],'first_seen_at':row['first_seen_at']}
