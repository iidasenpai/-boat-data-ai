"""Transparent experimental rules; scores are not probabilities."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from itertools import permutations
from .store import instant, number, dump
from .meeting import meeting_runs, adjustment
from .ingest import entries, normalized_preview, normalized_result

VERSION = 'rules-0.2'

def predict(store, rid, as_of, improved=False):
    at = instant(as_of)
    program = store.payload(rid, 'program', at, 'openapi') or {}
    deadline = program.get('closed_at')
    if not deadline or instant(deadline) <= at or normalized_result(store, rid, at):
        return None
    boats = entries(store, rid, at)
    if len(boats) != 6:
        return None
    preview = normalized_preview(store, rid, at)
    points = store.payload(rid, 'tokuten_hayami', at, 'csv') or {}
    rows = []
    runs, meeting = meeting_runs(store,rid,at) if improved else ({},None)
    times = [number(p.get('time')) for p in preview.values() if number(p.get('time')) is not None]
    courses = [number(preview.get(l, {}).get('course')) for l in boats]
    course_known = sorted(c for c in courses if c is not None) == [1,2,3,4,5,6]
    for lane, e in boats.items():
        p = preview.get(lane, {})
        course = int(p['course']) if course_known else lane
        values = {k:number(e.get(k)) for k in ('national_win_rate','local_win_rate','motor_top_2_percent','motor_top_3_percent','average_start_timing')}
        missing = [k for k,v in values.items() if v is None]
        score = [0,22,8,7,9,3,1][course]
        reasons = [f'想定{course}コース' + ('（展示進入）' if course_known else '（枠なり仮定）')]
        for key, weight, neutral, label in [('national_win_rate',3,5,'全国勝率'),('local_win_rate',1,5,'当地勝率'),('motor_top_2_percent',.15,30,'モーター2連率'),('motor_top_3_percent',.08,45,'モーター3連率')]:
            value = values[key]
            score += weight*((value if value is not None else neutral)-neutral)
            reasons.append(f'{label} {value if value is not None else "未取得"}')
        avg = values['average_start_timing']
        if avg is not None and 0 < avg < .5:
            score += max(-3,min(3,(.17-avg)*40))
        exhibition = number(p.get('time'))
        if exhibition is not None and len(times) == 6:
            score += max(-3,min(3,(sum(times)/6-exhibition)*30))
        st = number(p.get('st'))
        # Flying exhibition is displayed, never treated as a fatal flaw.
        if st is not None and st >= 0:
            score += max(-2,min(2,(.15-st)*10))
        point = number(points.get(f'艇{lane}_得点率'))
        if point is not None:
            score += max(-4,min(4,(point-5)*1.2))
        change=adjustment(runs.get(lane,[])) if improved else 0
        score+=change
        if improved:reasons.append(f'今節取得 {len(runs.get(lane,[]))}走 / コース・ST補正 {change:+.3f}')
        rows.append({'meeting_runs':runs.get(lane,[]),'meeting_adjustment':change,'lane':lane,'name':e.get('name'),'course':course,'score':round(score,2),'reasons':reasons,'missing':missing,'exhibition_time':exhibition,'exhibition_st':st,'average_st':avg,'meeting_points':point})
    ranked = sorted(rows,key=lambda x:(-x['score'],x['lane']))
    # Only a provisional shortlist until sufficient recent exhibition data exists.
    # Freshness comes from the last successful observation, not the first time
    # identical payloads were seen (snapshots are deduplicated).
    fetch = store.db.execute("SELECT observed_at FROM fetches WHERE source='openapi' AND status=200 AND normalized=1 AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (at,)).fetchone()
    fresh = bool(fetch and datetime.fromisoformat(at)-datetime.fromisoformat(fetch[0]) <= timedelta(minutes=10))
    complete = course_known and len(times)==6 and all(number(p.get('st')) is not None for p in preview.values()) and not any(r['missing'] for r in rows)
    head = ranked[0]['lane']
    seconds = [r['lane'] for r in ranked[1:3]]
    thirds = [r['lane'] for r in ranked[1:4]]
    outer = max((r for r in rows if r['lane'] in (5,6)),key=lambda r:r['score'])['lane']
    if outer != head and outer not in thirds: thirds.append(outer)
    tickets = [f'{head}-{b}-{c}' for b in seconds for c in thirds if b!=c]
    final_ready = fresh and complete
    notes=['未学習の固定ルール。評価点は的中確率ではありません。','今節は取得済みの走だけ。着順は実コース別の固定基準から補正し、少数走は縮小。' if improved else '今節全走のコース・STは未反映。','整備・周回気配は未対応。','定期更新には遅延があります。直前情報は公式出走表で確認。']
    if improved:notes.append(meeting['note'])
    return {'race_id':rid,'at':at,'deadline':deadline,'version':'rules-meeting-0.1' if improved else VERSION,'meeting':meeting,'status':'展示反映・参考買い目' if final_ready else '展示前・暫定買い目','final_ready':final_ready,'rows':ranked,'tickets':tickets,'count':len(tickets),'total_yen':100*len(tickets),'formation':f'{head} → '+','.join(map(str,seconds))+' → '+','.join(map(str,thirds)) if tickets else None,'notes':notes}

def build_predictions(store, day, as_of):
    result = []
    for row in store.db.execute('SELECT race_id FROM races WHERE race_date=? ORDER BY venue,race_number',(day,)).fetchall():
        baseline = predict(store,row[0],as_of)
        if baseline:
            improved = predict(store,row[0],as_of,improved=True)
            for prediction in (baseline,improved):
                key = hashlib.sha256((row[0]+prediction['version']+dump(prediction)).encode()).hexdigest()
                store.db.execute('INSERT OR IGNORE INTO predictions VALUES(?,?,?,?,?)',(key,row[0],instant(as_of),prediction['version'],dump(prediction)))
            improved['baseline']=baseline
            result.append(improved)
    store.db.commit()
    return result
