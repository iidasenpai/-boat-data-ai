import json
from itertools import permutations
from datetime import datetime,timedelta
from .store import instant,JST
from .ingest import entries,normalized_result,normalized_preview,normalized_odds

VENUES = ['桐生','戸田','江戸川','平和島','多摩川','浜名湖','蒲郡','常滑','津','三国','びわこ','住之江','尼崎','鳴門','丸亀','児島','宮島','徳山','下関','若松','芦屋','福岡','唐津','大村']

def monitor(store, day, as_of):
    as_of = instant(as_of)
    current = datetime.fromisoformat(as_of)
    issues = []
    races = []
    for race in store.db.execute('SELECT * FROM races WHERE race_date=? ORDER BY venue,race_number',(day,)):
        rid = race['race_id']
        p = store.payload(rid,'program',as_of,'openapi') or {}
        title = store.payload(rid,'title',as_of,'csv') or {}
        deadline = p.get('closed_at')
        canceled = bool(title.get('中止状態')) or p.get('is_canceled',False)
        due = datetime.fromisoformat(instant(deadline)) if deadline else None
        ent = entries(store,rid,as_of)
        prev = normalized_preview(store,rid,as_of)
        result = normalized_result(store,rid,as_of)
        odds = normalized_odds(store,rid,as_of)
        absent = {i for i,e in (result or {}).get('racers',{}).items() if e.get('status') in ('欠','欠場')}
        eligible = set(ent)-absent
        combinations = {'-'.join(map(str,c)) for c in permutations(sorted(eligible),3)}
        found = []
        def flag(code):
            found.append(code);issues.append({'race_id':rid,'code':code})
        if not canceled:
            if len(ent)!=6:
                flag('ENTRY_COUNT_REVIEW')
            if not due:
                flag('DEADLINE_UNKNOWN')
            if due and current>=due-timedelta(minutes=3):
                if not all(prev.get(i,{}).get('time') is not None and (prev.get(i,{}).get('st') is not None or str(prev.get(i,{}).get('start_timing_source') or '').startswith('L')) for i in eligible):
                    flag('PREVIEW_INCOMPLETE')
                if not odds or not combinations.issubset(odds['values']) or any(odds['values'][k] is None for k in combinations):
                    flag('ODDS_INCOMPLETE')
                if odds and odds['source_at'] and datetime.fromisoformat(odds['source_at'])>=due:
                    flag('ODDS_AFTER_DEADLINE')
            if due and current>=due+timedelta(minutes=45):
                if not result:
                    flag('RESULT_DELAY_OR_MISSING')
                if result and not store.payload(rid,'payout',as_of):
                    flag('PAYOUT_MISSING')
            api = store.payload(rid,'result',as_of,'openapi')
            csvrow = store.payload(rid,'result',as_of,'csv')
            if api and csvrow:
                for pos in range(1,4):
                    api_lanes = {int(k) for k,v in api.get('racers',{}).items() if v.get('place_number')==pos}
                    csv_lane = csvrow.get(f'{pos}着_艇番')
                    if csv_lane and api_lanes and int(csv_lane) not in api_lanes:
                        flag('RESULT_SOURCE_CONFLICT');break
        races.append({'race_id':rid,'venue':VENUES[race['venue']-1],'number':race['race_number'],
            'title':p.get('subtitle') or title.get('レース名'),'deadline':deadline,'entries':len(ent),
            'preview':sum(prev.get(i,{}).get('time') is not None for i in ent),
            'odds':len(odds['values']) if odds else 0,'result':bool(result),'canceled':canceled,'absent_lanes':sorted(absent),'eligible_combinations':len(combinations),'issues':found})
    fetches = []
    for row in store.db.execute('SELECT * FROM fetches WHERE observed_at<=? ORDER BY id DESC',(as_of,)):
        if day.replace('-','') not in row['url'] and day.replace('-','/') not in row['url']:
            continue
        if row['source'] in {r['source'] for r in fetches}:
            continue
        fetches.append({k:row[k] for k in ('source','status','observed_at','error','normalized')})
        if row['source']=='openapi' and (row['error'] or (day==current.astimezone(JST).date().isoformat() and current-datetime.fromisoformat(row['observed_at'])>timedelta(minutes=10))):
            issues.append({'race_id':None,'code':'OPENAPI_FETCH_FAILED_OR_STALE'})
    if not any(f['source']=='openapi' for f in fetches):
        issues.append({'race_id':None,'code':'OPENAPI_NOT_FETCHED'})
    if not races:
        issues.append({'race_id':None,'code':'SCHEDULE_UNKNOWN'})
    active = {r['race_id'][8:10] for r in races}
    return {'day':day,'as_of':as_of,'race_count':len(races),'active_venues':len(active),
        'venues':[{'code':f'{v:02}','name':VENUES[v-1],'status':'出走表あり' if f'{v:02}' in active else '開催未確認（非開催とは断定しない）'} for v in range(1,25)],
        'issue_count':len(issues),'issues':issues,'races':races,'fetches':fetches,
        'optional_fields':'オリジナル展示・得点率は配信対象外があるため必須欠損とは分離',
        'official_audit':'選択したレースの公式HTML保存。自動照合パーサーは未実装'}
