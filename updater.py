"""Offline, stdlib-only updater for shared-v1 project payloads. Run with app closed."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import zipfile
import ctypes
from ctypes import wintypes
import tempfile
import urllib.request
import urllib.parse
import re
import io
import sys
import uuid
from contextlib import contextmanager

BUNDLE_CONTRACT='biol-dkb-distribution-v1'
BUNDLE_MANIFEST='distribution-manifest.json'
_progress_callback = None


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def report_progress(message):
    if _progress_callback is not None:
        _progress_callback(message)


def files_baseline(files):
    return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',', ':')).encode()).hexdigest()


def managed_root(install):
    root=Path(install).resolve()
    for candidate in (root,root.parent):
        manifest=candidate/BUNDLE_MANIFEST
        if not manifest.is_file():manifest=candidate/'.update/backup'/BUNDLE_MANIFEST
        if not manifest.is_file():manifest=candidate.with_name(candidate.name+'.update-backup')/BUNDLE_MANIFEST
        if manifest.is_file():
            value=json.loads(manifest.read_text(encoding='utf-8'))
            if value.get('contract')!=BUNDLE_CONTRACT or value.get('project')!='DKB' or value.get('app_dir')!='DKB_Auto':raise ValueError('Invalid bundle identity')
            if root not in (candidate,candidate/'DKB_Auto'):continue
            return candidate
    return root


def manifest_name(root):return BUNDLE_MANIFEST if (root/BUNDLE_MANIFEST).exists() else 'package-manifest.json'


def installed_manifest(install):
    root=managed_root(install)
    return json.loads((root/manifest_name(root)).read_text(encoding='utf-8'))


def workspace(install):
    install=managed_root(install)
    bundle=(install/BUNDLE_MANIFEST).is_file() or (install/'.update/backup'/BUNDLE_MANIFEST).is_file()
    if bundle:
        home=install/'.update'
        if home.is_symlink() or (home.exists() and getattr(home.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked update workspace unsupported')
        home.mkdir(exist_ok=True)
        return home/'backup',home/'stage',home/'lock'
    return tuple(install.with_name(install.name+suffix) for suffix in ('.update-backup','.update-stage','.update-lock'))


def verify_installed(install):
    manifest=installed_manifest(install)
    for name,value in manifest['files'].items():
        if not user_file(name) and digest(safe(install,name))!=value:
            raise ValueError('Installed file SHA256 mismatch: '+name)
    return manifest


def prepare_workspace(install):
    """Retain completed backups internally; never discard interrupted work."""
    install=managed_root(install)
    if not (install/BUNDLE_MANIFEST).is_file():return
    backup,stage,lock=workspace(install)
    legacy=tuple(install.with_name(install.name+s) for s in ('.update-backup','.update-stage','.update-lock'))
    if stage.exists() or lock.exists() or (backup.parent/'transaction.json').exists() or any(p.exists() for p in legacy[1:]):
        raise RuntimeError('Previous update interrupted; inspect before applying')
    retained=[p for p in (backup,legacy[0]) if p.exists()]
    if retained:
        receipt=backup.parent/'accepted.json'
        accepted=False
        try:accepted=json.loads(receipt.read_text(encoding='utf-8'))['manifest_sha256']==digest(install/BUNDLE_MANIFEST)
        except (OSError,ValueError,KeyError):pass
        if not accepted or legacy[0].exists():verify_installed(install)
        history=backup.parent/'history';history.mkdir(exist_ok=True)
        for path in retained:
            old=json.loads((path/BUNDLE_MANIFEST).read_text(encoding='utf-8'))
            if old.get('contract')!=BUNDLE_CONTRACT or old.get('project')!='DKB':raise ValueError('Invalid retained backup')
            os.replace(path,history/('backup-'+uuid.uuid4().hex))
        receipt.unlink(missing_ok=True)
    legacy_log=install.with_name('DKB-update.log')
    if legacy_log.is_file():
        logs=backup.parent/'logs';logs.mkdir(exist_ok=True)
        os.replace(legacy_log,logs/('legacy-'+uuid.uuid4().hex+'.log'))


def accept(install):
    """Restart acknowledged; retain rollback without blocking the next update."""
    install=managed_root(install)
    if not (install/BUNDLE_MANIFEST).is_file():return
    backup,stage,lock=workspace(install)
    (backup.parent/'accepted.json').write_text(json.dumps({'manifest_sha256':digest(install/BUNDLE_MANIFEST)}),encoding='utf-8')


def swap_payload(install,source,backup):
    """Keep the installation root stable; move children on the same volume."""
    journal=backup.parent/'transaction.json'
    state={'old':[], 'new':[]}
    def save():
        temp=journal.with_suffix('.tmp')
        temp.write_text(json.dumps(state),encoding='utf-8');os.replace(temp,journal)
    backup.mkdir()
    try:
        for path in list(install.iterdir()):
            if path.name=='.update':continue
            state['old'].append(path.name);save()
            os.replace(path,backup/path.name)
        for path in list(source.iterdir()):
            state['new'].append(path.name);save()
            os.replace(path,install/path.name)
    except BaseException:
        # Entries recorded before each move also cover interrupted renames.
        for name in reversed(state['new']):
            path=install/name
            if path.exists():os.replace(path,source/name)
        for name in reversed(state['old']):
            path=backup/name
            if path.exists():os.replace(path,install/name)
        backup.rmdir();journal.unlink(missing_ok=True)
        raise
    source.rmdir();journal.unlink()


def user_file(name):
    p = Path(name)
    # Runtime package data (e.g. cv2/data) is a managed dependency, not a
    # user's DKB data directory. Component paths are still checked by safe().
    if p.parts and p.parts[0] in {'BIOL_Runtime','BIOL_Project_Base','DKB_Auto_Updater'}:
        return False
    return p.name.casefold() in {'license.dat', 'user_values.json', 'manager_identity.json'} or any(
        part.casefold() in {'settings', 'logs', 'data', 'backups', 'audit', '__pycache__'} for part in p.parts)


@contextmanager
def app_closed(install):
    """Own the same named object as the existing runtime while swapping files."""
    root = managed_root(install)
    if not root.exists():
        root = root.with_name(root.name + '.update-backup')
    old = json.loads((root / manifest_name(root)).read_text(encoding='utf-8'))
    app=root/old['app_dir'] if old.get('contract')==BUNDLE_CONTRACT else root
    product = json.loads((app / 'runtime/products' / (old['project'] + '.json')).read_text(encoding='utf-8'))
    if old['project'] != 'DKB' or product.get('product_code') != old['project']:
        raise ValueError('This updater only manages its configured DKB project')
    app_id = product.get('app_id', old['project'])
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.set_last_error(0)
    handle = k.CreateMutexW(None, False, 'Local\\BIOL_Project_Runtime_SingleInstance_' + app_id)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            raise RuntimeError('Project is running; close it before updating')
        yield
    finally:
        k.CloseHandle(handle)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''): h.update(block)
    return h.hexdigest()


def safe(root, name):
    p = Path(name)
    root = Path(root).resolve()
    target = (root / p).resolve()
    if p.is_absolute() or ':' in name or (p.parts and p.parts[0].casefold()=='.update') or target == root or not target.is_relative_to(root):
        raise ValueError('Unsafe package path: ' + name)
    return target


def apply(install, archive, expected_sha256):
    install, archive = managed_root(install), Path(archive).resolve()
    prepare_workspace(install)
    backup,stage,lock=workspace(install)
    report_progress('다운로드 파일 검증 중...')
    if digest(archive) != expected_sha256:
        raise ValueError('ZIP SHA256 mismatch; installation unchanged')
    if backup.exists() or stage.exists():
        raise RuntimeError('Previous update pending; recover before retry')
    with lock.open('x'):
        pass
    swapped = False
    try:
        old = installed_manifest(install)
        bundle=old.get('contract')==BUNDLE_CONTRACT
        marker=BUNDLE_MANIFEST if bundle else 'package-manifest.json'
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if len({n.casefold() for n in names}) != len(names): raise ValueError('Duplicate ZIP paths')
            new = json.loads(z.read(marker))
            if new.get('update_base_files_sha256') is not None and new['update_base_files_sha256']!=files_baseline(old['files']):
                raise ValueError('Installed baseline changed after download')
            if bundle and (new.get('contract')!=BUNDLE_CONTRACT or new.get('app_dir')!='DKB_Auto'):raise ValueError('Invalid bundle update')
            if old['project'] != new['project']: raise ValueError('Project mismatch')
            if any(user_file(n) for n in new['files']) or any(user_file(n) for n in new.get('removed', [])):
                raise ValueError('User files cannot be updated or deleted')
            if not bundle and old['runtime_version'] != new['runtime_version']:
                raise ValueError('Runtime version change requires separate installation/release')
            for n in names:
                safe(stage, n)
                if n == 'license.dat':
                    # Full-install convenience file only; never replace a user's key.
                    if z.getinfo(n).file_size > 512 or json.loads(z.read(n).decode('utf-8-sig')) != {'license_key': ''}:
                        raise ValueError('Release license must be an empty placeholder')
                    continue
                if n != marker and n not in new['files']: raise ValueError('Unlisted ZIP file')
            removed = {n for n in set(old['files']) - set(new['files']) if not user_file(n)}
            if removed != set(new.get('removed', [])): raise ValueError('Deletion manifest mismatch')
            report_progress('현재 설치 파일 검증 중...')
            for n, h in old['files'].items():
                if user_file(n): continue
                p = safe(install, n)
                if not p.is_file() or digest(p) != h: raise ValueError('Local managed file changed: ' + n)
            # Reject reparse links; staging must never write outside this installation.
            report_progress('설치 경로 확인 중...')
            if any(p.is_symlink() or (getattr(p.lstat(), 'st_file_attributes', 0) & 1024)
                   for top in install.iterdir() if top.name!='.update'
                   for p in ([top,*top.rglob('*')] if top.is_dir() else [top])): raise ValueError('Linked installation unsupported')
            report_progress('복구용 설치본 준비 중...')
            def stage_file(source,target):
                name=Path(source).relative_to(install).as_posix()
                # Unchanged managed files share bytes only; never write through
                # these links. User files keep the original independent copy.
                if bundle and name in old['files'] and not user_file(name):
                    try:os.link(source,target);return target
                    except OSError:pass
                return shutil.copy2(source,target)
            shutil.copytree(install, stage,copy_function=stage_file,
                ignore=lambda home,names: ['.update'] if Path(home)==install else [])
            for n in removed: safe(stage, n).unlink()
            report_progress('변경 파일 적용 중...')
            for n in names:
                if n == 'license.dat': continue
                target = safe(stage, n)
                if n not in old['files'] and n != marker and target.exists():
                    raise ValueError('Unmanaged file collision: ' + n)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Break any staging hard link before replacing managed bytes.
                target.unlink(missing_ok=True)
                target.write_bytes(z.read(n))
            report_progress('업데이트 파일 검증 중...')
            for n, h in new['files'].items():
                if bundle and old['files'].get(n)==h and n not in names:continue
                p = safe(stage, n)
                if not p.is_file() or digest(p) != h: raise ValueError('File SHA256 mismatch: ' + n)
        report_progress('설치본 교체 중...')
        if bundle:
            swap_payload(install,stage,backup);swapped=True
        else:
            os.replace(install, backup)
            try:
                os.replace(stage, install);swapped=True
            except BaseException:
                os.replace(backup, install);raise
        # Keep the previous version until explicit finish/recover.
        return 'Applied and verified; previous version retained at ' + str(backup)
    except Exception as exc:
        report_progress('업데이트 적용 실패: '+str(exc))
        raise
    finally:
        failing=sys.exc_info()[0] is not None
        try:
            if not swapped and stage.exists() and not (backup.parent/'transaction.json').exists():
                report_progress('실패한 임시 설치본 정리 중...')
                shutil.rmtree(stage)
            if not (backup.parent/'transaction.json').exists():lock.unlink(missing_ok=True)
        except OSError as exc:
            report_progress('임시 설치본 정리 실패: '+str(exc))
            if not failing:raise


def recover(install):
    install = managed_root(install)
    backup,stage,lock=workspace(install)
    # Explicit recovery is only for a stopped updater/app, including interrupted swaps.
    if not backup.is_dir(): raise ValueError('No backup; original installation must be checked manually')
    if stage.exists(): raise ValueError('Stage remains; preserve it and inspect before recovery')
    # Rolling back program files must not roll back a newly entered user key.
    # Empty full-install placeholders must never replace the retained real key.
    if install.is_dir():
        old=json.loads((backup/manifest_name(backup)).read_text(encoding='utf-8'))
        prefix=old['app_dir']+'/' if old.get('contract')==BUNDLE_CONTRACT else ''
        for path in (p for top in install.iterdir() if top.name!='.update'
                     for p in (top.rglob('*') if top.is_dir() else [top])):
            if path.is_file() and not path.is_symlink() and user_file(path.relative_to(install).as_posix()):
                name=path.relative_to(install).as_posix()
                if name in (prefix+'license.dat',prefix+'settings/license-backup.json'):continue
                target=safe(backup,name);target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target)
        for name in (prefix+'license.dat',prefix+'settings/license-backup.json'):
            source=safe(install,name)
            if not source.is_file():continue
            try:
                data=json.loads(source.read_text(encoding='utf-8-sig'))
                key=data.get('license_key')
                valid=isinstance(key,str) and len(key.strip())>=12 and key!='이 따옴표 안에 키값을 넣어주세요'
            except (OSError,ValueError,AttributeError):valid=False
            if valid:
                target=safe(backup,name);target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
    if backup.parent==install/'.update':
        swap_payload(install,backup,stage)
    else:
        if install.exists(): os.replace(install, stage)
        try: os.replace(backup, install)
        except BaseException:
            if stage.exists(): os.replace(stage, install)
            raise
    if stage.exists(): shutil.rmtree(stage)
    lock.unlink(missing_ok=True)
    return 'Previous installation restored'


def finish(install):
    """Discard the retained previous version only after explicit acceptance."""
    install = managed_root(install)
    backup,stage,lock=workspace(install)
    if lock.exists() or stage.exists() or (backup.parent/'transaction.json').exists():
        raise RuntimeError('Updater is running or interrupted; inspect before finishing')
    verify_installed(install)
    if backup.exists(): shutil.rmtree(backup)
    return 'Update accepted; backup removed'


_cancel_event = None

def set_cancel_event(event):
    """GUI-owned checks/downloads are cancelled when their owner exits."""
    global _cancel_event
    _cancel_event = event

def download(url):
    def checkpoint():
        if _cancel_event is not None and _cancel_event.is_set():
            raise InterruptedError('Update download cancelled')
    checkpoint()
    if urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('HTTPS download required')
    req = urllib.request.Request(url, headers={'User-Agent': 'BIOL-DKB-Updater/1.0', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=3 if _cancel_event is not None else 60) as response:
        if urllib.parse.urlparse(response.url).scheme != 'https':
            raise ValueError('Insecure redirect rejected')
        chunks=[]
        while True:
            checkpoint()
            chunk=response.read(65536)
            if not chunk:break
            chunks.append(chunk)
        checkpoint()
        return b''.join(chunks)


def version_key(value):
    match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?',str(value))
    if not match:raise ValueError('Invalid project release version')
    suffix=match[4]
    pre=(1,) if suffix is None else (0,tuple((0,int(v)) if v.isdigit() else (1,v) for v in suffix.split('.')))
    return (int(match[1]),int(match[2]),int(match[3]),pre)


class RemoteDelta(io.RawIOBase):
    """Read ZIP directory and selected compressed entries, never a Full asset."""
    def __init__(self,url):
        super().__init__()
        if urllib.parse.urlparse(url).scheme!='https':raise ValueError('HTTPS required')
        self.url=url;self.position=0;self.length=None
        self._range(0,0)

    def _range(self,start,end):
        if _cancel_event is not None and _cancel_event.is_set():raise InterruptedError('Update cancelled')
        req=urllib.request.Request(self.url,headers={'User-Agent':'BIOL-DKB-Updater/1.0',
            'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'})
        with urllib.request.urlopen(req,timeout=3 if _cancel_event is not None else 60) as response:
            if urllib.parse.urlparse(response.url).scheme!='https':raise ValueError('Insecure redirect')
            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
            if response.status!=206 or not match or (int(match[1]),int(match[2]))!=(start,end):
                raise RuntimeError('Delta server does not support selective download; Full download refused')
            length=int(match[3])
            if self.length is not None and self.length!=length:raise ValueError('Remote delta changed')
            self.length=length
            data=response.read(end-start+2)
            if len(data)!=end-start+1:raise ValueError('Incomplete delta range')
            return data

    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.position
    def seek(self,offset,whence=0):
        position=offset if whence==0 else self.position+offset if whence==1 else self.length+offset
        if position<0:raise ValueError('Negative ZIP offset')
        self.position=position;return position
    def read(self,size=-1):
        if self.position>=self.length:return b''
        end=self.length-1 if size<0 else min(self.length-1,self.position+size-1)
        if end<self.position:return b''
        data=self._range(self.position,end);self.position=end+1;return data


def release_documents(release,cfg):
    assets={a['name']:a['browser_download_url'] for a in release['assets']}
    meta=json.loads(download(assets['release.json']))
    if (meta.get('contract'),meta.get('project'),meta.get('repository'),meta.get('tag'))!=(
            'biol-project-update-v1',cfg['project'],cfg['repository'],release['tag_name']):
        raise ValueError('Release identity mismatch')
    if version_key(meta['version'])!=version_key(release['tag_name']):raise ValueError('Release version mismatch')
    data=download(assets['package-manifest.json'])
    if hashlib.sha256(data).hexdigest()!=meta['package_manifest_sha256']:raise ValueError('Release manifest SHA256 mismatch')
    manifest=json.loads(data)
    if manifest['project']!=cfg['project'] or manifest['runtime_version']!=meta['runtime_version']:
        raise ValueError('Release manifest identity mismatch')
    return meta,manifest,assets


def inspect_release(install):
    """Read-only structured check; safe while the app holds its instance mutex."""
    cfg = json.loads(Path(__file__).with_name('update-config.json').read_text(encoding='utf-8'))
    if cfg.get('contract') != 'biol-project-update-source-v1' or cfg.get('repository') != 'stupid42sr-lang/DKB_Auto_Updater':
        raise ValueError('Unexpected update source')
    old = installed_manifest(install)
    if old['project'] != cfg['project']:
        raise ValueError('Install project mismatch')
    releases = json.loads(download('https://api.github.com/repos/' + cfg['repository'] + '/releases?per_page=100'))
    choices = [r for r in releases if not r['draft'] and (cfg.get('allow_prerelease') or not r['prerelease'])]
    if not choices:
        raise ValueError('No permitted published release')
    release = max(choices,key=lambda r:version_key(r['tag_name']))
    assets = {a['name']: a['browser_download_url'] for a in release['assets']}
    meta = json.loads(download(assets['release.json']))
    if meta.get('contract') != 'biol-project-update-v1' or meta['project'] != cfg['project'] or meta['repository'] != cfg['repository'] or meta['tag'] != release['tag_name']:
        raise ValueError('Release identity mismatch')
    if version_key(meta['version'])!=version_key(release['tag_name']):
        raise ValueError('Release version mismatch')
    manifest_data = download(assets['package-manifest.json'])
    if hashlib.sha256(manifest_data).hexdigest() != meta['package_manifest_sha256']:
        raise ValueError('Release manifest SHA256 mismatch')
    new = json.loads(manifest_data)
    if new['project']!=cfg['project'] or new['runtime_version']!=meta['runtime_version']:
        raise ValueError('Release manifest identity mismatch')
    bundle=old.get('contract')==BUNDLE_CONTRACT
    app=managed_root(install)/old['app_dir'] if bundle else Path(install)
    current=json.loads((app/'runtime/products'/(old['project']+'.json')).read_text(encoding='utf-8'))['app_version']
    result=dict(version=meta['version'],current_version=current,metadata=meta,assets=assets,
                manifest_sha256=meta['package_manifest_sha256'])
    if version_key(meta['version'])<=version_key(current):return dict(result,state='current')
    if bundle and new.get('contract')!=BUNDLE_CONTRACT:raise ValueError('Bundle update contract missing')
    if not bundle and new.get('contract')==BUNDLE_CONTRACT:return dict(result,state='full_install_required')
    if not bundle and old['runtime_version'] != meta['runtime_version']:return dict(result,state='runtime_required')
    if not bundle and old.get('launcher',{}).get('base_version')!=meta.get('base_version'):return dict(result,state='base_required')
    baseline = {p: h for p, h in old['files'].items() if not user_file(p)}
    actual = hashlib.sha256(json.dumps(baseline, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if actual != meta['base_files_sha256']:
        if bundle:
            if new.get('app_dir')!='DKB_Auto':raise ValueError('Invalid latest bundle identity')
            # Intermediate ZIPs are file sources only; install latest once.
            return dict(result,state='available',direct_delta=True,target_manifest=new,
                release_candidates=[{'tag_name':r['tag_name'],'assets':r['assets']} for r in choices
                    if version_key(current)<version_key(r['tag_name'])<=version_key(meta['version'])])
        return dict(result,state='baseline_mismatch')
    return dict(result,state='available')


def prepare_update(install):
    result=inspect_release(install)
    if result['state']!='available':raise ValueError('Project delta is not applicable: '+result['state'])
    root=managed_root(install)
    prepare_workspace(root)
    if any(path.exists() for path in workspace(root)):
        raise RuntimeError('Previous update pending; inspect before applying')
    meta=result['metadata']
    if result.get('direct_delta'):
        return prepare_direct_delta(install,result)
    data=download(result['assets'][meta['delta']['name']])
    if hashlib.sha256(data).hexdigest()!=meta['delta']['sha256']:raise ValueError('ZIP SHA256 mismatch')
    folder=Path(tempfile.mkdtemp(prefix='biol-project-delta-'))
    archive=folder/'update.zip'
    try:
        archive.write_bytes(data)
        with zipfile.ZipFile(archive) as z:
            marker=BUNDLE_MANIFEST if installed_manifest(install).get('contract')==BUNDLE_CONTRACT else 'package-manifest.json'
            if hashlib.sha256(z.read(marker)).hexdigest()!=result['manifest_sha256']:
                raise ValueError('Delta manifest SHA256 mismatch')
        return dict(result,archive=str(archive),download_dir=str(folder),sha256=meta['delta']['sha256'])
    except BaseException:
        shutil.rmtree(folder);raise


def prepare_direct_delta(install,result):
    old=installed_manifest(install);new=dict(result['target_manifest'])
    needed={n:h for n,h in new['files'].items() if old['files'].get(n)!=h}
    new['removed']=sorted(set(old['files'])-set(new['files']))
    new['update_base_files_sha256']=files_baseline(old['files'])
    if any(user_file(n) for n in (*new['files'],*new['removed'])):
        raise ValueError('User files cannot be updated or deleted')
    folder=Path(tempfile.mkdtemp(prefix='biol-project-delta-'));archive=folder/'update.zip'
    cfg=json.loads(Path(__file__).with_name('update-config.json').read_text(encoding='utf-8'))
    try:
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
            for candidate in sorted(result['release_candidates'],key=lambda r:version_key(r['tag_name']),reverse=True):
                if not needed:break
                cm,cn,assets=release_documents(candidate,cfg)
                if cn.get('contract')!=BUNDLE_CONTRACT or cn.get('app_dir')!='DKB_Auto':
                    raise ValueError('Invalid delta source bundle')
                matching={n for n,h in needed.items() if cn['files'].get(n)==h}
                if not matching:continue
                with RemoteDelta(assets[cm['delta']['name']]) as remote,zipfile.ZipFile(remote) as source:
                    names=source.namelist()
                    if len({n.casefold() for n in names})!=len(names):raise ValueError('Duplicate ZIP paths')
                    if hashlib.sha256(source.read(BUNDLE_MANIFEST)).hexdigest()!=cm['package_manifest_sha256']:
                        raise ValueError('Delta manifest SHA256 mismatch')
                    for name in sorted(matching.intersection(names)):
                        safe(folder,name)
                        report_progress('변경 파일 다운로드: '+name)
                        data=source.read(name)
                        if hashlib.sha256(data).hexdigest()!=needed[name]:raise ValueError('Delta file SHA256 mismatch: '+name)
                        output.writestr(name,data);del needed[name]
            if needed:raise ValueError('Required latest files unavailable in published deltas: '+', '.join(sorted(needed)))
            output.writestr(BUNDLE_MANIFEST,json.dumps(new,ensure_ascii=False,indent=2).encode('utf-8'))
        prepared={k:v for k,v in result.items() if k not in ('target_manifest','release_candidates')}
        return dict(prepared,archive=str(archive),download_dir=str(folder),sha256=digest(archive))
    except BaseException:
        shutil.rmtree(folder);raise


def github_update(install, check=False):
    result=inspect_release(install)
    if result['state']=='current':return 'Already current: '+result['version']
    if result['state']!='available':raise ValueError('Project update blocked: '+result['state'])
    if check:
        return 'Matching delta available: '+result['metadata']['tag']
    prepared=prepare_update(install)
    try:return apply(install,prepared['archive'],prepared['sha256'])
    finally:shutil.rmtree(prepared['download_dir'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install', required=True)
    p.add_argument('--zip')
    p.add_argument('--sha256')
    p.add_argument('--recover', action='store_true')
    p.add_argument('--finish', action='store_true')
    p.add_argument('--github', action='store_true')
    p.add_argument('--check', action='store_true')
    a = p.parse_args()
    if a.check and not a.github: p.error('--check requires --github')
    modes = [a.recover, a.finish, a.github, bool(a.zip and a.sha256)]
    if sum(map(bool, modes)) != 1: p.error('Choose exactly one of --github, --zip/--sha256, --recover, --finish')
    with app_closed(a.install):
        if a.recover: print(recover(a.install))
        elif a.finish: print(finish(a.install))
        elif a.github: print(github_update(a.install, a.check))
        else: print(apply(a.install, a.zip, a.sha256))
