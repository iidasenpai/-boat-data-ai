"""Compare stored pre-deadline predictions with observed results, never reconstruct bets."""
import json
from .store import instant, dump
from .ingest import normalized_result
from .monitor import VENUES

def build_reviews(store, as_of, limit=600):
    records=[]
    races=store.db.execute('SELECT * FROM races ORDER BY race_date DESC,venue,race_number LIMIT ?', (limit,)).fetchall()
    for race in races:
        rid=race['race_id']; result=normalized_result(store,rid,as_of)
        if not result:continue
        order=sorted(((v['place'],k) for k,v in result['racers'].items() if v.get('place') in (1,2,3)))
        combination='-'.join(str(lane) for _,lane in order) if [p for p,_ in order]==[1,2,3] else None
        program=store.payload(rid,'program',as_of,'openapi') or {}
        deadline=program.get('closed_at')
        saved=None
        if deadline:
            saved=store.db.execute('SELECT * FROM predictions WHERE race_id=? AND model_version!="rules-meeting-0.1" AND predicted_at<? AND predicted_at<=? ORDER BY predicted_at DESC,id DESC LIMIT 1',(rid,instant(deadline),instant(as_of))).fetchone()
        prediction=json.loads(saved['payload']) if saved else None
        tickets=prediction.get('tickets',[]) if prediction else []
        hit=combination in tickets if tickets and combination else None
        pay=store.payload(rid,'payout',as_of,'openapi') or {}
        payouts=pay.get('payouts',{}).get('trifecta',[])
        amount=next((v.get('amount') for v in payouts if v.get('combination')==combination),None)
        refund=bool(result.get('refunds') or pay.get('refunds')) or any(str(v.get('status','')).startswith(('F','L','欠')) for v in result['racers'].values())
        cost=100*len(tickets) if tickets else None
        returned=(amount if hit else 0) if hit is not None and amount is not None and not refund else None
        comments=[]
        if not prediction or not tickets: comments.append('締切前の買い目記録がないため、的中判定の対象外。結果から予想を作り直していません。')
        elif combination:
            comments.append('保存した買い目に結果が含まれました。' if hit else '保存した買い目に結果が含まれませんでした。')
            if not hit:
                actual=combination.split('-');parsed=[t.split('-') for t in tickets]
                if not any(t[0]==actual[0] for t in parsed):comments.append('1着候補が外れました。')
                elif not any(t[:2]==actual[:2] for t in parsed):comments.append('1着は候補内ですが、1・2着の組み合わせが抜けました。')
                else:comments.append('1・2着の組み合わせは候補内ですが、3着が抜けました。')
            comments.append('展示反映の参考予想。' if prediction.get('final_ready') else '暫定予想の記録。直前精査は未完了。')
        if refund:comments.append('返還・欠場等あり。参考収支の計算対象外。')
        item={'race_id':rid,'day':race['race_date'],'venue':VENUES[race['venue']-1],'number':race['race_number'],'combination':combination,'payout':amount,'prediction':prediction,'tickets':tickets,'hit':hit,'cost':cost,'returned':returned,'profit':returned-cost if returned is not None else None,'comments':comments,'technique':result.get('technique'),'racers':[dict(lane=k,**v) for k,v in result['racers'].items()]}
        comparison=None
        if saved:
            other=store.db.execute('SELECT * FROM predictions WHERE race_id=? AND model_version=? AND predicted_at=? ORDER BY id DESC LIMIT 1',(rid,'rules-meeting-0.1',saved['predicted_at'])).fetchone()
            if other:
                candidate=json.loads(other['payload']);ct=candidate.get('tickets',[])
                ch=combination in ct if combination and ct else None
                cr=(amount if ch else 0) if ch is not None and amount is not None and not refund else None
                comparison={'prediction':candidate,'hit':ch,'cost':len(ct)*100,'returned':cr,'profit':cr-len(ct)*100 if cr is not None else None}
                snap=store.latest(rid,'result',as_of)
                if snap:store.db.execute('INSERT OR IGNORE INTO reviews(prediction_id,reviewed_at,result_fingerprint,payload) VALUES(?,?,?,?)',(other['id'],instant(as_of),snap['fingerprint'],dump(comparison)))
        item['comparison']=comparison
        records.append(item)
        if saved:
            snap=store.latest(rid,'result',as_of)
            if snap:
                store.db.execute('INSERT OR IGNORE INTO reviews(prediction_id,reviewed_at,result_fingerprint,payload) VALUES(?,?,?,?)',(saved['id'],instant(as_of),snap['fingerprint'],dump(item)))
    store.db.commit()
    return records
