"""Empirical profiles only. No causal claims or untrained AI scores."""
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from .store import instant, dump, number, JST
from .ingest import entries, normalized_result, normalized_preview

VERSION = 'profiles-1.0'

def summary(rows, prior=None, strength=20):
    n = len(rows)
    starts = [r['st'] for r in rows if r['st'] is not None and r['st'] >= 0]
    deltas = [r['st']-r['preview_st'] for r in rows if r['st'] is not None and r['preview_st'] is not None and r['st']>=0]
    wins = sum(r['place']==1 for r in rows)
    p2 = sum(r['place'] in (1,2) for r in rows)
    p3 = sum(r['place'] in (1,2,3) for r in rows)
    # Failures/F remain in the denominator. Missing place isn't interpreted as 6th.
    out = {'n':n,'wins':wins,'top2':p2,'top3':p3,'win_rate':wins/n if n else None,
           'top2_rate':p2/n if n else None,'top3_rate':p3/n if n else None,
           'mean_st':statistics.mean(starts) if starts else None,
           'std_st':statistics.pstdev(starts) if len(starts)>1 else None,
           'st_n':len(starts),'exhibition_to_race_st_delta':statistics.mean(deltas) if deltas else None,
           'delta_n':len(deltas),'display_f_n':sum(r['preview_st'] is not None and r['preview_st']<0 for r in rows),
           'race_f_n':sum(r['st'] is not None and r['st']<0 for r in rows),
           'unclassified_finish_n':sum(r['place'] is None for r in rows),
           'winning_techniques':dict(Counter(r['technique'] for r in rows if r['place']==1 and r['technique']))}
    if prior and prior['n']:
        for metric, count in [('win_rate',wins),('top2_rate',p2),('top3_rate',p3)]:
            out['smoothed_'+metric] = (count+strength*prior[metric])/(n+strength)
        out['prior_n'] = prior['n']
        out['prior_strength'] = strength
    return out

def group(rows, field, prior=None):
    groups = defaultdict(list)
    for row in rows:
        groups[str(row[field])].append(row)
    return {k:summary(v,prior) for k,v in sorted(groups.items())}

def motor_epochs(store, as_of):
    out = {}
    rows = store.db.execute("SELECT * FROM auxiliary WHERE kind='motor_stats' AND first_seen_at<=? ORDER BY first_seen_at,id",(as_of,))
    for row in rows:
        p = json.loads(row['payload'])
        out[(int(p['場コード']),int(p['モーター番号']))] = p.get('モーター期起算日')
    return out

def observations(store, as_of):
    as_of = instant(as_of)
    rows = []
    motor_records = defaultdict(list)
    for aux in store.db.execute("SELECT payload FROM auxiliary WHERE kind='motor_stats' AND first_seen_at<=? ORDER BY first_seen_at DESC,id DESC",(as_of,)):
        p = json.loads(aux['payload'])
        motor_records[(int(p['場コード']),int(p['モーター番号']))].append(p)
    for race in store.db.execute('SELECT * FROM races ORDER BY race_date,race_number'):
        rid = race['race_id']
        result = normalized_result(store,rid,as_of)
        if not result:
            continue
        ent = entries(store,rid,as_of)
        prev = normalized_preview(store,rid,as_of)
        program = store.payload(rid,'program',as_of,'openapi') or {}
        for lane, e in result['racers'].items():
            if e.get('status') in ('欠','欠場'):
                continue
            entry = ent.get(lane,{})
            player = e.get('number') or entry.get('number')
            motor = entry.get('motor_number') or number(entry.get('card',{}).get('モーター番号'))
            epoch = None
            # Match the motor epoch valid for this race date; never carry a later reset backwards.
            for p in motor_records.get((race['venue'], motor), []):
                if p.get('モーター期起算日','9999')<=race['race_date'] and p.get('記録日','')<=race['race_date']:
                    epoch = p['モーター期起算日']; break
            wind = result.get('wind')
            rows.append({'race_id':rid,'date':race['race_date'],'venue':race['venue'],'race_number':race['race_number'],
                         'lane':lane,'player':int(player) if player else None,'name':entry.get('name') or e.get('name'),
                         'motor':int(motor) if motor else None,'motor_epoch':epoch,
                         'course':e.get('course'),'place':e.get('place'),'st':e.get('st'),
                         'preview_st':prev.get(lane,{}).get('st'),
                         'technique':result.get('technique'),'wind':wind,'direction':result.get('direction'),
                         'wind_bin':'unknown' if wind is None else '0-2m' if wind<=2 else '3-4m' if wind<=4 else '5m+',
                         'wave_bin':'unknown' if result.get('wave') is None else '0-2cm' if result['wave']<=2 else '3cm+',
                         'season':(int(race['race_date'][5:7])%12)//3,
                         'f_count':entry.get('flying_count',number(entry.get('card',{}).get('F本数'))),
                         'grade':program.get('grade_number_source')})
    return rows

def build_profiles(store, as_of):
    as_of = instant(as_of)
    rows = observations(store,as_of)
    day = datetime.fromisoformat(as_of).astimezone(JST).date()
    global_prior = summary(rows)
    courses = group(rows,'course')
    out = {'as_of':as_of,'version':VERSION,'race_count':len({r['race_id'] for r in rows}),
           'global':global_prior,'global_courses':courses,'players':{},'venues':{},'motors':{}}
    for pid in sorted({r['player'] for r in rows if r['player']}):
        selected = [r for r in rows if r['player']==pid]
        selected.sort(key=lambda r:(r['date'],r['race_number']),reverse=True)
        base = summary(selected,global_prior)
        c = {str(course):summary([r for r in selected if r['course']==course],courses.get(str(course))) for course in range(1,7)}
        windows = {f'last_{n}':summary(selected[:n],global_prior) for n in (10,30)}
        windows.update({f'{n}d':summary([r for r in selected if r['date'] >= (day-timedelta(days=n-1)).isoformat()],global_prior) for n in (180,365)})
        # Venue+course shrinks toward the player's all-venue course statistics.
        vc = {}
        for r in selected:
            key = f"{r['venue']:02}:{r['course']}"
            if key not in vc:
                vc[key] = summary([x for x in selected if x['venue']==r['venue'] and x['course']==r['course']],c[str(r['course'])] if str(r['course']) in c else base)
        out['players'][str(pid)] = {'name':selected[0]['name'],'all':base,'windows':windows,'courses':c,
            'venues':group(selected,'venue',base),'venue_courses':vc,'wind':group(selected,'wind_bin',base),
            'f_count':group(selected,'f_count',base),'recent_runs':selected[:30]}
    for venue in range(1,25):
        selected = [r for r in rows if r['venue']==venue]
        periods = {}
        for n in (30,90,365):
            subset = [r for r in selected if r['date'] >= (day-timedelta(days=n-1)).isoformat()]
            base = summary(subset,global_prior)
            periods[f'{n}d'] = {'all':base,'courses':{str(c):summary([r for r in subset if r['course']==c],courses.get(str(c))) for c in range(1,7)},
                'wind_courses':{f'{b}:{c}':summary([r for r in subset if r['wind_bin']==b and r['course']==c],courses.get(str(c))) for b in ('0-2m','3-4m','5m+') for c in range(1,7)},
                'directions':group(subset,'direction',base),'wave':group(subset,'wave_bin',base),
                'race_numbers':group(subset,'race_number',base),'seasons':group(subset,'season',base)}
        daily = [r for r in selected if r['date']==day.isoformat()]
        out['venues'][f'{venue:02}'] = {'periods':periods,'today':{'race_n':len({r['race_id'] for r in daily}),
            'courses':{str(c):summary([r for r in daily if r['course']==c],periods['365d']['courses'][str(c)]) for c in range(1,7)}}}
    for r in rows:
        if not r['motor']:
            continue
        key = f"{r['venue']:02}:{r['motor']}:{r['motor_epoch'] or 'unknown'}"
        if key in out['motors']:
            continue
        subset = [x for x in rows if (x['venue'],x['motor'],x['motor_epoch'])==(r['venue'],r['motor'],r['motor_epoch'])]
        subset.sort(key=lambda x:(x['date'],x['race_number']),reverse=True)
        out['motors'][key] = {'epoch_verified':r['motor_epoch'] is not None,'all':summary(subset,global_prior),
            'last_20':summary(subset[:20],global_prior),'courses':group(subset,'course',global_prior),
            'users':sorted({x['player'] for x in subset if x['player']}),'recent_runs':subset[:20],
            'ability_adjusted_score':None,'note':'選手能力を補正した機力スコアはモデル検証後に追加'}
    store.db.execute('INSERT INTO profile_builds(as_of,version,payload) VALUES(?,?,?)',(as_of,VERSION,dump(out)))
    store.db.commit()
    return out
