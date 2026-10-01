import hashlib,json,shutil,tempfile,unittest,zipfile
from pathlib import Path
from boatdata.store import Store
from boatdata.github_state import restore,persist,prune_checkpoints,extract_safe,STATE_TAG

class FakeGitHub:
 def __init__(self,root):self.root=Path(root);self.uploads=[];self.failed=None;self.nextid=1;self.ids={}
 def release(self,tag):return {'id':tag} if (self.root/tag).exists() else None
 def ensure(self,tag):(self.root/tag).mkdir(exist_ok=True);return self.release(tag)
 def assets(self,release):
  files=[]
  for p in (self.root/release['id']).iterdir():
   key=(release['id'],p.name)
   if key not in self.ids:self.ids[key]=self.nextid;self.nextid+=1
   files.append({'id':self.ids[key],'name':p.name})
  return files
 def upload(self,tag,path):
  self.uploads.append((tag,path.name))
  if self.failed and path.name.startswith(self.failed):raise RuntimeError('simulated upload failure')
  dest=self.root/tag/path.name
  if dest.exists():raise RuntimeError('asset exists')
  shutil.copyfile(path,dest)
 def download(self,tag,name,directory):
  dest=Path(directory)/name;shutil.copyfile(self.root/tag/name,dest);return dest
 def delete_asset(self,id):
  for (tag,name),assetid in self.ids.items():
   if assetid==id:(self.root/tag/name).unlink();return

class GitHubStateTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'remote').mkdir();self.gh=FakeGitHub(self.root/'remote');self.s=Store(self.root/'local')
  self.s.record_fetch('test','test','2026-10-01T00:00:00+00:00',200,b'original RAW')
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def save(self,generation='100-1',previous=None):return persist(self.gh,self.s,generation,previous or {'raw_shards':[]})
 def test_restore_database_and_original_raw(self):
  m=self.save();restored=restore(self.gh,self.root/'restore',with_raw=True)
  self.assertEqual(restored['generation'],'100-1');db=Store(self.root/'restore');row=db.db.execute('SELECT * FROM raw_objects').fetchone();self.assertEqual((db.root/row['path']).read_bytes(),b'original RAW');db.db.close()
 def test_manifest_uploaded_last(self):
  self.save();self.assertEqual(self.gh.uploads[-1],(STATE_TAG,'manifest-100-1.json'))
 def test_interrupted_state_upload_preserves_previous(self):
  prev=self.save();prev['_baseline_shas']=[r[0] for r in self.s.db.execute('SELECT sha FROM raw_objects')];self.gh.failed='state-'
  self.s.record_fetch('test','test','2026-10-01T01:00:00+00:00',200,b'new RAW')
  with self.assertRaises(RuntimeError):self.save('101-1',prev)
  self.assertEqual(restore(self.gh,self.root/'restore')['generation'],'100-1');self.assertEqual(len(prev['raw_shards']),1)
 def test_interrupted_manifest_upload_preserves_previous(self):
  prev=self.save();prev['_baseline_shas']=[r[0] for r in self.s.db.execute('SELECT sha FROM raw_objects')];self.gh.failed='manifest-'
  with self.assertRaises(RuntimeError):self.save('101-1',prev)
  self.assertEqual(restore(self.gh,self.root/'restore')['generation'],'100-1')
 def test_missing_database_does_not_reset(self):
  m=self.save();(self.root/'remote'/STATE_TAG/m['database']['asset']).unlink()
  with self.assertRaises(RuntimeError):restore(self.gh,self.root/'restore')
 def test_checksum_failure_stops_restore(self):
  m=self.save();(self.root/'remote'/STATE_TAG/m['database']['asset']).write_bytes(b'corrupt')
  with self.assertRaises(ValueError):restore(self.gh,self.root/'restore')
 def test_only_new_raw_uploaded(self):
  prev=self.save();prev['_baseline_shas']=[r[0] for r in self.s.db.execute('SELECT sha FROM raw_objects')]
  m=self.save('101-1',prev);self.assertEqual(len(m['raw_shards']),1)
  prev=m;prev['_baseline_shas']=[r[0] for r in self.s.db.execute('SELECT sha FROM raw_objects')];self.s.record_fetch('test','test','2026-10-01T01:00:00+00:00',200,b'new RAW')
  m=self.save('102-1',prev);self.assertEqual(len(m['raw_shards']),2);self.assertEqual(m['raw_object_count'],2)
 def test_pruning_never_deletes_raw(self):
  prev=None
  for i in range(100,105):
   prev=self.save(f'{i}-1',prev);prev['_baseline_shas']=[r[0] for r in self.s.db.execute('SELECT sha FROM raw_objects')]
  prune_checkpoints(self.gh,3);self.assertEqual(len(self.gh.assets(self.gh.release(STATE_TAG))),6)
  self.assertEqual(len(list((self.root/'remote').glob('boat-raw-*/*.zip'))),1)
  self.assertEqual(restore(self.gh,self.root/'restore',with_raw=True)['generation'],'104-1')
 def test_zip_traversal_rejected(self):
  path=self.root/'bad.zip'
  with zipfile.ZipFile(path,'w') as z:z.writestr('../outside','evil')
  with self.assertRaises(ValueError):extract_safe(path,self.root/'restore')
 def test_orphan_initial_upload_requires_inspection(self):
  self.gh.ensure(STATE_TAG);(self.root/'remote'/STATE_TAG/'state-100-1.zip').write_bytes(b'orphan')
  with self.assertRaises(RuntimeError):restore(self.gh,self.root/'restore')

if __name__=='__main__':unittest.main()
