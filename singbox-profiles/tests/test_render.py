#!/usr/bin/env python3
"""
Golden-file tests for render.py.

How this works:
  1. Monkey-patch render.py's file-path globals to point at tests/fixtures/
     (profiles.yaml, .secrets.yaml, home_wg/, hy2.crt). This is how we
     run the full render pipeline without touching production state and
     without adding a --manifest-path flag to render.py itself.
  2. Call render.load_manifest() + render.compose() for each user/device,
     emit_json the result, and compare byte-for-byte against the matching
     tests/goldens/<user>-<device>.json.
  3. A mismatch prints a unified diff and fails with exit 1.
  4. Pass `--update` (or UPDATE_GOLDENS=1) to overwrite goldens with the
     fresh output — use this deliberately after intentional changes, then
     eyeball the git diff of tests/goldens/.

Why not pytest: stdlib-only keeps this test suite runnable on any fresh
checkout of the repo without `pip install`. The tradeoffs are lost
fixtures/parametrize ergonomics and no colored output, both minor for a
single-file harness.

Run directly:
    ./tests/test_render.py           # assert against goldens
    ./tests/test_render.py --update  # regenerate goldens

Exit codes: 0 = all pass, 1 = one or more mismatches, 2 = harness error.
"""
import difflib
import importlib.util
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent.resolve()
FIXTURES = TESTS_DIR / 'fixtures'
GOLDENS = TESTS_DIR / 'goldens'
RENDER_PY = TESTS_DIR.parent / 'render.py'


def _load_render_module():
    """
    Import render.py as a module and overwrite its file-path globals so
    it reads from the fixture dir instead of production. Must happen
    BEFORE any render.* function is called.
    """
    spec = importlib.util.spec_from_file_location('render', RENDER_PY)
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)

    # These constants drive every file read in render.py. Overriding them
    # after import is enough — no module-level I/O happens at import time.
    render.ROOT = FIXTURES
    render.MANIFEST = FIXTURES / 'profiles.yaml'
    render.SECRETS = FIXTURES / '.secrets.yaml'
    render.HOME_WG_DIR = FIXTURES / 'home_wg'
    render.HY2_CERT = FIXTURES / 'hy2.crt'

    return render


def _render_all(render):
    """
    Render every user/device in the fixture manifest. Returns a dict
    {'<user>-<device>': '<json-string>'}.
    Uses -y-equivalent: auto_yes=True prevents prompting, but we also
    patch-save_secrets to a no-op so the test run never mutates the
    fixture .secrets.yaml.
    """
    render.save_secrets = lambda data: None  # defensive: fixtures are read-only

    manifest = render.load_manifest(auto_yes=True)
    out = {}
    for uname, user in manifest['users'].items():
        user.setdefault('_name', uname)
        for dev in user['devices']:
            cfg = render.compose(user, dev, manifest['defaults'])
            out[f'{uname}-{dev["name"]}'] = render.emit_json(cfg)
    return out


def _compare(rendered, update):
    """
    Diff rendered output vs goldens. Return (passes, fails) counts.
    When `update=True`, overwrite goldens instead of failing.
    """
    passes = fails = 0
    GOLDENS.mkdir(exist_ok=True)

    # Track known goldens so we can report orphans (golden files without
    # a matching rendered output — signals a renamed/removed device).
    existing_goldens = {p.stem for p in GOLDENS.glob('*.json')}
    rendered_keys = set(rendered.keys())
    orphans = existing_goldens - rendered_keys

    for key in sorted(rendered.keys()):
        golden = GOLDENS / f'{key}.json'
        new = rendered[key]
        if update:
            golden.write_text(new)
            print(f'  ✎ wrote {golden.name}')
            passes += 1
            continue
        if not golden.exists():
            print(f'  ✗ {key}: NO GOLDEN (run with --update to create)')
            fails += 1
            continue
        old = golden.read_text()
        if old == new:
            print(f'  ✓ {key}')
            passes += 1
        else:
            print(f'  ✗ {key}: rendered output differs from golden')
            diff = difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f'goldens/{key}.json',
                tofile=f'rendered/{key}.json',
                n=3,
            )
            sys.stdout.writelines(diff)
            fails += 1

    for orphan in sorted(orphans):
        print(f'  ⚠ orphan golden: {orphan}.json (no matching device in fixtures)')

    return passes, fails


def main():
    update = '--update' in sys.argv or os.environ.get('UPDATE_GOLDENS') == '1'
    try:
        render = _load_render_module()
    except Exception as e:
        print(f'harness error: failed to import render.py: {e}', file=sys.stderr)
        sys.exit(2)

    try:
        rendered = _render_all(render)
    except Exception as e:
        print(f'harness error: render failed: {e}', file=sys.stderr)
        raise

    passes, fails = _compare(rendered, update=update)
    total = passes + fails
    print()
    if update:
        print(f'regenerated {passes} golden(s)')
        sys.exit(0)
    print(f'{passes}/{total} passed, {fails} failed')
    sys.exit(0 if fails == 0 else 1)


if __name__ == '__main__':
    main()
