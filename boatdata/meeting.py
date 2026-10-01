"""Observed meeting runs, with strict event and point-in-time boundaries."""
from datetime import date,timedelta
from .store import instant,number
from .ingest import entries,normalized_result

def meeting_runs(store,rid,as_of):
    at=instant(as_of);p=store.payload(rid,'program',at,'openapi') or {}
    day=number(p.get('day_number'));title=p.get('title');target=entries(store,rid,at)
    blank={lane:[] for lane in target}
    if not title or day is None or day!=int(day) or not 1<=day<=14:
        return blank,{'verified':False,'note':'開催初日・開催名が未確認のため今節集計なし。'}
    start=date.fromisoformat(p['date'])-timedelta(days=int(day)-1)
    key=(at,rid[8:10],str(start),p['date'],title)
    cache=getattr(store,'_meeting_cache',{})
    if key not in cache:
        values=[];covered=set()
        for row in store.db.execute('SELECT race_id FROM races WHERE venue=? AND race_date BETWEEN ? AND ? ORDER BY race_date,race_number',(int(rid[8:10]),str(start),p['date'])).fetchall():
            other=row[0];q=store.payload(other,'program',at,'openapi') or {}
            d=number(q.get('day_number'))
            if q.get('title')!=title or not d or not q.get('date'):continue
            if date.fromisoformat(q['date'])-timedelta(days=int(d)-1)!=start:continue
            covered.add(q['date'])
            result=normalized_result(store,other,at)
            if not result:continue
            lineup=entries(store,other,at)
            for lane,r in result['racers'].items():
                e=lineup.get(lane)
                if not e:continue
                if str(r.get('status','')).startswith('欠'):continue
                values.append({'race_id':other,'player':e['number'],'day':q['date'],'number':int(other[10:]),'course':r.get('course'),'st':r.get('st'),'place':r.get('place'),'status':r.get('status'),'deadline':q.get('closed_at')})
        cache[key]=(values,covered);store._meeting_cache=cache
    values,covered=cache[key]
    for lane,e in target.items():
        for run in values:
            if run['player']!=e['number'] or run['race_id']==rid:continue
            if run['day']==p['date'] and (not run['deadline'] or instant(run['deadline'])>=instant(p['closed_at'])):continue
            blank[lane].append(dict(run))
    missing=[str(start+timedelta(days=i)) for i in range(int(day)-1) if str(start+timedelta(days=i)) not in covered]
    return blank,{'verified':True,'start':str(start),'title':title,'missing_days':missing,'note':'取得済みの同開催実績。未取得の走は含みません。'+(' 未取得日: '+','.join(missing) if missing else '')}

def adjustment(runs):
    expected={1:2.2,2:3.2,3:3.5,4:3.7,5:4.1,6:4.4}
    valid=[r for r in runs if r.get('course') in expected and r.get('place') in range(1,7)]
    n=len(valid)
    # Shrink short series strongly. Expectations are fixed assumptions, not fit values.
    performance=sum(expected[r['course']]-r['place'] for r in valid)/(n+4)*2 if n else 0
    sts=[r['st'] for r in runs if isinstance(r.get('st'),(int,float)) and 0<=r['st']<.5]
    start=sum(.17-x for x in sts)/(len(sts)+4)*25 if sts else 0
    penalties=sum(str(r.get('status','')).startswith(('F','L')) for r in runs)*.8
    return round(max(-5,min(5,performance+start-penalties)),3)
