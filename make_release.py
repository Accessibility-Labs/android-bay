"""Package only reviewed manifest entries; never recursively include a working archive."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import zipfile

BASE = Path(__file__).resolve().parent
MANIFEST = BASE / 'PUBLIC_FILES.json'
FORBIDDEN = {'state', 'demo-state', 'recoveries', 'demo recoveries', '.git', '.keys',
             '.build-tools', 'originals', 'viewer-data', 'device-context', 'deleted-items',
             'location-data', 'exports', '__pycache__', 'build', 'dist'}
FORBIDDEN_SUFFIXES = {'.jks', '.keystore', '.key', '.pem', '.pyc', '.log', '.partial', '.jsonl', '.db', '.ab', '.vcf'}

def checked_path(name):
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe manifest path')
    if any(part.lower() in FORBIDDEN for part in rel.parts) or rel.suffix.lower() in FORBIDDEN_SUFFIXES:
        raise ValueError('Private/generated path refused: ' + name)
    path = BASE.joinpath(*rel.parts)
    for component in (path, *path.parents):
        if component == BASE:
            break
        if component.is_symlink() or os.path.isjunction(component):
            raise ValueError('Linked path refused: ' + name)
    if not path.is_file() or not path.resolve().is_relative_to(BASE):
        raise ValueError('Missing or external file: ' + name)
    return path

def main():
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest['files']
    names = [entry['path'] for entry in entries]
    if len(set(name.casefold() for name in names)) != len(names) or 'PUBLIC_FILES.json' in names:
        raise ValueError('Duplicate/self-referential manifest entry')
    required = {'Launch.cmd', 'README.md', 'LICENSE', 'runtime/python.exe',
                'tools/platform-tools/adb.exe', 'tools/exiftool/exiftool.exe',
                'companion/AndroidRescueHelper.apk', 'web/index.html'}
    if not required.issubset(names):
        raise ValueError('Incomplete portable package')
    output_dir = BASE / 'dist'
    if output_dir.is_symlink() or os.path.isjunction(output_dir):
        raise ValueError('Linked output directory refused')
    output_dir.mkdir(exist_ok=True)
    output = output_dir / 'AndroidBay-Windows-x64.zip'
    temporary = output.with_suffix('.zip.tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('Temporary output exists; inspect it before retrying')
    with temporary.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for entry in entries:
                data = checked_path(entry['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != entry['sha256']:
                    raise ValueError('Unreviewed change: ' + entry['path'])
                archive.writestr('AndroidBay/' + entry['path'], data)
            archive.writestr('AndroidBay/PUBLIC_FILES.json', manifest_bytes)
    temporary.replace(output)
    digest = hashlib.file_digest(output.open('rb'), 'sha256').hexdigest()
    output.with_suffix('.zip.sha256.txt').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    print(json.dumps({'file': str(output), 'sha256': digest, 'fileCount': len(entries) + 1, 'bytes': output.stat().st_size}))

if __name__ == '__main__':
    main()
