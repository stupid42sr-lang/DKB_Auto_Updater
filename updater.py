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
from contextlib import contextmanager


def user_file(name):
    p = Path(name)
    return p.name.casefold() in {'license.dat', 'user_values.json'} or any(
        part.casefold() in {'settings', 'logs', 'data', 'backups', 'audit', '__pycache__'} for part in p.parts)


@contextmanager
def app_closed(install):
    """Own the same named object as the existing runtime while swapping files."""
    root = Path(install)
    if not root.exists():
        root = root.with_name(root.name + '.update-backup')
    old = json.loads((root / 'package-manifest.json').read_text(encoding='utf-8'))
    product = json.loads((root / 'runtime/products' / (old['project'] + '.json')).read_text(encoding='utf-8'))
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
    if p.is_absolute() or ':' in name or target == root or not target.is_relative_to(root):
        raise ValueError('Unsafe package path: ' + name)
    return target


def apply(install, archive, expected_sha256):
    install, archive = Path(install).resolve(), Path(archive).resolve()
    backup = install.with_name(install.name + '.update-backup')
    stage = install.with_name(install.name + '.update-stage')
    lock = install.with_name(install.name + '.update-lock')
    if digest(archive) != expected_sha256:
        raise ValueError('ZIP SHA256 mismatch; installation unchanged')
    if backup.exists() or stage.exists():
        raise RuntimeError('Previous update pending; recover before retry')
    with lock.open('x'):
        pass
    swapped = False
    try:
        old = json.loads((install / 'package-manifest.json').read_text(encoding='utf-8'))
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if len({n.casefold() for n in names}) != len(names): raise ValueError('Duplicate ZIP paths')
            new = json.loads(z.read('package-manifest.json'))
            if old['project'] != new['project']: raise ValueError('Project mismatch')
            if any(user_file(n) for n in new['files']) or any(user_file(n) for n in new.get('removed', [])):
                raise ValueError('User files cannot be updated or deleted')
            if old['runtime_version'] != new['runtime_version']:
                raise ValueError('Runtime version change requires separate installation/release')
            for n in names:
                safe(stage, n)
                if n != 'package-manifest.json' and n not in new['files']: raise ValueError('Unlisted ZIP file')
            removed = {n for n in set(old['files']) - set(new['files']) if not user_file(n)}
            if removed != set(new.get('removed', [])): raise ValueError('Deletion manifest mismatch')
            for n, h in old['files'].items():
                if user_file(n): continue
                p = safe(install, n)
                if not p.is_file() or digest(p) != h: raise ValueError('Local managed file changed: ' + n)
            # Reject reparse links; staging must never write outside this installation.
            if any(p.is_symlink() or (getattr(p.lstat(), 'st_file_attributes', 0) & 1024)
                   for p in install.rglob('*')): raise ValueError('Linked installation unsupported')
            shutil.copytree(install, stage)
            for n in removed: safe(stage, n).unlink()
            for n in names:
                target = safe(stage, n)
                if n not in old['files'] and n != 'package-manifest.json' and target.exists():
                    raise ValueError('Unmanaged file collision: ' + n)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(n))
            for n, h in new['files'].items():
                p = safe(stage, n)
                if not p.is_file() or digest(p) != h: raise ValueError('File SHA256 mismatch: ' + n)
        os.replace(install, backup)
        try:
            os.replace(stage, install)
            swapped = True
        except BaseException:
            os.replace(backup, install)
            raise
        # Keep the previous version until explicit finish/recover.
        return 'Applied and verified; previous version retained at ' + str(backup)
    finally:
        if not swapped and stage.exists(): shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


def recover(install):
    install = Path(install).resolve()
    backup = install.with_name(install.name + '.update-backup')
    stage = install.with_name(install.name + '.update-stage')
    lock = install.with_name(install.name + '.update-lock')
    # Explicit recovery is only for a stopped updater/app, including interrupted swaps.
    if not backup.is_dir(): raise ValueError('No backup; original installation must be checked manually')
    if stage.exists(): raise ValueError('Stage remains; preserve it and inspect before recovery')
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
    install = Path(install).resolve()
    backup = install.with_name(install.name + '.update-backup')
    if install.with_name(install.name + '.update-lock').exists():
        raise RuntimeError('Updater is running or interrupted; inspect before finishing')
    manifest = json.loads((install / 'package-manifest.json').read_text(encoding='utf-8'))
    for n, h in manifest['files'].items():
        if user_file(n): continue
        if digest(safe(install, n)) != h: raise ValueError('Installed file SHA256 mismatch: ' + n)
    if backup.exists(): shutil.rmtree(backup)
    return 'Update accepted; backup removed'


def download(url):
    if urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('HTTPS download required')
    req = urllib.request.Request(url, headers={'User-Agent': 'BIOL-DKB-Updater/1.0', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        if urllib.parse.urlparse(response.url).scheme != 'https':
            raise ValueError('Insecure redirect rejected')
        return response.read()


def github_update(install, check=False):
    cfg = json.loads(Path(__file__).with_name('update-config.json').read_text(encoding='utf-8'))
    if cfg.get('contract') != 'biol-project-update-source-v1' or cfg.get('repository') != 'stupid42sr-lang/DKB_Auto_Updater':
        raise ValueError('Unexpected update source')
    old = json.loads((Path(install) / 'package-manifest.json').read_text(encoding='utf-8'))
    if old['project'] != cfg['project']:
        raise ValueError('Install project mismatch')
    releases = json.loads(download('https://api.github.com/repos/' + cfg['repository'] + '/releases?per_page=100'))
    choices = [r for r in releases if not r['draft'] and (cfg.get('allow_prerelease') or not r['prerelease'])]
    if not choices:
        raise ValueError('No permitted published release')
    release = choices[0]
    assets = {a['name']: a['browser_download_url'] for a in release['assets']}
    meta = json.loads(download(assets['release.json']))
    if meta.get('contract') != 'biol-project-update-v1' or meta['project'] != cfg['project'] or meta['repository'] != cfg['repository'] or meta['tag'] != release['tag_name']:
        raise ValueError('Release identity mismatch')
    if old['runtime_version'] != meta['runtime_version']:
        raise ValueError('Install shared Runtime separately before this project release')
    manifest_data = download(assets['package-manifest.json'])
    if hashlib.sha256(manifest_data).hexdigest() != meta['package_manifest_sha256']:
        raise ValueError('Release manifest SHA256 mismatch')
    new = json.loads(manifest_data)
    if new['files'] == old['files']:
        return 'Already current: ' + meta['version']
    baseline = {p: h for p, h in old['files'].items() if not user_file(p)}
    actual = hashlib.sha256(json.dumps(baseline, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if actual != meta['base_files_sha256']:
        raise ValueError('Delta baseline mismatch; installation unchanged. Use a matching release chain.')
    if check:
        return 'Matching delta available: ' + meta['tag'] + ' (' + str(meta['delta']['changed_files']) + ' changed files)'
    data = download(assets[meta['delta']['name']])
    with tempfile.TemporaryDirectory(prefix='dkb-update-download-') as folder:
        archive = Path(folder) / 'update.zip'
        archive.write_bytes(data)
        return apply(install, archive, meta['delta']['sha256'])


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
