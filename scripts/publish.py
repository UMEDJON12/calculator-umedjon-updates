#!/usr/bin/env python3
"""Publish only an already signed APK after native Android checks have passed."""
import base64,glob,hashlib,json,os,re,subprocess,time,urllib.error,urllib.request
from pathlib import Path

REPO='UMEDJON12/calculator-umedjon-updates'
CERT='53eab873fe40528cef6b084d644451057dd56d9d5bd0338d258c2c850743f0c7'
API='https://api.github.com/repos/'+REPO
ROOT=Path(__file__).resolve().parents[1]

def sdk(name):
    paths=sorted(glob.glob(os.environ.get('ANDROID_HOME','/usr/local/lib/android/sdk')+'/build-tools/*/'+name),reverse=True)
    if not paths:raise RuntimeError('Missing Android SDK tool: '+name)
    return paths[0]

def validate():
    data=json.loads((ROOT/'release-input/update.json').read_text())
    apk=ROOT/'release-input/Calculator-UMEDJON-current.apk'
    assert data['schemaVersion']==1 and data['variant']=='standard' and data['testOnly'] is False
    assert data['packageName']=='tj.umedjon.calculator' and data['signingCertificateSha256']==CERT
    assert isinstance(data['versionCode'],int) and data['versionCode']>0
    assert re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',data['versionName'])
    assert data['apkFileName']=='Calculator-UMEDJON-'+data['versionName']+'.apk'
    assert data['apkUrl']=='https://github.com/'+REPO+'/releases/download/v'+data['versionName']+'/'+data['apkFileName']
    assert data['sizeBytes']==apk.stat().st_size and data['sha256']==hashlib.sha256(apk.read_bytes()).hexdigest()
    signature=subprocess.check_output([sdk('apksigner'),'verify','--print-certs',str(apk)],text=True)
    assert re.findall(r'certificate SHA-256 digest: ([a-fA-F0-9]+)',signature)==[CERT]
    badging=subprocess.check_output([sdk('aapt'),'dump','badging',str(apk)],text=True)
    match=re.search(r"package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'",badging)
    assert match and (match[1],int(match[2]),match[3])==(data['packageName'],data['versionCode'],data['versionName'])
    assert int(re.search(r"sdkVersion:'([0-9]+)'",badging)[1])==data['minSdk']
    return data,apk

def api(path,method='GET',data=None,raw=None,content_type='application/json'):
    body=raw if raw is not None else json.dumps(data).encode() if data is not None else None
    request=urllib.request.Request((path if path.startswith('https://uploads.github.com/') else API+path),method=method,data=body,headers={
        'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':content_type,'User-Agent':'Calculator-UMEDJON-release'})
    with urllib.request.urlopen(request,timeout=90) as response:
        body=response.read();return json.loads(body) if body else None

def attempt(fn,tries=6,delay=5):
    """GitHub needs a moment to expose a just-published release and its assets."""
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i==tries-1:raise
            time.sleep(delay)

def publish():
    data,apk=validate();tag='v'+data['versionName']
    assert os.environ['GITHUB_REPOSITORY']==REPO
    event=os.environ.get('GITHUB_EVENT_NAME','')
    if event=='release':
        # For a release event the tag comes from the payload (already checked while
        # staging); GITHUB_REF of release events is not the tag ref and must not be used.
        payload=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()).get('release',{})
        assert payload.get('tag_name')==tag,'Release event tag does not match the update metadata'
        assert not payload.get('draft',False),'A draft release must never be handled as published'
    else:
        assert os.environ['GITHUB_REF'] in ('refs/heads/main','refs/tags/'+tag),'Run from main or from the release tag being published'
    try:release=api('/releases/tags/'+tag)
    except urllib.error.HTTPError as e:
        if e.code!=404:raise
        # Creating and publishing a release is the maintainer's decision («Publish release»).
        raise AssertionError('No published release for '+tag+'; automation never creates or publishes releases')
    assert not release['prerelease']
    assert not release['draft'],'A draft release must be published by the maintainer, never by automation'
    expected={data['apkFileName']:apk.read_bytes(),'update.json':(ROOT/'release-input/update.json').read_bytes()}
    assets={a['name']:a for a in release['assets']}
    for name,body in expected.items():
        if name in assets:
            assert assets[name]['size']==len(body) and assets[name].get('digest')=='sha256:'+hashlib.sha256(body).hexdigest(),'Existing release asset differs; publish a new version'
            continue
        assert release['draft'],'Never alter an already published release'
        uploaded=api(release['upload_url'].split('{')[0]+'?name='+name,'POST',raw=body,content_type='application/vnd.android.package-archive' if name.endswith('.apk') else 'application/json')
        assert uploaded['size']==len(body) and uploaded.get('digest')=='sha256:'+hashlib.sha256(body).hexdigest()
    if attempt(lambda: api('/releases/latest'))['id']!=release['id']:
        # The release was published by hand; make sure the app channel points at it.
        release=api('/releases/'+str(release['id']),'PATCH',{'make_latest':'true'})
    latest=attempt(lambda: api('/releases/latest'));assert latest['id']==release['id'] and not release['draft']
    # Verify that anonymous users can fetch both public files before advertising them.
    for name,body in expected.items():
        def fetch():
            with urllib.request.urlopen('https://github.com/'+REPO+'/releases/download/'+tag+'/'+name,timeout=90) as response:
                return response.read(len(body)+1)
        assert attempt(fetch)==body,'Public download differs from verified input'
    channel=dict(data,ready=True,tagName=tag,publishedAt=release['published_at'])
    path='/contents/updates/stable.json';request={'message':'Publish verified calculator '+data['versionName']+' channel','branch':'main','content':base64.b64encode((json.dumps(channel,ensure_ascii=False,indent=2)+'\n').encode()).decode()}
    body=json.dumps(channel,ensure_ascii=False,indent=2)+'\n'
    for i in range(5):
        try:
            old=api(path+'?ref=main')
            previous_text=base64.b64decode(old['content']).decode()
            if previous_text==body:
                print(json.dumps({'published':True,'version':data['versionName'],'publicApkVerified':True,'stableChannel':'already current'}))
                return
            previous=json.loads(previous_text)
            assert previous['versionCode']<=data['versionCode'],'Never downgrade the stable channel'
            request['sha']=old['sha']
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            request.pop('sha',None)
        try:
            api(path,'PUT',request)
            break
        except urllib.error.HTTPError as e:
            # 409/422: somebody moved main between read and write (e.g. the workflow commit).
            if i==4 or e.code not in (409,422):raise
            time.sleep(3)
    else:
        raise AssertionError('stable.json was not updated')
    print(json.dumps({'published':True,'version':data['versionName'],'publicApkVerified':True,'release':release['html_url']}))

if __name__=='__main__':
    if '--verify-only' in __import__('sys').argv:
        data,_=validate();print('PASS: release APK, signature and metadata verified:',data['versionName'])
    else:publish()
