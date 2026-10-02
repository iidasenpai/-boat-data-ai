"""One free-tier Actions cycle. No cache, paid runner, or artifact storage."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime,timedelta,date
from pathlib import Path
from .store import Store,JST,now
from .collect import collect
from .profiles import build_profiles
from .monitor import monitor
from .dashboard import export_dashboard
from .github_state import GitHub,restore,persist,prune_checkpoints,extract_safe

MAX_SITE=90*1024*1024

def build_site(store,day,output):
    profile=build_profiles(store,now())
    # This is a reproducible view, not primary observations: keep only the latest 3 builds.
    store.db.execute('DELETE FROM profile_builds WHERE id NOT IN (SELECT id FROM profile_builds ORDER BY id DESC LIMIT 3)')
    store.db.commit()
    status=monitor(store,day,now())
    web_profile=json.loads(json.dumps(profile))
    for kind in ('players','motors'):
        for entry in web_profile[kind].values():
            if 'recent_runs' in entry:entry['recent_runs']=entry['recent_runs'][:5]
    export_dashboard(store,status,web_profile)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(store.root/'dashboard/index.html',output/'index.html')
    shutil.copyfile(store.root/'dashboard/monitor.json',output/'monitor.json')
    (output/'.nojekyll').touch()
    (output/'boat-site.json').write_text(json.dumps({'application':'boatdata','schema':1}),encoding='utf-8')
    (output/'health.json').write_text(json.dumps({'at':now(),'day':day,'races':status['race_count'],
        'issues':status['issue_count'],'completed_profiles':profile['race_count'],
        'mode':'GitHubの定期更新。締切前の反映は保証しません。'},ensure_ascii=False),encoding='utf-8')
    total=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
    if total>MAX_SITE:raise ValueError('site exceeds 90 MiB; reduce display before deploying (DB/RAW still preserved)')
    return status

def deploy_site(gh,output):
    # The caller uses a checkout with GITHUB_TOKEN credentials configured by checkout action.
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        def git(*args):
            result=subprocess.run(['git','-C',str(tmp),*args],capture_output=True,text=True)
            if result.returncode:raise RuntimeError(result.stderr.strip())
            return result.stdout
        remote=subprocess.run(['git','remote','get-url','origin'],check=True,capture_output=True,text=True).stdout.strip()
        git('init','--initial-branch=gh-pages')
        git('remote','add','origin',remote)
        # Reuse Actions checkout's scoped HTTP header without printing its token.
        result=subprocess.run(['git','config','--get-regexp',r'^http\..*\.extraheader$'],capture_output=True,text=True)
        for line in result.stdout.splitlines():
            key,value=line.split(' ',1);git('config',key,value)
        git('config','user.name','github-actions[bot]');git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
        refs=git('ls-remote','--heads','origin','gh-pages')
        if refs:
            git('fetch','--depth=1','origin','gh-pages');git('checkout','-B','gh-pages','FETCH_HEAD')
            marker=tmp/'boat-site.json'
            if not marker.exists() or json.loads(marker.read_text()).get('application')!='boatdata':
                raise RuntimeError('gh-pages contains a different site; refusing to replace it. Use a dedicated repository.')
        # Replace only generated site output; no personal files belong on this managed branch.
        for p in tmp.iterdir():
            if p.name=='.git':continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for p in Path(output).iterdir():
            if p.is_file():shutil.copyfile(p,tmp/p.name)
        git('add','.')
        if git('status','--porcelain').strip():
            git('commit','-m','Update boat data dashboard');git('push','origin','HEAD:gh-pages')
    try:
        config=gh.api(f'repos/{gh.repo}/pages')
    except RuntimeError as e:
        if 'HTTP 404' not in str(e):raise
        print('::warning::画面はgh-pagesに保存済み。Settings > PagesでDeploy from a branch / gh-pages / rootを選んでください。')
        return {'status':'setup_required'}
    if config.get('source',{}).get('branch')!='gh-pages' or config.get('build_type','legacy')!='legacy':
        raise RuntimeError('Pages must use Deploy from a branch, gh-pages / root; existing configuration was not changed')
    # GITHUB_TOKEN pushes do not auto-trigger Pages; explicitly request the supported legacy build API.
    result=gh.api(f'repos/{gh.repo}/pages/builds','--method','POST')
    return {'status':result.get('status','queued'),'url':config.get('html_url')}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',default='runtime-data')
    parser.add_argument('--repo',default=os.environ.get('GH_REPO'))
    parser.add_argument('--generation',default=os.environ.get('BOAT_GENERATION'))
    parser.add_argument('--date',default=datetime.now(JST).date().isoformat())
    parser.add_argument('--catchup',type=int,default=3)
    parser.add_argument('--site',default='site')
    args=parser.parse_args()
    if not args.repo or not args.generation:parser.error('repo and generation are required')
    if not 1<=args.catchup<=7:parser.error('catchup must be 1..7')
    gh=GitHub(args.repo)
    meta=gh.api(f'repos/{gh.repo}')
    if meta.get('private') is not False:raise RuntimeError('Free-only mode requires a PUBLIC repository. Private repository work is disabled.')
    previous=restore(gh,args.data)
    if not previous.get('generation') and Path('bootstrap-data.zip').exists():
        extract_safe('bootstrap-data.zip',args.data)
    store=Store(args.data)
    previous['_baseline_shas']=[r[0] for r in store.db.execute('SELECT sha FROM raw_objects')] if previous.get('generation') else []
    # On the first cycle, collect up to 7 previous days. Subsequently revisit the last 3 days for corrections.
    days=args.catchup if previous.get('generation') else 7
    target=date.fromisoformat(args.date)
    reports=[]
    for offset in range(days,-1,-1):
        day=(target-timedelta(days=offset)).isoformat()
        result=collect(store,day,full=True)
        reports.append({'day':day,'sources':result})
    site_error=None
    try:status=build_site(store,args.date,args.site)
    except Exception as e:site_error=e;status={'issue_count':None}
    # Preserve all successfully fetched data even if an optional source or view generation failed.
    manifest=persist(gh,store,args.generation,previous)
    prune_checkpoints(gh,keep=3)
    Path('cycle-report.json').write_text(json.dumps({'at':now(),'days':reports,'manifest_generation':manifest['generation'],'issues':status['issue_count']},ensure_ascii=False,indent=2),encoding='utf-8')
    if site_error:raise site_error
    print(json.dumps(deploy_site(gh,args.site),ensure_ascii=False))
    critical=[r['day'] for r in reports if any(x['source']=='openapi' and x['error'] for x in r['sources'])]
    if critical:raise RuntimeError('OpenAPI failed for '+','.join(critical)+'; successful data saved, retry on next cycle')

if __name__=='__main__':main()
