#!/usr/bin/env python3
"""The plugins' own code for the editor of the Tessera app: a module per release the index lists.

The app's editor shows a screen with the screen's own firmware, compiled for the browser. A plugin's tile is drawn by
the plugin's C++, which is not in that firmware, so the editor loads it as a module of its own: the plugin's component,
compiled with the same compiler, flags and headers as that firmware (Tessera's web/wasm/build_plugin.py). With it a
person sees the plugin's tile in the editor as it is on the screen, with their own options and data, before anything is
built. This tool builds those modules; the workflow `modules` publishes them on the branch `modules`, where the app
finds them:

    modules/<number>/index.json            what there is for the firmware preview of that number
    modules/<number>/<id>-<version>.wasm   the module of one release

`<number>` is the preview's own (Tessera's web/wasm/toolchain.py, plugin_abi): a module is loaded into the preview's
memory, so it fits exactly the preview it was built against. Tessera's main and dev each have one; a change to the
plugin API's headers is a new number, and every module is built again for it.

    TESSERA_TREE=<a checkout of Tessera> python3 tools/build_modules.py --out <folder> [--own]

Every release of index.json is built once: one that is already in <folder> is left alone, so a run costs only what is
new, and a run that built nothing and learned no new reason leaves index.json as it is (no commit on the branch each
hour; its `built` is when a module or a reason last changed). A plugin of plugins/ is built from this checkout, a community plugin from its release's commit, fetched from its
repository (`--own` leaves those out). A plugin that needs or brings hardware has nothing a browser can run, and one
whose plugin.yaml makes ESPHome entities of its own is not built yet: both are listed under `skipped` with the reason,
and the editor keeps its drawn placeholder for them. Each module is checked the way the app checks it before a browser
gets it (Tessera's screen_manager/app/plugin_module.py): it names its plugin, version and preview, and reaches only
into the preview.

Needs Emscripten on the PATH at the version Tessera pins (.github/workflows/preview.yml, EMSDK_VERSION) and PyYAML.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWN = 'maxgramser/tessera-plugins'


def tessera():
    tree = os.environ.get('TESSERA_TREE')
    if not tree or not (Path(tree) / 'web/wasm/build_plugin.py').is_file():
        raise SystemExit('TESSERA_TREE: a checkout of Tessera with web/wasm/build_plugin.py (its dev, until the release that brings it)')
    return Path(tree).resolve()


def fetched(repo, sha, work):
    """A plugin's repository at one commit, in a folder of its own."""
    folder = Path(work) / hashlib.sha256(f'{repo}@{sha}'.encode()).hexdigest()[:16]
    if not folder.is_dir():
        folder.mkdir(parents=True)
        for args in (['init', '-q'], ['fetch', '-q', '--depth', '1', repo, sha], ['checkout', '-q', 'FETCH_HEAD']):
            subprocess.run(['git', *args], cwd=folder, check=True)
    return folder


def source(plugin, work, own_only):
    """The folder a release is built from, or None when it is not this run's to build."""
    repo, path, release = plugin['repo'], plugin.get('path') or '.', plugin['release']
    local = ROOT / path
    if repo.lower().rstrip('/').endswith(OWN) and (local / 'tessera-plugin.yaml').is_file():
        import yaml
        if str((yaml.safe_load((local / 'tessera-plugin.yaml').read_text(encoding='utf-8')) or {}).get('version')) == str(release['version']):
            return local
    if own_only:
        return None
    return fetched(repo, release['sha'], work) / path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out', required=True, help='the branch `modules` as a folder: modules/<number>/ is written in it')
    parser.add_argument('--own', action='store_true', help='only the plugins of this repository (no fetching)')
    parser.add_argument('--index', default=str(ROOT / 'index.json'))
    args = parser.parse_args()
    tree = tessera()
    sys.path[:0] = [str(tree / 'web/wasm'), str(tree / 'screen_manager/app')]
    import plugin_module
    import toolchain
    number = toolchain.plugin_abi()
    folder = Path(args.out).resolve() / 'modules' / number
    folder.mkdir(parents=True, exist_ok=True)
    listing = folder / 'index.json'
    before = json.loads(listing.read_text()) if listing.is_file() else {}
    modules, skipped = before.get('modules') or {}, before.get('skipped') or {}
    had = json.dumps([modules, skipped], sort_keys=True)   # as the listing has them, to tell what this run changed
    built = failed = 0
    with tempfile.TemporaryDirectory() as work:
        for plugin in json.loads(Path(args.index).read_text())['plugins']:
            name, version = plugin['id'], str(plugin['release']['version'])
            file = folder / f'{name}-{version}.wasm'
            have = (modules.get(name) or {}).get(version)
            if (have and file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == have['sha256']) or \
                    (skipped.get(name) or {}).get(version):
                continue
            try:
                where = source(plugin, work, args.own)
            except subprocess.CalledProcessError as error:
                print(f'not fetched  {name} {version}: {error}')
                failed += 1
                continue
            if where is None:
                continue
            done = subprocess.run([sys.executable, str(tree / 'web/wasm/build_plugin.py'), str(where), '--out', str(file)],
                                  capture_output=True, text=True)
            if done.returncode == 2:
                reason = next((line for line in done.stderr.splitlines() if line.startswith('Not built: ')), 'Not built.')[11:]
                skipped.setdefault(name, {})[version] = reason
                print(f'skipped      {name} {version}: {reason}')
                continue
            body = file.read_bytes() if done.returncode == 0 and file.is_file() else b''
            if not body:
                # A plugin that does not compile for the browser (it uses a part of ESPHome the preview does not
                # stand in for) is no failure of this run: it is listed with the compiler's first word on it, keeps
                # its placeholder in the editor, and is tried again for the next number or its next release.
                file.unlink(missing_ok=True)
                errors = [line.strip() for line in done.stderr.splitlines() if 'error' in line]
                reason = 'it does not compile for the browser' + (f': {errors[0][-160:]}' if errors else '')
                skipped.setdefault(name, {})[version] = reason
                print(f'skipped      {name} {version}: {reason}')
                continue
            why = plugin_module.fits(body, name, version, number)
            if why:
                file.unlink(missing_ok=True)
                print(f'not built    {name} {version}: {why}')
                failed += 1
                continue
            modules.setdefault(name, {})[version] = {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
            print(f'built        {name} {version}: {len(body)} bytes')
            built += 1
    if not listing.is_file() or json.dumps([modules, skipped], sort_keys=True) != had:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=tree, capture_output=True, text=True).stdout.strip()
        listing.write_text(json.dumps({'format': 1, 'abi': number, 'tessera': head, 'emscripten': toolchain.compiler(),
                                       'built': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                       'modules': modules, 'skipped': skipped}, indent=1, sort_keys=True) + '\n')
    total = sum(len(versions) for versions in modules.values())
    print(f'{number}: {built} built now, {total} modules in all, {sum(len(v) for v in skipped.values())} skipped, {failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
