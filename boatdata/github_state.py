"""GitHub Releases persistence: commit manifest last; never discard RAW shards."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from .store import Store, now

STATE_TAG='boat-state'
MAX_ARCHIVE=1800*1024*1024

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def extract_safe(path,directory):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            target=(directory/info.filename).resolve()
            if directory not in target.parents or (info.external_attr>>16)&0o170000==0o120000:
                raise ValueError('unsafe archive member')
        z.extractall(directory)

class GitHub:
    def __init__(self,repo):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('invalid repository')
        self.repo=repo
    def command(self,*args):
        p=subprocess.run(['gh',*args],capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr.strip())
        return p.stdout
    def api(self,path,*args):return json.loads(self.command('api',path,*args))
    def release(self,tag):
        try:return self.api(f'repos/{self.repo}/releases/tags/{tag}')
        except RuntimeError as e:
            if 'HTTP 404' in str(e):return None
            raise
    def assets(self,release):
        pages=self.api(f"repos/{self.repo}/releases/{release['id']}/assets",'--paginate','--slurp')
        return [item for page in pages for item in page]
    def ensure(self,tag):
        release=self.release(tag)
        if release is None:
            self.command('release','create',tag,'--repo',self.repo,'--title',tag,'--notes','自動収集データ。RAWは削除しません。','--latest=false')
            release=self.release(tag)
        return release
    def download(self,tag,name,directory):
        self.command('release','download',tag,'--repo',self.repo,'--pattern',name,'--dir',str(directory),'--clobber')
        return Path(directory)/name
    def upload(self,tag,path):
        if Path(path).stat().st_size>MAX_ARCHIVE:raise ValueError('archive exceeds configured 1800 MiB; split before publishing')
        self.command('release','upload',tag,str(path),'--repo',self.repo)
    def delete_asset(self,id):self.command('api',f'repos/{self.repo}/releases/assets/{id}','--method','DELETE')

def restore(gh,directory,with_raw=False):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    release=gh.release(STATE_TAG)
    if release is None:return {'raw_shards':[],'generation':None}
    assets=gh.assets(release)
    manifests=sorted([a['name'] for a in assets if re.fullmatch(r'manifest-\d+-\d+\.json',a['name'])],
                     key=lambda name:tuple(map(int,re.findall(r'\d+',name))),reverse=True)
    if not manifests:
        # An interrupted initial upload might leave an orphan archive; don't reset silently.
        if assets:raise RuntimeError('state release has files but no committed manifest; inspect orphan upload before starting')
        return {'raw_shards':[],'generation':None}
    with tempfile.TemporaryDirectory() as tmp:
        manifest=json.loads(gh.download(STATE_TAG,manifests[0],tmp).read_text(encoding='utf-8'))
        if sum(s.get('objects',0) for s in manifest.get('raw_shards',[]))!=manifest.get('raw_object_count'):
            raise ValueError('RAW shards inventory mismatch')
        if manifest.get('schema')!=1:raise ValueError('unsupported manifest schema')
        names={a['name'] for a in assets}
        name=manifest['database']['asset']
        if name not in names:raise RuntimeError('committed database asset is missing; refusing a fresh database')
        archive=gh.download(STATE_TAG,name,tmp)
        if sha(archive)!=manifest['database']['sha256']:raise ValueError('database archive checksum mismatch')
        extract_safe(archive,directory)
        db=Store(directory)
        if db.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('restored SQLite integrity check failed')
        if db.db.execute('SELECT count(*) FROM raw_objects').fetchone()[0]!=manifest['raw_object_count']:raise ValueError('RAW inventory does not match manifest')
        db.db.close()
        if with_raw:
            for shard in manifest['raw_shards']:
                path=gh.download(shard['tag'],shard['asset'],tmp)
                if sha(path)!=shard['sha256']:raise ValueError('RAW archive checksum mismatch')
                extract_safe(path,directory)
                path.unlink()
    (directory/'remote-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    return manifest

def persist(gh,store,generation,previous):
    if not re.fullmatch(r'\d+-\d+',generation):raise ValueError('generation must be run_id-run_attempt')
    previous=previous or {'raw_shards':[]}
    baseline=set(previous.get('_baseline_shas',[]))
    manifest=json.loads(json.dumps({k:v for k,v in previous.items() if not k.startswith('_')}))
    manifest.update(schema=1,generation=generation,created_at=now())
    manifest.setdefault('raw_shards',[])
    raw_rows=store.db.execute('SELECT sha,path FROM raw_objects ORDER BY sha').fetchall()
    new=[r for r in raw_rows if r['sha'] not in baseline]
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        if new:
            tag='boat-raw-'+manifest['created_at'][:7]
            release=gh.ensure(tag)
            if len(gh.assets(release))>=990:raise RuntimeError('monthly RAW release nearly full; stop instead of deleting originals')
            raw=tmp/f'raw-{generation}.zip'
            with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as z:
                for row in new:
                    p=store.root/row['path']
                    if not p.is_file() or sha(p)!=row['sha']:raise ValueError('new RAW object missing or corrupt: '+row['sha'])
                    z.write(p,row['path'])
            gh.upload(tag,raw)
            manifest['raw_shards'].append({'tag':tag,'asset':raw.name,'sha256':sha(raw),'objects':len(new)})
        db=tmp/'boatrace.sqlite';store.backup(db)
        archive=tmp/f'state-{generation}.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:z.write(db,'boatrace.sqlite')
        gh.ensure(STATE_TAG);gh.upload(STATE_TAG,archive)
        manifest['database']={'asset':archive.name,'sha256':sha(archive)}
        manifest['raw_object_count']=len(raw_rows)
        if sum(s['objects'] for s in manifest['raw_shards'])!=len(raw_rows):
            raise ValueError('manifest cannot commit: RAW inventory incomplete')
        file=tmp/f'manifest-{generation}.json'
        file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        # Manifest upload is the commit point. Failed prior uploads leave the old state intact.
        gh.upload(STATE_TAG,file)
    return manifest

def prune_checkpoints(gh,keep=3):
    release=gh.release(STATE_TAG)
    if not release:return
    assets=gh.assets(release)
    manifests=sorted([a for a in assets if re.fullmatch(r'manifest-\d+-\d+\.json',a['name'])],
                     key=lambda a:tuple(map(int,re.findall(r'\d+',a['name']))),reverse=True)
    keep_generations={a['name'].removeprefix('manifest-').removesuffix('.json') for a in manifests[:keep]}
    # Only derived DB checkpoints. RAW files in monthly releases are never deleted.
    for asset in assets:
        match=re.fullmatch(r'(?:manifest|state)-(\d+-\d+)\.(?:json|zip)',asset['name'])
        if match and match.group(1) not in keep_generations:gh.delete_asset(asset['id'])
