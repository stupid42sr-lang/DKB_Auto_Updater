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
import urllib.error
import http.client
import re
import io
import sys
import uuid
import ssl
import time
import random
import logging
import math
import threading
import types
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from contextlib import contextmanager

BUNDLE_CONTRACT='biol-dkb-distribution-v1'
BUNDLE_MANIFEST='distribution-manifest.json'
BACKUP_OPTIONAL_PROTOCOL=1
LIVE_PREPARATION_PROTOCOL=1
UNBACKED_MARKER='unbacked-update.json'
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


def workspace(install,*,create=True):
    install=managed_root(install)
    bundle=(install/BUNDLE_MANIFEST).is_file() or (install/'.update/backup'/BUNDLE_MANIFEST).is_file()
    if bundle:
        home=install/'.update'
        if home.is_symlink() or (home.exists() and getattr(home.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked update workspace unsupported')
        if create:home.mkdir(exist_ok=True)
        return home/'backup',home/'stage',home/'lock'
    return tuple(install.with_name(install.name+suffix) for suffix in ('.update-backup','.update-stage','.update-lock'))


def verify_installed(install):
    manifest=installed_manifest(install)
    for name,value in manifest['files'].items():
        if not user_file(name) and digest(safe(install,name))!=value:
            raise ValueError('Installed file SHA256 mismatch: '+name)
    return manifest


def _archive_backup(source,target):
    """Retry transient Windows directory locks without discarding old bytes."""
    for attempt in range(5):
        try:
            os.replace(source,target)
            return
        except PermissionError as error:
            if getattr(error,'winerror',None) not in (5,32,33):raise
            if attempt==4:
                report_progress('이전 업데이트 백업 보관 실패 · 현재 설치·백업 유지 · 파일 사용 프로그램과 동기화 상태 확인 필요')
                raise
            report_progress('이전 업데이트 백업 사용 중 · 보관 재시도 %d/4'%(attempt+1))
            time.sleep(.5)


def prepare_workspace(install, preserve_damaged=False):
    """Retain completed backups internally; never discard interrupted work."""
    install=managed_root(install)
    if not (install/BUNDLE_MANIFEST).is_file():return
    backup,stage,lock=workspace(install)
    legacy=tuple(install.with_name(install.name+s) for s in ('.update-backup','.update-stage','.update-lock'))
    if stage.exists() or lock.exists() or any((backup.parent/n).exists() for n in ('transaction.json','transaction.tmp')) or any(p.exists() for p in legacy[1:]):
        raise RuntimeError('Previous update interrupted; inspect before applying')
    retained=[p for p in (backup,legacy[0]) if p.exists()]
    if retained:
        receipt=backup.parent/'accepted.json'
        accepted=False
        try:accepted=json.loads(receipt.read_text(encoding='utf-8'))['manifest_sha256']==digest(install/BUNDLE_MANIFEST)
        except (OSError,ValueError,KeyError):pass
        if (not accepted or legacy[0].exists()
                or (backup in retained and not (backup/BUNDLE_MANIFEST).is_file())):
            try:verify_installed(install)
            except (OSError,ValueError):
                # Complete backups can be archived without discarding evidence.
                # Partial/interrupted work still requires explicit recovery.
                if not preserve_damaged or any(not (p/BUNDLE_MANIFEST).is_file() for p in retained):raise
                accepted=False
        if accepted:prune_history(install)
        history=backup.parent/'history'
        try:history.mkdir(exist_ok=True)
        except PermissionError as error:
            if getattr(error,'winerror',None) not in (5,32,33):raise
            report_progress('이전 백업 보관 폴더 접근 불가 · 기존 백업 유지 · 로컬 복구 백업 없이 업데이트 계속')
            return False
        for path in retained:
            if path==backup and not (path/BUNDLE_MANIFEST).is_file():
                # Manual rollback can leave a partial backup directory. Preserve
                # it, but only after proving the current installation is intact.
                if (path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&1024
                        or history.is_symlink() or getattr(history.lstat(),'st_file_attributes',0)&1024
                        or path.resolve().parent!=backup.parent.resolve()
                        or history.resolve().parent!=backup.parent.resolve()):
                    raise ValueError('Linked incomplete backup unsupported')
                target=history/('incomplete-'+uuid.uuid4().hex)
                try:_archive_backup(path,target)
                except PermissionError as error:
                    if getattr(error,'winerror',None) not in (5,32,33):raise
                    report_progress('이전 백업 보관 생략 · 기존 백업 유지 · 로컬 복구 백업 없이 업데이트 계속')
                    return False
                report_progress('불완전한 백업 별도 보관 · 현재 설치 검증 완료 · '+str(target))
                continue
            old=json.loads((path/BUNDLE_MANIFEST).read_text(encoding='utf-8'))
            if old.get('contract')!=BUNDLE_CONTRACT or old.get('project')!='DKB':raise ValueError('Invalid retained backup')
            try:_archive_backup(path,history/('backup-'+uuid.uuid4().hex))
            except PermissionError as error:
                if getattr(error,'winerror',None) not in (5,32,33):raise
                report_progress('이전 백업 보관 생략 · 기존 백업 유지 · 로컬 복구 백업 없이 업데이트 계속')
                return False
        receipt.unlink(missing_ok=True)
    legacy_log=install.with_name('DKB-update.log')
    if legacy_log.is_file():
        logs=backup.parent/'logs';logs.mkdir(exist_ok=True)
        os.replace(legacy_log,logs/('legacy-'+uuid.uuid4().hex+'.log'))


def prune_history(install):
    """Remove only completed DKB history; preserve the current rollback backup."""
    install=managed_root(install)
    backup,stage,lock=workspace(install)
    if backup.parent!=install/'.update':return
    if stage.exists() or lock.exists() or (backup.parent/'transaction.json').exists():return
    history=backup.parent/'history'
    if not history.exists():return
    def linked(path):
        return path.is_symlink() or bool(getattr(path.lstat(),'st_file_attributes',0)&1024)
    try:
        if linked(history) or history.resolve().parent!=backup.parent.resolve():
            raise ValueError('Linked history directory unsupported')
        for path in history.iterdir():
            if not path.name.startswith('backup-'):continue
            try:
                if linked(path) or path.resolve().parent!=history.resolve() or not path.is_dir():
                    raise ValueError('Invalid history backup path')
                old=json.loads((path/BUNDLE_MANIFEST).read_text(encoding='utf-8'))
                if old.get('contract')!=BUNDLE_CONTRACT or old.get('project')!='DKB':
                    raise ValueError('Invalid history backup identity')
                # Refuse junctions throughout the tree before recursive removal.
                for home,dirs,files in os.walk(path,followlinks=False):
                    if any(linked(Path(home)/name) for name in (*dirs,*files)):
                        raise ValueError('Linked history backup contents unsupported')
                report_progress('과거 업데이트 백업 정리 중...')
                shutil.rmtree(path)
            except (OSError,ValueError) as exc:
                report_progress('과거 백업 정리 보류 · '+path.name+' · '+str(exc))
    except (OSError,ValueError) as exc:
        report_progress('과거 업데이트 백업 정리 보류 · '+str(exc))


def accept(install):
    """Restart acknowledged; retain rollback without blocking the next update."""
    install=managed_root(install)
    if not (install/BUNDLE_MANIFEST).is_file():return
    backup,stage,lock=workspace(install)
    (backup.parent/'accepted.json').write_text(json.dumps({'manifest_sha256':digest(install/BUNDLE_MANIFEST)}),encoding='utf-8')
    prune_history(install)


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
def app_closed(install, identity_root=None):
    """Own the same named object as the existing runtime while swapping files."""
    root = Path(identity_root) if identity_root is not None else managed_root(install)
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


def _apply_without_backup(install,stage,new,removed):
    """User-authorized managed-file replacement; never reuse a stale backup."""
    updater=stage/'DKB_Auto_Updater/updater.py'
    if updater.is_file() and b'BACKUP_OPTIONAL_PROTOCOL=1' not in updater.read_bytes():
        raise ValueError('Target updater does not support backup-free recovery markers; compatible full install required')
    # Staging and package verification remain mandatory before the first write.
    for name,sha in new['files'].items():
        path=safe(stage,name)
        if not path.is_file() or digest(path)!=sha:raise ValueError('Staged file SHA256 mismatch: '+name)
    marker=install/'.update'/UNBACKED_MARKER
    for path in (marker,marker.with_suffix('.tmp')):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked backup-free update marker unsupported')
    state={'state':'applying','manifest_sha256':digest(stage/BUNDLE_MANIFEST)}
    _recovery_write(marker,state)
    report_progress('로컬 복구 백업 없이 업데이트 적용 · 기존 백업·사용자 설정 보존')
    for name,sha in sorted(new['files'].items()):
        source,target=safe(stage,name),safe(install,name)
        if target.is_file() and digest(target)==sha:continue
        target.parent.mkdir(parents=True,exist_ok=True)
        os.replace(source,target)
    for name in removed:safe(install,name).unlink(missing_ok=True)
    for name,sha in new['files'].items():
        if digest(safe(install,name))!=sha:raise ValueError('Installed file SHA256 mismatch: '+name)
    # Publish the new manifest only after every managed file is confirmed.
    os.replace(stage/BUNDLE_MANIFEST,install/BUNDLE_MANIFEST)
    verify_installed(install)
    state['state']='complete';_recovery_write(marker,state)
    shutil.rmtree(stage)
    return 'Applied and verified without a local rollback backup; previous retained backup unchanged'


def apply(install, archive, expected_sha256):
    install, archive = managed_root(install), Path(archive).resolve()
    backup_optional=prepare_workspace(install, preserve_damaged=True) is False
    backup,stage,lock=workspace(install)
    report_progress('다운로드 파일 검증 중...')
    if digest(archive) != expected_sha256:
        raise ValueError('ZIP SHA256 mismatch; installation unchanged')
    if (backup.exists() and not backup_optional) or stage.exists():
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
                if not p.is_file() or digest(p) != h:
                    # Never use damaged bytes as the base of a new install.
                    # Only verified replacement bytes (or declared removal)
                    # make an old mismatch irrelevant to the target payload.
                    if n in removed:continue
                    if (n not in names or n not in new['files']
                            or hashlib.sha256(z.read(n)).hexdigest()!=new['files'][n]):
                        raise ValueError('Local managed file changed; verified repair required: ' + n)
                    report_progress('손상 파일 복구 준비: '+n)
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
            for n in removed: safe(stage, n).unlink(missing_ok=True)
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
            marker_path=install/'.update'/UNBACKED_MARKER
            if backup_optional:
                result=_apply_without_backup(install,stage,new,removed);swapped=True
                return result
            # A new normal backup supersedes the old backup-free completion marker.
            if marker_path.exists():
                if json.loads(marker_path.read_text(encoding='utf-8')).get('state')!='complete':
                    raise RuntimeError('Interrupted backup-free update; verified full installation required')
                marker_path.unlink()
            try:swap_payload(install,stage,backup)
            except PermissionError as error:
                if (getattr(error,'winerror',None) not in (5,32,33)
                        or backup.exists() or (backup.parent/'transaction.json').exists()):raise
                result=_apply_without_backup(install,stage,new,removed);swapped=True
                return result
            swapped=True
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
            unbacked=backup.parent/UNBACKED_MARKER
            interrupted_unbacked=(unbacked.exists() and json.loads(unbacked.read_text(encoding='utf-8')).get('state')=='applying')
            if not swapped and stage.exists() and not (backup.parent/'transaction.json').exists() and not interrupted_unbacked:
                report_progress('실패한 임시 설치본 정리 중...')
                shutil.rmtree(stage)
            if not (backup.parent/'transaction.json').exists() and not interrupted_unbacked:lock.unlink(missing_ok=True)
        except OSError as exc:
            report_progress('임시 설치본 정리 실패: '+str(exc))
            if not failing:raise


def recover(install):
    install = managed_root(install)
    if (install/'.update'/UNBACKED_MARKER).exists():
        raise RuntimeError('No current rollback backup; preserved older backup must not be restored automatically. Use a verified full installation.')
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


def _recovery_write(path, value):
    """Persist intent before a rename; an interrupted rename is reconciled by hashes."""
    temp=path.with_suffix('.tmp')
    with temp.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)


def _recovery_tree(path):
    """Hash all bytes, including user data; reject links before traversing them."""
    if path.is_symlink() or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&1024):
        raise ValueError('Linked recovery path: '+str(path))
    if not path.exists():return None
    if path.is_file():return {'file':digest(path)}
    result={'dir':{}}
    for child in sorted(path.iterdir(),key=lambda p:p.name.casefold()):
        result['dir'][child.name]=_recovery_tree(child)
    return result


def _recovery_name(name):
    if (not isinstance(name,str) or not name or name in ('.','..') or
            any(c in name for c in '/\\:') or name.casefold()=='.update' or
            name.endswith((' ','.'))):
        raise ValueError('Invalid recovery child name')
    return name


def _recovery_manifest(root):
    value=json.loads((root/BUNDLE_MANIFEST).read_text(encoding='utf-8'))
    if (value.get('contract')!=BUNDLE_CONTRACT or value.get('project')!='DKB' or
            value.get('app_dir')!='DKB_Auto' or not isinstance(value.get('files'),dict)):
        raise ValueError('Invalid recovery manifest')
    seen=set()
    for name,sha in value['files'].items():
        safe(root,name)
        if any(part in ('.','..') for part in Path(name).parts) or '\\' in name:
            raise ValueError('Invalid manifest path')
        _recovery_name(Path(name).parts[0])
        if name.casefold() in seen or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
            raise ValueError('Invalid manifest SHA256/path')
        seen.add(name.casefold())
    return value


def recovery_idle(install):
    """Inspect real processes, never interpret a leftover lock file as liveness."""
    import subprocess
    if os.name!='nt':raise RuntimeError('Cannot establish updater process state on this platform')
    code=("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,Name,CommandLine) | ConvertTo-Json -Compress")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',code],
            capture_output=True,encoding='utf-8-sig',errors='strict',timeout=30,
            creationflags=0x08000000,check=True)
        if not result.stdout:raise ValueError('No process inventory output')
        processes=json.loads(result.stdout)
        if not isinstance(processes,list) or not processes:raise ValueError('Empty process inventory')
        by_pid={p['ProcessId']:p for p in processes}
        if os.getpid() not in by_pid:raise ValueError('Incomplete process inventory')
        ancestors=set();pid=os.getpid()
        while pid in by_pid and pid not in ancestors:
            ancestors.add(pid);pid=by_pid[pid]['ParentProcessId']
        for process in processes:
            name=process['Name'].casefold();command=process.get('CommandLine')
            if name.startswith(('python','dkb','biol')) and not command:
                raise RuntimeError('Cannot inspect process: '+name)
            if process['ProcessId'] in ancestors:continue
            text=(command or '').casefold()
            if str(install).casefold() in text and name.startswith(('dkb','python')):
                raise RuntimeError('DKB installation process is still running')
            if any(token in text for token in ('updater.py','project_update.py','bundle_handoff.py','recover_previous.py')):
                raise RuntimeError('Update/recovery process is still running')
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        raise RuntimeError('Cannot establish updater process state; recovery refused') from exc


def _recovery_preflight(root):
    home=root/'.update';backup=home/'backup';stage=home/'stage'
    if (home/UNBACKED_MARKER).exists():
        raise RuntimeError('No current rollback backup; use a verified full installation, older backup preserved')
    # Scan before reading any manifest/journal, including the workspace itself.
    for path in (root,home):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked installation/workspace')
    for child in root.iterdir():
        if child.name!='.update':_recovery_tree(child)
    for leaf in ('backup','stage','transaction.json','transaction.tmp','lock','recovery.json','recovery.tmp','accepted.json'):
        _recovery_tree(home/leaf)
    state_path=home/'recovery.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    pending=any((home/name).exists() for name in ('backup','stage','transaction.json','transaction.tmp','lock'))
    if state is not None and not isinstance(state,dict):raise ValueError('Invalid recovery progress')
    if state and (not state.get('complete') or not pending):return state
    journal_path=home/'transaction.json'
    journal=None
    if not journal_path.exists() and (home/'transaction.tmp').exists():journal_path=home/'transaction.tmp'
    if journal_path.exists():
        journal=json.loads(journal_path.read_text(encoding='utf-8'))
        if not isinstance(journal,dict) or set(journal)!={'old','new'}:raise ValueError('Invalid swap journal')
        for names in journal.values():
            if not isinstance(names,list):raise ValueError('Invalid swap journal entries')
            for name in names:_recovery_name(name)
            if len({n.casefold() for n in names})!=len(names):raise ValueError('Duplicate journal entry')
        if not backup.is_dir():raise ValueError('Journal backup missing')
    elif not pending:
        raise ValueError('No previous installation or interrupted update')
    # An old child is either in backup or still in install. Intent alone is not proof of a move.
    old_names=set(p.name for p in backup.iterdir()) if backup.exists() else set()
    if journal:
        old_names.update(journal['old'])
        old_names.update(p.name for p in root.iterdir() if p.name!='.update' and p.name not in journal['new'])
    elif not backup.exists() or not old_names:
        old_names.update(p.name for p in root.iterdir() if p.name!='.update')
    if len({name.casefold() for name in old_names})!=len(old_names):raise ValueError('Ambiguous original child names')
    selected={}
    for name in old_names:
        _recovery_name(name)
        source=backup/name
        if not source.exists():
            source=root/name
            if journal and name in journal['new'] and not (stage/name).exists():
                raise ValueError('Original child missing: '+name)
        if not source.exists():raise ValueError('Original child missing: '+name)
        selected[name]=source
    manifest_source=selected.get(BUNDLE_MANIFEST)
    if manifest_source is None:raise ValueError('Original manifest missing')
    manifest=_recovery_manifest(manifest_source.parent)
    # Verify the split old installation before creating a recovery plan or moving anything.
    for name,sha in manifest['files'].items():
        parts=Path(name).parts;top=selected.get(parts[0])
        if top is None:raise ValueError('Original file missing: '+name)
        path=top.joinpath(*parts[1:])
        if not user_file(name) and (not path.is_file() or digest(path)!=sha):
            raise ValueError('Original file SHA256 mismatch: '+name)
    old_user={}
    for name,source in selected.items():
        for path in (source.rglob('*') if source.is_dir() else [source]):
            relative=Path(name)/path.relative_to(source) if source.is_dir() else Path(name)
            if path.is_file() and user_file(relative.as_posix()):old_user[relative.as_posix()]=digest(path)
    for other in (root,stage):
        if not other.exists():continue
        for child in other.iterdir():
            if child.name=='.update':continue
            for path in (child.rglob('*') if child.is_dir() else [child]):
                name=path.relative_to(other).as_posix()
                if path.is_file() and user_file(name):
                    if name not in old_user or old_user[name]!=digest(path):
                        raise ValueError('User data copies conflict; preserved: '+name)
    work=home/('recovery-'+uuid.uuid4().hex)
    restore=work/'restore';restore.mkdir(parents=True)
    for name,source in selected.items():
        target=restore/name
        if source.is_dir():shutil.copytree(source,target)
        else:shutil.copy2(source,target)
        if _recovery_tree(source)!=_recovery_tree(target):
            raise ValueError('Recovery copy/user data verification failed: '+name)
    if _recovery_manifest(restore)!=manifest:raise ValueError('Recovery manifest changed')
    for name,sha in manifest['files'].items():
        if not user_file(name) and digest(safe(restore,name))!=sha:raise ValueError('Recovery copy verification failed')
    identity=work/'identity'
    (identity/'DKB_Auto/runtime/products').mkdir(parents=True)
    shutil.copy2(restore/BUNDLE_MANIFEST,identity/BUNDLE_MANIFEST)
    shutil.copy2(restore/'DKB_Auto/runtime/products/DKB.json',identity/'DKB_Auto/runtime/products/DKB.json')
    operations=[]
    def move(source,target):
        operations.append({'source':source.relative_to(root).as_posix(),
                           'target':target.relative_to(root).as_posix(),'tree':_recovery_tree(source)})
    for child in root.iterdir():
        if child.name!='.update':move(child,work/'replaced'/child.name)
    for child in restore.iterdir():move(child,root/child.name)
    for name in ('backup','stage','transaction.json','transaction.tmp','lock','accepted.json'):
        if (home/name).exists():move(home/name,work/'evidence'/name)
    state={'version':1,'work':work.name,'manifest':manifest,'operations':operations,'next':0,'complete':False}
    _recovery_write(state_path,state)
    return state


def _recovery_validate(root,state):
    if (not isinstance(state,dict) or state.get('version')!=1 or
            not re.fullmatch('recovery-[0-9a-f]{32}',str(state.get('work',''))) or
            not isinstance(state.get('operations'),list) or type(state.get('next')) is not int or
            not 0<=state['next']<=len(state['operations']) or type(state.get('complete')) is not bool):
        raise ValueError('Invalid recovery progress')
    work=root/'.update'/state['work'];_recovery_tree(work)
    seen=set()
    for op in state['operations']:
        source=op.get('source');target=op.get('target')
        if not isinstance(source,str) or not isinstance(target,str):raise ValueError('Invalid recovery operation')
        sp=source.split('/');tp=target.split('/')
        for part in sp+tp:
            if part!='.update':_recovery_name(part)
        valid=(len(sp)==1 and sp[0]!='.update' and tp==['.update',state['work'],'replaced',sp[0]] or
               len(sp)==4 and sp[3]!='.update' and sp[:3]==['.update',state['work'],'restore'] and tp==[sp[3]] or
               len(sp)==2 and sp[0]=='.update' and sp[1] in
               ('backup','stage','transaction.json','transaction.tmp','lock','accepted.json') and
               tp==['.update',state['work'],'evidence',sp[1]])
        if not valid or target.casefold() in seen or op.get('tree') is None:
            raise ValueError('Unsafe recovery operation')
        seen.add(target.casefold())
        for path in (root/source,root/target):
            if not path.resolve().is_relative_to(root):raise ValueError('Recovery path escapes installation')
            # Check every existing ancestor before creating destination parents.
            for parent in (path,*path.parents):
                if parent==root.parent:break
                if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(),'st_file_attributes',0)&1024):
                    raise ValueError('Linked recovery operation')
    return work


@contextmanager
def _recovery_guard(root):
    for path in (root,*root.parents,root/'.update'):
        if path.is_symlink() or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&1024):
            raise ValueError('Linked recovery root')
    if not (root/'.update').is_dir():raise ValueError('No update workspace')
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    k.CreateMutexW.restype=wintypes.HANDLE
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    name=hashlib.sha256(str(root).casefold().encode('utf-8')).hexdigest()
    ctypes.set_last_error(0)
    handle=k.CreateMutexW(None,False,'Local\\DKB_Explicit_Recovery_'+name)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error()==183:raise RuntimeError('Another recovery owns the workspace')
        yield
    finally:k.CloseHandle(handle)


def recover_interrupted(install):
    root=Path(install).absolute()
    with _recovery_guard(root):return _recover_interrupted_locked(root)


def _recover_interrupted_locked(install):
    """Explicit bundle recovery only. Never invoked by normal apply/prepare."""
    root=Path(install).absolute()
    recovery_idle(root)
    state=_recovery_preflight(root)
    work=_recovery_validate(root,state)
    # Identity is taken from the verified original copy, even when install is split.
    identity=work/'identity'
    manifest=_recovery_manifest(identity)
    if state['manifest']!=manifest:raise ValueError('Recovery identity/plan mismatch')
    product_path='DKB_Auto/runtime/products/DKB.json'
    if product_path not in manifest['files'] or digest(identity/product_path)!=manifest['files'][product_path]:
        raise ValueError('Cannot establish verified application identity')
    with app_closed(root,identity_root=identity):
        while state['next']<len(state['operations']):
            op=state['operations'][state['next']];source=root/op['source'];target=root/op['target']
            src=_recovery_tree(source);dst=_recovery_tree(target)
            if src==op['tree'] and dst is None:
                target.parent.mkdir(parents=True,exist_ok=True)
                os.replace(source,target)
            elif src is not None or dst!=op['tree']:
                raise ValueError('Recovery files changed/conflict; preserved: '+op['source'])
            state['next']+=1;_recovery_write(root/'.update/recovery.json',state)
        if _recovery_manifest(root)!=manifest:raise ValueError('Restored manifest mismatch')
        for name,sha in manifest['files'].items():
            if not user_file(name) and digest(safe(root,name))!=sha:raise ValueError('Restored file mismatch: '+name)
        state['complete']=True;_recovery_write(root/'.update/recovery.json',state)
    return 'Previous installation verified and restored; replaced files and evidence retained at '+str(work)


def finish(install):
    """Discard the retained previous version only after explicit acceptance."""
    install = managed_root(install)
    backup,stage,lock=workspace(install)
    if lock.exists() or stage.exists() or (backup.parent/'transaction.json').exists():
        raise RuntimeError('Updater is running or interrupted; inspect before finishing')
    verify_installed(install)
    if (backup.parent/UNBACKED_MARKER).exists():
        return 'Update accepted without local rollback; older backup preserved'
    if backup.exists(): shutil.rmtree(backup)
    return 'Update accepted; backup removed'


_cancel_event = None
_ssl_context = None


def update_ssl_context():
    """Keep OS trust and add the CA bundle already shipped in the Runtime."""
    global _ssl_context
    if _ssl_context is None:
        context=ssl.create_default_context()
        try:
            import certifi
        except ImportError:
            pass  # Standalone use retains the existing verified OS trust path.
        else:
            context.load_verify_locations(cafile=certifi.where())
        _ssl_context=context
    return _ssl_context

def set_cancel_event(event):
    """GUI-owned checks/downloads are cancelled when their owner exits."""
    global _cancel_event
    _cancel_event = event

# Shared only within one DKB updater process, including fresh backend imports.
# No credentials, user settings, persistent timer or installed files are stored.
_candidate=types.ModuleType('_biol_dkb_update_transport_v1')
_candidate.lock=threading.RLock();_candidate.cache=OrderedDict();_candidate.inflight={};_candidate.cooldown={};_candidate.jobs=set()
_shared=sys.modules.setdefault('_biol_dkb_update_transport_v1',_candidate)
_request_local=threading.local()


class UpdateDeferred(RuntimeError):
    def __init__(self,stage,reason,wait=0):
        self.stage=stage;self.reason=reason;self.wait_seconds=max(0,float(wait))
        self.update_diagnostic=dict(protocol=1,stage=stage,reason=reason,
            temporary=reason in ('busy','deadline','server_cooldown'),retry_after_seconds=math.ceil(self.wait_seconds))
        super().__init__('Update '+stage+': '+reason+(' (retry after %.1fs)'%self.wait_seconds if wait else ''))


@contextmanager
def request_budget(seconds,stage):
    previous=getattr(_request_local,'deadline',None);prior_stage=getattr(_request_local,'stage',None)
    end=time.monotonic()+seconds
    _request_local.deadline=min(previous,end) if previous is not None else end
    _request_local.stage=stage
    try:yield
    finally:
        _request_local.deadline=previous;_request_local.stage=prior_stage


def network_timeout():
    base=15 if _cancel_event is not None else 60
    end=getattr(_request_local,'deadline',None)
    if end is None:return base
    remaining=end-time.monotonic()
    if remaining<=0:raise UpdateDeferred(getattr(_request_local,'stage',None) or 'network','deadline')
    return max(.05,min(base,remaining))


@contextmanager
def update_request(install,stage,seconds):
    root=managed_root(install).resolve();key=str(root).casefold()
    prior_root=getattr(_request_local,'root',None)
    nested=prior_root==key
    if nested:
        with request_budget(seconds,stage):yield
        return
    with _shared.lock:
        if key in _shared.jobs:raise UpdateDeferred(stage,'busy')
        _shared.jobs.add(key)
    handle=None
    try:
        if os.name=='nt':
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            k.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];k.CreateMutexW.restype=wintypes.HANDLE
            k.CloseHandle.argtypes=[wintypes.HANDLE]
            ctypes.set_last_error(0)
            handle=k.CreateMutexW(None,False,'Local\\DKB_Update_Request_'+hashlib.sha256(key.encode()).hexdigest())
            if not handle:raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error()==183:raise UpdateDeferred(stage,'busy')
        _request_local.root=key
        with request_budget(seconds,stage):yield
    except Exception as exc:
        reason=(exc.reason if isinstance(exc,UpdateDeferred) else
                'http_'+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else
                'cancelled' if isinstance(exc,InterruptedError) else
                'timeout' if isinstance(exc,TimeoutError) or isinstance(getattr(exc,'reason',None),TimeoutError) else
                'tls' if isinstance(getattr(exc,'reason',exc),ssl.SSLError) else
                'connection' if isinstance(exc,urllib.error.URLError) else
                'validation' if isinstance(exc,ValueError) else type(exc).__name__)
        phase=getattr(_request_local,'network_stage',None) or stage
        wait=vars(exc).get('wait_seconds',0)
        if isinstance(exc,urllib.error.HTTPError):
            with _shared.lock:wait=max(wait,_shared.cooldown.get(urllib.parse.urlsplit(exc.filename).netloc,0)-time.monotonic())
        exc.update_diagnostic=dict(protocol=1,stage=phase,reason=reason,
            temporary=reason in ('busy','deadline','server_cooldown','timeout','connection') or reason in ('http_408','http_429','http_500','http_502','http_503','http_504') or reason=='http_403' and wait>0,
            retry_after_seconds=max(0,math.ceil(wait)))
        logging.getLogger('project_gui.DKB').warning('업데이트 분석 · 작업=%s 단계=%s 원인=%s',stage,phase,reason,extra={'gui_suppress':True})
        raise
    finally:
        _request_local.root=prior_root;_request_local.network_stage=None
        if handle is not None:k.CloseHandle(handle)
        with _shared.lock:_shared.jobs.discard(key)


def download_checkpoint():
    if _cancel_event is not None and _cancel_event.is_set():
        raise InterruptedError('Update download cancelled')
    end=getattr(_request_local,'deadline',None)
    if end is not None and time.monotonic()>=end:raise UpdateDeferred(getattr(_request_local,'stage',None) or 'network','deadline')


def transport_checkpoint(url):
    download_checkpoint()
    with _shared.lock:wait=_shared.cooldown.get(urllib.parse.urlsplit(url).netloc,0)-time.monotonic()
    if wait>0:raise UpdateDeferred(getattr(_request_local,'stage',None) or 'network','server_cooldown',wait)


def retry_download(operation,*,index_swap=False,source_url=None):
    """Bound retries for transient transport failures; never weaken validation."""
    for attempt in range(3):
        download_checkpoint()
        try:return operation()
        except (TimeoutError,ConnectionError,http.client.IncompleteRead,urllib.error.URLError) as exc:
            download_checkpoint()
            headers=(getattr(exc,'headers',None) or {})
            limited=isinstance(exc,urllib.error.HTTPError) and exc.code==403 and (
                headers.get('Retry-After') is not None or headers.get('X-RateLimit-Remaining')=='0')
            transient=(isinstance(exc,(TimeoutError,ConnectionError,http.client.IncompleteRead))
                or (isinstance(exc,urllib.error.HTTPError) and (exc.code in (408,429,500,502,503,504) or index_swap and exc.code==404))
                or limited or isinstance(getattr(exc,'reason',None),(TimeoutError,ConnectionError)))
            if not transient:raise
            delay=2**attempt
            if isinstance(exc,urllib.error.HTTPError):
                retry_after=headers.get('Retry-After')
                try:
                    if retry_after is not None:
                        try:server_wait=float(retry_after)
                        except ValueError:server_wait=parsedate_to_datetime(retry_after).timestamp()-time.time()
                        delay=max(delay,server_wait)
                    elif headers.get('X-RateLimit-Remaining')=='0':
                        delay=max(delay,float(headers['X-RateLimit-Reset'])-time.time())
                    elif exc.code==429:delay=max(delay,60)
                except (ValueError,TypeError,KeyError,OverflowError):raise exc
                # A long cooldown cannot fit the command lease. Never shorten
                # it or spin requests before the server allows another attempt.
                if (limited or exc.code in (429,503)) and getattr(_request_local,'root',None) is not None:
                    origins={urllib.parse.urlsplit(exc.filename).netloc}
                    if source_url:origins.add(urllib.parse.urlsplit(source_url).netloc)
                    with _shared.lock:
                        for origin in origins:
                            _shared.cooldown[origin]=max(_shared.cooldown.get(origin,0),time.monotonic()+delay)
                if delay>60:raise
                if exc.fp is not None:exc.close()
            if attempt==2:raise
            delay+=random.uniform(0,min(2,delay))
            end=getattr(_request_local,'deadline',None)
            if end is not None and delay>=end-time.monotonic():
                raise UpdateDeferred(getattr(_request_local,'stage',None) or 'network','server_cooldown',delay) from exc
            report_progress('업데이트 서버 응답 지연·연결 끊김 · 자동 재시도 '+str(attempt+2)+'/3')
            if _cancel_event is not None:
                if _cancel_event.wait(delay):download_checkpoint()
            else:time.sleep(delay)


def _download_uncached(url):
    download_checkpoint()
    if urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('HTTPS download required')
    parsed=urllib.parse.urlsplit(url)
    _request_local.network_stage=('index' if parsed.path.endswith('/DEV_index.json') else
        'archive' if parsed.path.endswith('.zip') else
        'history' if parsed.hostname=='api.github.com' else 'metadata')
    mutable_index=(parsed.hostname=='github.com' and re.fullmatch(
        '/'+re.escape(STABLE_REPOSITORY)+r'/releases/download/v\d+\.\d+\.\d+/DEV_index\.json',parsed.path) is not None)
    def request():
        transport_checkpoint(url)
        request_url=url
        headers={'User-Agent':'BIOL-DKB-Updater/1.0','Accept':'application/vnd.github+json'}
        if mutable_index:
            # A mutable release index can retain a failing/expired redirect in
            # intermediate caches. Refresh only this exact official endpoint,
            # on every bounded retry; all downstream hash checks still apply.
            query=[(k,v) for k,v in urllib.parse.parse_qsl(parsed.query,keep_blank_values=True) if k!='_biol_index']
            query.append(('_biol_index',uuid.uuid4().hex))
            request_url=urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
            headers.update({'Accept':'application/octet-stream','Cache-Control':'no-cache','Pragma':'no-cache'})
        req=urllib.request.Request(request_url,headers=headers)
        with urllib.request.urlopen(req, timeout=network_timeout(),context=update_ssl_context()) as response:
            if urllib.parse.urlparse(response.url).scheme != 'https':
                raise ValueError('Insecure redirect rejected')
            chunks=[]
            while True:
                download_checkpoint()
                chunk=response.read(65536)
                if not chunk:break
                chunks.append(chunk)
            download_checkpoint()
            return b''.join(chunks)
    return retry_download(request,index_swap=mutable_index,source_url=url)


def download(url):
    parsed=urllib.parse.urlsplit(url)
    metadata=parsed.path.endswith('.json') or (parsed.hostname=='api.github.com' and '/releases' in parsed.path)
    if not metadata or getattr(_request_local,'root',None) is None:return _download_uncached(url)
    ttl=3 if parsed.path.endswith('/DEV_index.json') else 120
    key=url;owner=False
    while True:
        download_checkpoint()
        with _shared.lock:
            entry=_shared.cache.get(key)
            if entry and time.monotonic()-entry[0]<ttl:
                _shared.cache.move_to_end(key);return entry[1]
            event=_shared.inflight.get(key)
            if event is None:event=_shared.inflight[key]=threading.Event();owner=True
        if owner:break
        event.wait(min(.1,network_timeout()))
    try:
        data=_download_uncached(url)
        try:json.loads(data);valid_json=True
        except (ValueError,UnicodeError):valid_json=False
        if valid_json and len(data)<=16*1024*1024:
            with _shared.lock:
                _shared.cache[key]=(time.monotonic(),data)
                while len(_shared.cache)>32 or sum(len(v[1]) for v in _shared.cache.values())>16*1024*1024:
                    _shared.cache.popitem(last=False)
        return data
    finally:
        with _shared.lock:
            _shared.inflight.pop(key,None);event.set()


def version_key(value):
    match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?(?:\+[A-Za-z0-9.-]+)?',str(value))
    if not match:raise ValueError('Invalid project release version')
    suffix=match[4]
    pre=(1,) if suffix is None else (0,tuple((0,int(v)) if v.isdigit() else (1,v) for v in suffix.split('.')))
    return (int(match[1]),int(match[2]),int(match[3]),pre)


class RemoteDelta(io.RawIOBase):
    """Read ZIP directory and selected entries; never download an entire Full."""
    def __init__(self,url):
        super().__init__()
        if urllib.parse.urlparse(url).scheme!='https':raise ValueError('HTTPS required')
        self.url=url;self.position=0;self.length=None
        self._range(0,0)

    def _range(self,start,end):
        download_checkpoint()
        _request_local.network_stage='archive_range'
        req=urllib.request.Request(self.url,headers={'User-Agent':'BIOL-DKB-Updater/1.0',
            'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'})
        def request():
            transport_checkpoint(self.url)
            with urllib.request.urlopen(req,timeout=network_timeout(),context=update_ssl_context()) as response:
                if urllib.parse.urlparse(response.url).scheme!='https':raise ValueError('Insecure redirect')
                match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                if response.status!=206 or not match or (int(match[1]),int(match[2]))!=(start,end):
                    raise RuntimeError('Delta server does not support selective download; Full download refused')
                length=int(match[3])
                if self.length is not None and self.length!=length:raise ValueError('Remote delta changed')
                data=response.read(end-start+2)
                download_checkpoint()
                if len(data)<end-start+1:raise http.client.IncompleteRead(data,end-start+1-len(data))
                if len(data)>end-start+1:raise ValueError('Invalid delta range length')
                self.length=length
                return data
        return retry_download(request,source_url=self.url)

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
    meta_data=(json.dumps(release['catalog_metadata']).encode() if 'catalog_metadata' in release else download(assets['release.json']));meta=json.loads(meta_data)
    assets=public_assets(release,meta)
    if (meta.get('contract'),meta.get('project'),meta.get('repository'),meta.get('tag'))!=(
            'biol-project-update-v1',cfg['project'],cfg['repository'],release['tag_name']):
        raise ValueError('Release identity mismatch')
    if version_key(meta['version'])!=version_key(release['tag_name']):raise ValueError('Release version mismatch')
    data=download(assets['package-manifest.json'])
    verify_dev_documents(release,cfg,meta_data,data,meta)
    if hashlib.sha256(data).hexdigest()!=meta['package_manifest_sha256']:raise ValueError('Release manifest SHA256 mismatch')
    manifest=json.loads(data)
    if manifest['project']!=cfg['project'] or manifest['runtime_version']!=meta['runtime_version']:
        raise ValueError('Release manifest identity mismatch')
    return meta,manifest,assets


STABLE_REPOSITORY='stupid42sr-lang/DKB_Auto_Updater'
# Existing DEV anchor verified as published/non-prerelease; publication still
# checks it through the authenticated publisher. Avoid one anonymous API call
# per PC to recheck this immutable, explicitly pinned release identity.
VERIFIED_DEV_ANCHORS={'v0.6.12':413494923}


def stable_history(cfg,meta=None,release=None):
    if meta and meta.get('history_catalog'):
        record=meta['history_catalog'];name=record.get('name')
        if name!='DKB_history.json' or not re.fullmatch('[0-9a-f]{64}',str(record.get('sha256',''))):
            raise ValueError('Invalid stable history catalog identity')
        tag=meta['tag']
        url='https://github.com/'+cfg['repository']+'/releases/download/'+tag+'/'+name
        data=download(url)
        if hashlib.sha256(data).hexdigest()!=record['sha256']:raise ValueError('History catalog SHA256 mismatch')
        catalog=json.loads(data)
        if (catalog.get('contract')!='dkb-stable-history-v1' or catalog.get('repository')!=cfg['repository']
                or not isinstance(catalog.get('releases'),list) or len(catalog['releases'])>2000):
            raise ValueError('Invalid stable history catalog')
        result=[];seen=set()
        for item in catalog['releases']:
            tag=item.get('tag_name','')
            if (not re.fullmatch(r'v?\d+\.\d+\.\d+',tag) or tag in seen
                    or item.get('draft') is not False or item.get('prerelease') is not False):
                raise ValueError('Invalid history release channel')
            seen.add(tag);base='https://github.com/'+cfg['repository']+'/releases/download/'+tag+'/'
            for asset in item['assets']:
                n=asset['name']
                if not isinstance(n,str) or Path(n).name!=n or '/' in n or '\\' in n or asset['browser_download_url']!=base+urllib.parse.quote(n,safe=''):
                    raise ValueError('Invalid history asset URL')
            result.append(item)
        if release is not None and release['tag_name'] not in seen:result.append(release)
        return result
    logging.getLogger('project_gui.DKB').info('업데이트 분석 · 단계=legacy_history_api · 공개본 카탈로그 없음',extra={'gui_suppress':True})
    return _legacy_stable_history(cfg)


def _legacy_stable_history(cfg):
    releases=json.loads(download('https://api.github.com/repos/'+cfg['repository']+'/releases?per_page=100'))
    return [r for r in releases if not r['draft'] and not r['prerelease']
            and re.fullmatch(r'v?\d+\.\d+\.\d+',r['tag_name'])]


def stable_latest(cfg):
    _request_local.network_stage='discovery'
    base='https://github.com/'+cfg['repository']+'/releases/'
    req=urllib.request.Request(base+'latest',method='HEAD',headers={'User-Agent':'BIOL-DKB-Updater/1.0'})
    def request():
        transport_checkpoint(req.full_url)
        with urllib.request.urlopen(req,timeout=network_timeout(),context=update_ssl_context()) as response:
            return urllib.parse.urlsplit(response.url)
    try:final=retry_download(request,source_url=req.full_url)
    except urllib.error.HTTPError as exc:
        if exc.code not in (404,405):raise
        # Preserve old/no-latest endpoint compatibility without using REST for
        # the ordinary public release check on every PC.
        return stable_history(cfg)
    prefix='/'+cfg['repository']+'/releases/tag/'
    if final.scheme!='https' or final.netloc!='github.com' or not final.path.startswith(prefix):
        raise ValueError('Invalid STABLE release redirect')
    tag=final.path[len(prefix):]
    if not re.fullmatch(r'v?\d+\.\d+\.\d+',tag):raise ValueError('Invalid STABLE release tag')
    assets_base=base+'download/'+tag+'/'
    return [dict(tag_name=tag,public_asset_base=assets_base,assets=[
        dict(name=n,browser_download_url=assets_base+n) for n in ('release.json','package-manifest.json')])]


def public_assets(release,meta):
    assets={a['name']:a['browser_download_url'] for a in release['assets']}
    if release.get('public_asset_base'):
        for role in ('delta','full'):
            if not meta.get(role):continue
            name=meta[role]['name']
            if not isinstance(name,str) or not name or Path(name).name!=name or '/' in name or '\\' in name:
                raise ValueError('Invalid public release asset name')
            assets[name]=release['public_asset_base']+urllib.parse.quote(name,safe='')
    return assets


def dev_releases(cfg):
    anchor=cfg.get('dev_anchor_tag','')
    if not re.fullmatch(r'v\d+\.\d+\.\d+',anchor):raise ValueError('Invalid DEV anchor')
    base='https://github.com/'+STABLE_REPOSITORY+'/releases/download/'+anchor+'/'
    if anchor not in VERIFIED_DEV_ANCHORS:
        actual=json.loads(download('https://api.github.com/repos/'+STABLE_REPOSITORY+'/releases/tags/'+anchor))
        if actual['tag_name']!=anchor or actual['draft'] or actual['prerelease']:
            raise ValueError('DEV anchor is not an existing published STABLE release')
    index=json.loads(download(base+'DEV_index.json'))
    if (index.get('contract')!='dkb-dev-assets-v1' or index.get('repository')!=STABLE_REPOSITORY
            or index.get('anchor_tag')!=anchor or not isinstance(index.get('builds'),list)
            or not 1<=len(index['builds'])<=1000):
        raise ValueError('Invalid DEV index')
    leaves={'release.json':'release.json','package-manifest.json':'package-manifest.json',
            'update.zip':'Update.zip','full.zip':'Auto.zip','SHA256SUMS.txt':'SHA256SUMS.txt'}
    result=[];seen=set()
    for item in index['builds']:
        version=item['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+-dev\.[1-9]\d*',version) or version in seen:
            raise ValueError('Invalid/duplicate DEV version')
        seen.add(version);records=item['assets']
        if not {'release.json','package-manifest.json','update.zip','SHA256SUMS.txt'}<=records.keys():
            raise ValueError('Incomplete DEV asset index')
        assets=[]
        for role,entry in records.items():
            if (role not in leaves or entry['name']!='DEV_'+version+'_'+leaves[role]
                    or not re.fullmatch(r'[0-9a-f]{64}',entry['sha256'])):
                raise ValueError('Non-DEV asset or invalid DEV hash')
            url=base+urllib.parse.quote(entry['name'],safe='')
            assets.append(dict(name=entry['name'],browser_download_url=url))
            if role in ('release.json','package-manifest.json'):
                assets.append(dict(name=role,browser_download_url=url))
        result.append(dict(tag_name='v'+version,assets=assets,dev_record=item))
    if index.get('latest')!=max(seen,key=version_key):raise ValueError('DEV latest version mismatch')
    return result


def verify_dev_documents(release,cfg,meta_data,manifest_data,meta):
    if cfg.get('channel')!='DEV':return
    item=release['dev_record'];records=item['assets']
    if (hashlib.sha256(meta_data).hexdigest()!=records['release.json']['sha256']
            or hashlib.sha256(manifest_data).hexdigest()!=records['package-manifest.json']['sha256']
            or meta.get('update_channel')!='DEV' or meta.get('version')!=item['version']
            or meta.get('dev_anchor_tag')!=cfg['dev_anchor_tag']
            or meta.get('dev_assets')!={role:entry['name'] for role,entry in records.items()}
            or meta['delta']['name']!=records['update.zip']['name']
            or meta['delta']['sha256']!=records['update.zip']['sha256']):
        raise ValueError('DEV index/manifest/release identity or SHA256 mismatch')
    if bool(meta.get('full'))!=('full.zip' in records):raise ValueError('DEV Full identity mismatch')
    if 'full.zip' in records and (meta['full']['name']!=records['full.zip']['name']
                                or meta['full']['sha256']!=records['full.zip']['sha256']):
        raise ValueError('DEV Full SHA256 identity mismatch')


def update_source(install):
    cfg=json.loads(Path(__file__).with_name('update-config.json').read_text(encoding='utf-8'))
    if (cfg.get('contract')!='biol-project-update-source-v1'
            or cfg.get('project')!='DKB' or cfg.get('repository')!=STABLE_REPOSITORY):
        raise ValueError('Unexpected update source')
    # Ignore legacy allow_prerelease=true: DEV is selected only through its index.
    cfg=dict(cfg,repository=STABLE_REPOSITORY,allow_prerelease=False,channel='STABLE')
    root=managed_root(install)
    app=root/'DKB_Auto' if (root/BUNDLE_MANIFEST).is_file() else Path(install)
    try:
        choice=json.loads((app/'settings/update-channel.json').read_text(encoding='utf-8-sig'))
        if choice.get('contract')!='dkb-update-channel-v1' or choice.get('channel')!='DEV':return cfg
        if not re.fullmatch(r'v\d+\.\d+\.\d+',cfg.get('dev_anchor_tag','')):return cfg
        return dict(cfg,channel='DEV')
    except (OSError,ValueError,TypeError,AttributeError):pass
    return cfg


def inspect_release(install):
    with update_request(install,'check',120):return _inspect_release(install)


def _inspect_release(install):
    """Read-only structured check; safe while the app holds its instance mutex."""
    cfg = update_source(install)
    # Small independent jitter spreads simultaneous fleet checks without a
    # shared service, lock, token, timer or background worker.
    delay=random.uniform(0,2)
    if _cancel_event is not None:
        if _cancel_event.wait(delay):download_checkpoint()
    else:time.sleep(delay)
    old = installed_manifest(install)
    if old['project'] != cfg['project']:
        raise ValueError('Install project mismatch')
    if cfg['channel']=='DEV':
        choices=dev_releases(cfg)
    else:
        choices=stable_latest(cfg)
    if not choices:
        raise ValueError('No permitted published release')
    release = max(choices,key=lambda r:version_key(r['tag_name']))
    assets = {a['name']: a['browser_download_url'] for a in release['assets']}
    meta_data=download(assets['release.json']);meta=json.loads(meta_data)
    assets=public_assets(release,meta)
    if meta.get('contract') != 'biol-project-update-v1' or meta['project'] != cfg['project'] or meta['repository'] != cfg['repository'] or meta['tag'] != release['tag_name']:
        raise ValueError('Release identity mismatch')
    if version_key(meta['version'])!=version_key(release['tag_name']):
        raise ValueError('Release version mismatch')
    manifest_data = download(assets['package-manifest.json'])
    verify_dev_documents(release,cfg,meta_data,manifest_data,meta)
    if hashlib.sha256(manifest_data).hexdigest() != meta['package_manifest_sha256']:
        raise ValueError('Release manifest SHA256 mismatch')
    new = json.loads(manifest_data)
    if new['project']!=cfg['project'] or new['runtime_version']!=meta['runtime_version']:
        raise ValueError('Release manifest identity mismatch')
    bundle=old.get('contract')==BUNDLE_CONTRACT
    app=managed_root(install)/old['app_dir'] if bundle else Path(install)
    current=json.loads((app/'runtime/products'/(old['project']+'.json')).read_text(encoding='utf-8'))['app_version']
    result=dict(version=meta['version'],current_version=current,metadata=meta,assets=assets,
                update_source=cfg, release_source=release,
                manifest_sha256=meta['package_manifest_sha256'], target_manifest=new)
    if version_key(meta['version'])<=version_key(current):return dict(result,state='current')
    result['repair_candidates']=[dict(tag_name=r['tag_name'],assets=r['assets'],
        **({'dev_record':r['dev_record']} if cfg['channel']=='DEV' else {}))
        for r in choices if version_key(r['tag_name'])<=version_key(meta['version'])]
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
            return dict(result,state='available',direct_delta=True,
                release_candidates=[dict(tag_name=r['tag_name'],assets=r['assets'],
                    **({'dev_record':r['dev_record']} if cfg['channel']=='DEV' else {})) for r in choices
                    if version_key(r['tag_name'])<=version_key(meta['version'])
                    and (cfg['channel']=='DEV' or '-dev.' in current
                         or version_key(current)<version_key(r['tag_name']))])
        return dict(result,state='baseline_mismatch')
    return dict(result,state='available')


def prepare_update(install):
    with update_request(install,'prepare',1200):
        prepared=_prepare_update(install)
        try:
            verify_prepared_update(install,prepared)
            return prepared
        except BaseException:
            shutil.rmtree(prepared['download_dir'],ignore_errors=True);raise


def verify_prepared_update(install,prepared,*,final_check=False):
    """Read-only preflight. Backup rotation/staging belong exclusively to apply()."""
    root=managed_root(install);old=installed_manifest(root)
    download_checkpoint()
    if digest(root/manifest_name(root))!=prepared['installed_manifest_sha256']:
        raise ValueError('Installed baseline changed after download')
    if update_source(install)!=prepared['update_source']:raise ValueError('Update channel changed')
    # apply() immediately repeats ZIP/file verification under app_closed. At
    # this final gate check only identity/baseline, avoiding a third full scan.
    if final_check:return True
    archive=Path(prepared['archive'])
    if digest(archive)!=prepared['sha256']:raise ValueError('ZIP SHA256 mismatch')
    marker=BUNDLE_MANIFEST if old.get('contract')==BUNDLE_CONTRACT else 'package-manifest.json'
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        if len({n.casefold() for n in names})!=len(names):raise ValueError('Duplicate ZIP paths')
        new=json.loads(z.read(marker))
        if new.get('project')!=old['project']:raise ValueError('Project mismatch')
        if old.get('contract')==BUNDLE_CONTRACT and (new.get('contract')!=BUNDLE_CONTRACT or new.get('app_dir')!='DKB_Auto'):
            raise ValueError('Invalid bundle update')
        if any(user_file(n) for n in (*new['files'],*new.get('removed',[]))):raise ValueError('User files cannot be updated or deleted')
        removed={n for n in set(old['files'])-set(new['files']) if not user_file(n)}
        if removed!=set(new.get('removed',[])):raise ValueError('Deletion manifest mismatch')
        for name in names:
            download_checkpoint();safe(root,name)
            if name==marker:continue
            data=z.read(name)
            if name=='license.dat' and len(data)<=512 and json.loads(data.decode('utf-8-sig'))=={'license_key':''}:continue
            if name not in new['files'] or hashlib.sha256(data).hexdigest()!=new['files'][name]:
                raise ValueError('Delta file SHA256 mismatch: '+name)
        for name,value in new['files'].items():
            download_checkpoint();safe(root,name)
            if name not in names and (old['files'].get(name)!=value or not safe(root,name).is_file() or digest(safe(root,name))!=value):
                raise ValueError('Verified replacement missing: '+name)
    return True


def _prepare_update(install):
    result=inspect_release(install)
    if result['state']!='available':raise ValueError('Project delta is not applicable: '+result['state'])
    root=managed_root(install)
    # Never archive/prune a backup, copy user files or stage the installation
    # while its worker is running. apply() repeats these checks after shutdown.
    backup,stage,lock=workspace(root,create=False)
    legacy=tuple(root.with_name(root.name+s) for s in ('.update-stage','.update-lock'))
    if (stage.exists() or lock.exists() or any((backup.parent/n).exists() for n in ('transaction.json','transaction.tmp'))
            or any(p.exists() for p in legacy)):
        raise RuntimeError('Previous update pending; inspect before applying')
    unbacked=backup.parent/UNBACKED_MARKER
    if unbacked.exists() and json.loads(unbacked.read_text(encoding='utf-8')).get('state')!='complete':
        raise RuntimeError('Previous update interrupted; inspect before applying')
    result=dict(result,installed_manifest_sha256=digest(root/manifest_name(root)))
    meta=result['metadata']
    old=installed_manifest(root)
    damaged=[]
    report_progress('설치 파일 복구 필요 여부 확인 중...')
    for name,value in old['files'].items():
        if user_file(name):continue
        path=safe(root,name)
        if not path.is_file() or digest(path)!=value:damaged.append(name)
    if damaged and old.get('contract')!=BUNDLE_CONTRACT:
        raise ValueError('Damaged legacy installation requires verified bundle migration')
    if damaged:
        report_progress('설치 파일 복구 준비 · '+str(len(damaged))+'개')
    if result.get('direct_delta') or damaged:
        result=dict(result, repair_files=damaged)
        if result['update_source']['channel']=='STABLE' and result.get('release_source',{}).get('public_asset_base'):
            # Historical sources are needed only for actual old/damaged
            # installs, never for the Manager's read-only batch check.
            choices=stable_history(result['update_source'],result['metadata'],result['release_source'])
            choices=[r for r in choices if version_key(r['tag_name'])<=version_key(result['version'])]
            if choices:
                result['release_candidates']=choices;result['repair_candidates']=choices
        # Latest verified release is also a file source when the baseline matches.
        if damaged and result.get('repair_candidates'):
            result['release_candidates']=result['repair_candidates']
        result.setdefault('release_candidates', [])
        if not result['release_candidates']:
            result['release_candidates']=[result['release_source']]
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
    needed={n:h for n,h in new['files'].items()
            if old['files'].get(n)!=h or n in result.get('repair_files',())}
    new['removed']=sorted(set(old['files'])-set(new['files']))
    new['update_base_files_sha256']=files_baseline(old['files'])
    if any(user_file(n) for n in (*new['files'],*new['removed'])):
        raise ValueError('User files cannot be updated or deleted')
    folder=Path(tempfile.mkdtemp(prefix='biol-project-delta-'));archive=folder/'update.zip'
    cfg=update_source(install)
    if cfg!=result.get('update_source'):raise ValueError('Update channel changed; check again')
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
                remaining=matching.intersection(needed)
                if remaining and cm.get('full'):
                    # A dependency may exist only in an older Full. Use it only
                    # when its authenticated source manifest has the exact
                    # latest target hash; never downgrade a dependency.
                    with RemoteDelta(assets[cm['full']['name']]) as remote,zipfile.ZipFile(remote) as source:
                        names=source.namelist()
                        if len({n.casefold() for n in names})!=len(names):raise ValueError('Duplicate ZIP paths')
                        if hashlib.sha256(source.read(BUNDLE_MANIFEST)).hexdigest()!=cm['package_manifest_sha256']:
                            raise ValueError('Repair manifest SHA256 mismatch')
                        for name in sorted(remaining.intersection(names)):
                            safe(folder,name)
                            report_progress('복구 파일 다운로드: '+name)
                            data=source.read(name)
                            if hashlib.sha256(data).hexdigest()!=needed[name]:raise ValueError('Repair file SHA256 mismatch: '+name)
                            output.writestr(name,data);del needed[name]
            if needed:raise ValueError('Verified repair files unavailable; installation unchanged: '+', '.join(sorted(needed)))
            output.writestr(BUNDLE_MANIFEST,json.dumps(new,ensure_ascii=False,indent=2).encode('utf-8'))
        prepared={k:v for k,v in result.items() if k not in ('target_manifest','release_candidates','repair_candidates','release_source')}
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
    if a.recover and (Path(a.install)/'.update').exists():
        print(recover_interrupted(a.install));sys.exit(0)
    with app_closed(a.install):
        if a.recover: print(recover(a.install))
        elif a.finish: print(finish(a.install))
        elif a.github: print(github_update(a.install, a.check))
        else: print(apply(a.install, a.zip, a.sha256))
