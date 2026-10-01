import json,subprocess,tempfile,unittest,os
from pathlib import Path
from unittest.mock import patch
from boatdata.store import Store
from boatdata.github_cycle import deploy_site,build_site

class PagesApi:
 repo='example/boatdata'
 def api(self,path,*args):
  if path.endswith('/pages/builds'):return {'status':'queued'}
  return {'source':{'branch':'gh-pages'},'build_type':'legacy','html_url':'https://example.github.io/boatdata/'}

class CycleTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.repo=self.root/'source';self.repo.mkdir()
  self.remote=self.root/'remote.git';subprocess.run(['git','init','--bare',str(self.remote)],check=True,capture_output=True)
  subprocess.run(['git','init',str(self.repo)],check=True,capture_output=True)
  subprocess.run(['git','-C',str(self.repo),'remote','add','origin',str(self.remote)],check=True)
  self.old=Path.cwd();os.chdir(self.repo)
  self.site=self.root/'site';self.site.mkdir();(self.site/'index.html').write_text('first');(self.site/'boat-site.json').write_text('{"application":"boatdata"}')
 def tearDown(self):os.chdir(self.old);self.tmp.cleanup()
 def test_first_deploy_and_update_without_force_push(self):
  x=deploy_site(PagesApi(),self.site);self.assertEqual(x['status'],'queued')
  (self.site/'index.html').write_text('second');deploy_site(PagesApi(),self.site)
  got=subprocess.run(['git','--git-dir',str(self.remote),'show','gh-pages:index.html'],capture_output=True,text=True,check=True).stdout
  self.assertEqual(got,'second')
  count=subprocess.run(['git','--git-dir',str(self.remote),'rev-list','--count','gh-pages'],capture_output=True,text=True,check=True).stdout.strip();self.assertEqual(count,'2')
 def test_existing_unrelated_site_is_preserved(self):
  deploy_site(PagesApi(),self.site)
  # Remove marker in a separate checkout, simulating an unrelated gh-pages branch.
  clone=self.root/'clone';subprocess.run(['git','clone','-b','gh-pages',str(self.remote),str(clone)],check=True,capture_output=True)
  (clone/'boat-site.json').unlink();subprocess.run(['git','-C',str(clone),'config','user.name','test'],check=True);subprocess.run(['git','-C',str(clone),'config','user.email','test@example.org'],check=True)
  subprocess.run(['git','-C',str(clone),'add','.'],check=True);subprocess.run(['git','-C',str(clone),'commit','-m','unrelated site'],check=True,capture_output=True);subprocess.run(['git','-C',str(clone),'push'],check=True,capture_output=True)
  with self.assertRaisesRegex(RuntimeError,'different site'):deploy_site(PagesApi(),self.site)
 def test_pages_not_configured_reports_required_setup(self):
  class Missing(PagesApi):
   def api(self,*args):raise RuntimeError('HTTP 404')
  self.assertEqual(deploy_site(Missing(),self.site)['status'],'setup_required')
 def test_build_site_and_checkpoint_view_retention(self):
  store=Store(self.root/'data');build_site(store,'2026-10-01',self.site)
  self.assertTrue((self.site/'index.html').exists());self.assertEqual(json.loads((self.site/'boat-site.json').read_text())['application'],'boatdata');store.db.close()

if __name__=='__main__':unittest.main()
