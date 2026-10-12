"""Download Blender separately (GPL) and optional Poly Haven HDRI (CC0). No sudo."""
import argparse
import hashlib
import json
import platform
import re
import tarfile
import urllib.request
from pathlib import Path


def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'agent-blender-studio/1.0 (Korra; asset setup)'})
    with urllib.request.urlopen(request,timeout=90) as response: return response.read()


def blender(dest,version):
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','AMD64'):
        raise SystemExit('Portable installer supports Linux x86_64. Install Blender from https://www.blender.org/download/ for your OS, then pass its executable explicitly.')
    if not re.fullmatch(r'\d+\.\d+\.\d+',version): raise ValueError('Expected Blender x.y.z')
    base='https://download.blender.org/release/Blender'+'.'.join(version.split('.')[:2])+'/'
    name=f'blender-{version}-linux-x64.tar.xz'
    sums=fetch(base+f'blender-{version}.sha256').decode()
    expected=next((line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip('*')==name),None)
    if not expected: raise ValueError('Archive missing from official SHA256 manifest')
    target=dest/f'blender-{version}-linux-x64'
    if target.exists(): raise SystemExit(f'Already exists: {target}; use it or select another --dir')
    dest.mkdir(parents=True,exist_ok=True)
    archive=dest/name
    # Download to a bounded temporary archive; nothing executes during extraction.
    try:
        data=fetch(base+name)
        if hashlib.sha256(data).hexdigest()!=expected: raise ValueError('Blender SHA256 mismatch')
        archive.write_bytes(data)
        with tarfile.open(archive,'r:xz') as tf:
            for member in tf.getmembers():
                namepath=Path(member.name)
                if namepath.is_absolute() or '..' in namepath.parts or namepath.parts[0]!=target.name:
                    raise ValueError('Unexpected archive path')
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()): raise ValueError('Unexpected archive entry')
                if member.issym() or member.islnk():
                    resolved=(dest/member.name).parent.joinpath(member.linkname).resolve()
                    if not resolved.is_relative_to(target.resolve()): raise ValueError('Escaping archive link')
            tf.extractall(dest,filter='data')
        print(target/'blender')
    finally: archive.unlink(missing_ok=True)


def hdri(dest,asset):
    if not re.fullmatch(r'[a-z0-9_]+',asset): raise ValueError('Invalid Poly Haven asset id')
    meta=json.loads(fetch('https://api.polyhaven.com/files/'+asset))
    file=meta['hdri']['1k']['hdr']; url=file['url']
    if not url.startswith('https://dl.polyhaven.org/'): raise ValueError('Unexpected Poly Haven download host')
    data=fetch(url)
    if file.get('size') and len(data)!=file['size']: raise ValueError('HDRI size mismatch')
    if file.get('md5') and hashlib.md5(data).hexdigest()!=file['md5']: raise ValueError('HDRI checksum mismatch')
    dest.mkdir(parents=True,exist_ok=True); out=dest/(asset+'.hdr')
    if out.exists(): raise SystemExit(f'Already exists: {out}')
    out.write_bytes(data)
    (dest/(asset+'.license.json')).write_text(json.dumps({'asset':asset,'url':url,'license':'CC0-1.0','source':'https://polyhaven.com/a/'+asset,'sha256':hashlib.sha256(data).hexdigest()},indent=2))
    print(out)


if __name__=='__main__':
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('blender'); b.add_argument('--version',default='5.2.2'); b.add_argument('--dir',type=Path,default=Path.home()/'.local/share/agent-blender-studio')
    h=sub.add_parser('hdri'); h.add_argument('--asset',default='studio_small_09'); h.add_argument('--dir',type=Path,required=True)
    a=p.parse_args()
    if a.command=='blender': blender(a.dir.expanduser(),a.version)
    else: hdri(a.dir.expanduser(),a.asset)
