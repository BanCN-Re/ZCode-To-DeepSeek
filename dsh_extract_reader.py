#!/usr/bin/env python3
"""Assemble DeepSeek Harness's own session reader so generated logs can be checked.

The strongest available verification of an exported log is the harness reading it
back and rebuilding the transcript a model would receive. That code lives in the
packaged `app.asar`, not on `PATH`, so this extracts the reader's module closure
into a real `node_modules` tree and leaves `dsh_check.mjs` able to require it.

Nothing here is machine-specific: the install is located from the usual places
(or `--asar`), and the asar index is rebuilt on the fly.

    python dsh_extract_reader.py [--out DIR] [--asar PATH] [--index PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys

# Where the harness is normally installed, newest-looking first.
ASAR_CANDIDATES = [
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'DeepSeek Harness',
                 'resources', 'app.asar'),
    '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar',
    os.path.join(os.environ.get('PROGRAMFILES', ''), 'DeepSeek Harness',
                 'resources', 'app.asar'),
    os.path.join('/opt', 'DeepSeek Harness', 'resources', 'app.asar'),
    os.path.join('/usr', 'lib', 'deepseek-harness', 'resources', 'app.asar'),
]

ENTRY = '/dsh/node_modules/@deepseek-ai/dsh-session-format-catalog/lib/index.js'
IMPORT = re.compile(r"""(?:from|import|require)\s*\(?\s*['"]([^'"]+)['"]""")


def find_asar(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise SystemExit('app.asar not found: %s' % explicit)
        return explicit
    for path in ASAR_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise SystemExit('could not find DeepSeek Harness app.asar; pass --asar PATH')


def read_index(asar):
    """Parse the asar header into `{path: offset}` plus the data base offset."""
    with open(asar, 'rb') as f:
        head = f.read(16)
        if len(head) < 16:
            raise SystemExit('not an asar archive: %s' % asar)
        # [uint32 size-of-size][uint32 headerSize][...pickle padding][json]
        sizes = struct.unpack('<IIII', head)
        header_size = sizes[1]
        f.seek(16)
        blob = f.read(header_size).decode('utf-8', 'replace')
    decoder = json.JSONDecoder()
    header, end = decoder.raw_decode(blob)
    base = 16 + end
    while base % 4:
        base += 1

    out = {}

    def walk(node, prefix):
        for name, v in node.get('files', {}).items():
            p = prefix + '/' + name
            if 'files' in v:
                walk(v, p)
            elif 'offset' in v:
                out[p] = (base + int(v['offset']), int(v['size']))
    walk(header, '')
    return out


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default=os.path.join(here, '_dshrt'),
                    help='where to build the reader tree (default: ./_dshrt)')
    ap.add_argument('--asar', help='path to app.asar (auto-detected by default)')
    ap.add_argument('--index', help='cached asar index json to reuse')
    args = ap.parse_args(argv)

    asar = find_asar(args.asar)
    index_path = args.index or os.path.join(here, 'dsh_asar_entries.json')

    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                raw = json.load(f)
            # accept both the flat {path: [off, size]} form and the legacy list
            if isinstance(raw, dict):
                disk = {k: tuple(v) for k, v in raw.items()}
            else:
                disk = {p: (o, s) for p, o, s in raw if o is not None}
            print('index   : cached (%d files)' % len(disk))
        except Exception:
            disk = None
    else:
        disk = None

    if not disk:
        print('index   : building from', asar)
        disk = read_index(asar)
        with open(index_path, 'w') as f:
            json.dump({k: list(v) for k, v in disk.items()}, f)
        print('index   : wrote', index_path)

    if ENTRY not in disk:
        raise SystemExit('this app.asar does not contain the session reader (%s)' % ENTRY)

    present = set(disk)

    def read(path):
        off, size = disk[path]
        if size > 8_000_000:
            return ''
        with open(asar, 'rb') as f:
            f.seek(off)
            return f.read(size).decode('utf-8', 'replace')

    def package_root(path):
        i = path.rfind('/node_modules/')
        if i < 0:
            return None
        rest = path[i + len('/node_modules/'):].split('/')
        n = 2 if rest[0].startswith('@') else 1
        return path[:i] + '/node_modules/' + '/'.join(rest[:n])

    def spec_package(spec):
        if spec.startswith('@'):
            parts = spec.split('/')
            return '/'.join(parts[:2]) if len(parts) >= 2 else spec
        return spec.split('/')[0]

    def package_files(root):
        return [p for p in present if p.startswith(root + '/')]

    def js_entrypoints(root):
        out = []
        pj = root + '/package.json'
        if pj in present:
            try:
                man = json.loads(read(pj))
            except Exception:
                man = {}
            for key in ('main', 'module'):
                v = man.get(key)
                if isinstance(v, str):
                    out.append(root + '/' + v.lstrip('./'))
            exp = man.get('exports')
            if isinstance(exp, dict):
                for v in exp.values():
                    if isinstance(v, str):
                        out.append(root + '/' + v.lstrip('./'))
                    elif isinstance(v, dict):
                        for vv in v.values():
                            if isinstance(vv, str):
                                out.append(root + '/' + vv.lstrip('./'))
        out += [root + '/lib/index.js', root + '/index.js']
        return out

    # Fixed point: scan everything kept, pulling in whatever it imports.
    keep = {ENTRY}
    packages = set()
    while True:
        before = len(keep)
        scanned = set()
        for path in sorted(keep):
            if path in scanned or path not in present:
                continue
            scanned.add(path)
            root = package_root(path)
            if root:
                packages.add(root)
            if not path.endswith('.js'):
                continue
            try:
                text = read(path)
            except Exception:
                continue
            for spec in IMPORT.findall(text):
                if spec.startswith('.'):
                    base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
                    base = base.replace('\\', '/')
                    for cand in (base, base + '.js', base + '/index.js'):
                        if cand in present:
                            keep.add(cand)
                            break
                else:
                    root2 = '/dsh/node_modules/' + spec_package(spec)
                    if root2 + '/package.json' in present:
                        packages.add(root2)
                        keep.update(package_files(root2))
                        keep.update(e for e in js_entrypoints(root2) if e in present)
                        sub = spec[len(spec_package(spec)):].lstrip('/')
                        if sub:
                            for cand in (root2 + '/' + sub, root2 + '/' + sub + '.js',
                                         root2 + '/' + sub + '/index.js'):
                                if cand in present:
                                    keep.add(cand)
        if len(keep) == before:
            break

    # Manifests are required for bare-specifier resolution.
    for root in list(packages):
        keep.update(p for p in package_files(root) if p.endswith('package.json'))
    keep = {p for p in keep if p in present}

    written = 0
    for path in sorted(keep):
        off, size = disk[path]
        dest = os.path.join(args.out, path.lstrip('/').replace('/', os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(asar, 'rb') as f:
            f.seek(off)
            blob = f.read(size)
        with open(dest, 'wb') as out:
            out.write(blob)
        written += 1

    print('packages: %d | files: %d' % (len(packages), written))
    print('reader  :', args.out)
    print('check   : node dsh_check.mjs <logfile> --full')
    return 0


if __name__ == '__main__':
    sys.exit(main())
