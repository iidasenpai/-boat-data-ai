import csv,io,json,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
from boatdata.store import Store,st,instant
from boatdata.ingest import ingest_api,ingest_csv,entries,normalized_result,normalized_odds
from boatdata.profiles import build_profiles
from boatdata.monitor import monitor
from boatdata.__main__ import backup
RID='202609292401';BEFORE='2026-09-29T18:00:00+09:00';AFTER='2026-09-29T20:00:00+09:00'
def sample():
 racers={str(i):{'entry_number':i,'number':4000+i,'name':'選手'+str(i),'motor_number':20+i,'flying_count':0} for i in range(1,7)}
 r={'date':'2026-09-29','stadium_number':24,'race_number':1,'closed_at':'2026-09-29 19:00:00','racers':racers}
 r['preview']={'racers':{str(i):{'start_timing_source':'F.02' if i==1 else '.10','course_number':i,'exhibition_time':6.8} for i in range(1,7)}}
 r['result']={'racers':{str(i):{'number':4000+i,'course_number':7-i,'place_number':i,'place_number_source':str(i),'start_timing_source':'.12'} for i in range(1,7)},'technique_number_source':'逃げ','wind_speed':2,'wave_height':1}
 return {'programs':{'stadiums':{'24':{'races':{'1':r}}}}}
def csvbytes(rows):
 s=io.StringIO();w=csv.DictWriter(s,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows);return s.getvalue().encode()
class FoundationTest(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def load(self,when=AFTER):
  with self.s.db:ingest_api(self.s,sample(),when)
 def test_st(self):self.assertEqual(st('F.02'),-.02);self.assertIsNone(st('L.99'));self.assertEqual(st('0.00'),0)
 def test_jst(self):self.assertEqual(instant('2026-09-29 19:00:00'),'2026-09-29T10:00:00+00:00')
 def test_dedup_and_correction(self):
  self.load();self.load();self.assertEqual(self.s.db.execute("select count(*) from snapshots where kind='result'").fetchone()[0],1)
  x=sample();x['programs']['stadiums']['24']['races']['1']['result']['racers']['1']['place_number']=2
  with self.s.db:ingest_api(self.s,x,'2026-09-29T21:00:00+09:00')
  self.assertEqual(self.s.db.execute("select count(*) from snapshots where kind='result'").fetchone()[0],2)
  self.assertEqual(normalized_result(self.s,RID,AFTER)['racers'][1]['place'],1)
 def test_late_download_excluded(self):
  self.load();self.assertIsNone(normalized_result(self.s,RID,BEFORE));self.assertEqual(build_profiles(self.s,BEFORE)['race_count'],0)
 def test_csv_old_timestamp_not_proof_of_availability(self):
  with self.s.db:ingest_csv(self.s,'od3',csvbytes([{'レースコード':RID,'取得日時':BEFORE,'3連単_1-2-3':'10.0'}]),AFTER)
  self.assertIsNone(normalized_odds(self.s,RID,BEFORE));self.assertEqual(normalized_odds(self.s,RID,AFTER)['values']['1-2-3'],10)
 def test_future_source_time_excluded(self):
  with self.s.db:self.s.put(RID,'odds','csv',{'3連単_1-2-3':'3'},BEFORE,AFTER)
  self.assertIsNone(normalized_odds(self.s,RID,BEFORE))
 def test_zero_odds(self):
  with self.s.db:ingest_csv(self.s,'od3',csvbytes([{'レースコード':RID,'3連単_1-2-3':'0.0'}]),AFTER)
  self.assertEqual(normalized_odds(self.s,RID,AFTER)['values']['1-2-3'],0)
 def test_actual_course_not_lane(self):
  self.load();p=build_profiles(self.s,AFTER);self.assertEqual(p['players']['4001']['courses']['6']['wins'],1);self.assertEqual(p['players']['4001']['courses']['1']['n'],0)
 def test_small_sample_smoothing(self):
  self.load();p=build_profiles(self.s,AFTER)['players']['4001']['all'];self.assertEqual(p['win_rate'],1);self.assertLess(p['smoothed_win_rate'],1)
 def test_csv_result_mapping(self):
  row={'レースコード':RID,'1着_艇番':'4','1コース_艇番':'2','1コース_スタートタイミング':'0.14','2コース_艇番':'4','2コース_スタートタイミング':'-0.01','2コース_F':'F'}
  with self.s.db:ingest_csv(self.s,'realtime',csvbytes([row]),AFTER)
  r=normalized_result(self.s,RID,AFTER)['racers'][4];self.assertEqual(r['course'],2);self.assertEqual(r['place'],1);self.assertEqual(r['st'],-.01)
 def test_all_fourteen_slots_preserved(self):
  row={'レースコード':RID,'艇1_登録番号':'4001','艇1_節D7走2_進入':'6','艇1_節D7走2_ST':'0.07','艇1_節D7走2_着順':'1'}
  with self.s.db:ingest_csv(self.s,'race_cards',csvbytes([row]),AFTER)
  self.assertEqual(entries(self.s,RID,AFTER)[1]['card']['節D7走2_進入'],'6')
 def test_invalid_ingest_rolls_back(self):
  with self.assertRaises(ValueError):
   with self.s.db:ingest_csv(self.s,'title',csvbytes([{'レースコード':RID},{'レースコード':'bad'}]),AFTER)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM races').fetchone()[0],0)
 def test_raw_dedup_fetch_logs(self):
  for _ in range(2):self.s.record_fetch('test','https://example.org',AFTER,200,b'abc')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM raw_objects').fetchone()[0],1);self.assertEqual(self.s.db.execute('SELECT count(*) FROM fetches').fetchone()[0],2)
 def test_immutable(self):
  self.load();self.s.db.execute('INSERT INTO predictions VALUES(?,?,?,?,?)',('p',RID,instant(BEFORE),'v0','{}'));self.s.db.commit()
  with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute("UPDATE predictions SET model_version='v1'")
  with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute('DELETE FROM snapshots')
 def test_source_conflict(self):
  self.load()
  with self.s.db:ingest_csv(self.s,'realtime',csvbytes([{'レースコード':RID,'1着_艇番':'2'}]),AFTER)
  self.assertIn('RESULT_SOURCE_CONFLICT',[x['code'] for x in monitor(self.s,'2026-09-29',AFTER)['issues']])
 def test_unknown_schedule_not_non_racing(self):
  m=monitor(self.s,'2026-09-29',AFTER);self.assertIn('未確認',m['venues'][0]['status']);self.assertIn('SCHEDULE_UNKNOWN',[x['code'] for x in m['issues']])
 def test_backup_raw_and_db(self):
  self.load();self.s.record_fetch('test','test',AFTER,200,b'abc');dest=Path(self.tmp.name)/'backup.zip';backup(self.s,dest)
  with zipfile.ZipFile(dest) as z:
   self.assertIn('boatrace.sqlite',z.namelist());self.assertTrue(any(x.startswith('raw/') for x in z.namelist()));z.extract('boatrace.sqlite',Path(self.tmp.name)/'restore')
  restored=sqlite3.connect(Path(self.tmp.name)/'restore'/'boatrace.sqlite');self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(restored.execute('SELECT count(*) FROM races').fetchone()[0],1);restored.close()
 def test_motor_epoch(self):
  self.load();rows=[{'記録日':'2026-09-29','モーター期起算日':'2026-08-01','場コード':'24','モーター番号':'21'},{'記録日':'2026-09-30','モーター期起算日':'2026-09-30','場コード':'24','モーター番号':'21'}]
  with self.s.db:ingest_csv(self.s,'motor_stats',csvbytes(rows),AFTER)
  p=build_profiles(self.s,AFTER);self.assertIn('24:21:2026-08-01',p['motors']);self.assertNotIn('24:21:2026-09-30',p['motors'])
 def test_f_denominator(self):
  x=sample();r=x['programs']['stadiums']['24']['races']['1']['result']['racers']['1'];r['place_number']=None;r['place_number_source']='F';r['start_timing_source']='F.02'
  with self.s.db:ingest_api(self.s,x,AFTER)
  p=build_profiles(self.s,AFTER)['players']['4001']['all'];self.assertEqual(p['n'],1);self.assertEqual(p['win_rate'],0);self.assertEqual(p['race_f_n'],1)
 def test_confirmed_absence_not_missing_or_start(self):
  from itertools import permutations
  x=sample();r=x['programs']['stadiums']['24']['races']['1'];r['result']['racers']['5'].update(place_number=16,place_number_source='欠',course_number=None,start_timing_source=None);r['preview']['racers']['5']={}
  with self.s.db:
   ingest_api(self.s,x,AFTER)
   values={'レースコード':RID}
   values.update({'3連単_'+'-'.join(map(str,c)):'' if 5 in c else '10' for c in permutations(range(1,7),3)})
   ingest_csv(self.s,'od3',csvbytes([values]),AFTER)
  m=monitor(self.s,'2026-09-29',AFTER);codes=[i['code'] for i in m['issues']]
  self.assertNotIn('PREVIEW_INCOMPLETE',codes);self.assertNotIn('ODDS_INCOMPLETE',codes)
  p=build_profiles(self.s,AFTER);self.assertNotIn('4005',p['players'])
 def test_pending_result_placeholder_not_completed(self):
  x=sample();r=x['programs']['stadiums']['24']['races']['1']['result']
  r['payouts']={'trifecta':[]}
  for e in r['racers'].values():e.update(place_number=None,place_number_source=None,course_number=None,start_timing_source=None)
  with self.s.db:ingest_api(self.s,x,AFTER)
  self.assertIsNone(normalized_result(self.s,RID,AFTER));self.assertEqual(build_profiles(self.s,AFTER)['race_count'],0)
  self.assertFalse(monitor(self.s,'2026-09-29',AFTER)['races'][0]['result'])
 def test_display_l_is_observed_status_not_missing(self):
  x=sample();x['programs']['stadiums']['24']['races']['1']['preview']['racers']['2']['start_timing_source']='L'
  with self.s.db:ingest_api(self.s,x,AFTER)
  codes=[i['code'] for i in monitor(self.s,'2026-09-29',AFTER)['issues']]
  self.assertNotIn('PREVIEW_INCOMPLETE',codes)
if __name__=='__main__':unittest.main()
