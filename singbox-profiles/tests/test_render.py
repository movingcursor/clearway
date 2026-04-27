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
    # AWG paths: the client template lives in the real templates/ dir
    # (read-only), but the awg-server stub path is rebased under FIXTURES so
    # the stub-emission test path doesn't write into the real awg-server/
    # location. Tests assert against the stub's *content*, not its filesystem
    # location, so a fixtures-relative path works for the comparison without
    # mutating the live repo layout.
    render.AWG_SERVER_DIR = FIXTURES / 'awg-server'
    render.AWG_SERVER_CONFIG = FIXTURES / 'awg-server' / 'config' / 'awg0.conf'
    # AWG_CLIENT_TEMPLATE stays at the real location (read-only template,
    # part of the renderer's static assets — not fixture-dependent).

    return render


def _render_all(render):
    """
    Render every output the renderer produces from the fixture manifest.
    Returns a dict {'<filename>': '<file-contents>'} — the key is the full
    filename including extension so .json (sing-box configs) and .conf
    (AWG client + awg-server stub) co-exist in one map. _compare() looks
    each one up under the same name in tests/goldens/.
    Uses -y-equivalent: auto_yes=True prevents prompting, but we also
    patch save_secrets to a no-op so the test run never mutates the
    fixture .secrets.yaml.
    """
    render.save_secrets = lambda data: None  # defensive: fixtures are read-only

    manifest = render.load_manifest(auto_yes=True)
    out = {}
    for uname, user in manifest['users'].items():
        user.setdefault('_name', uname)
        for dev in user['devices']:
            cfg = render.compose(user, dev, manifest['defaults'])
            out[f'{uname}-{dev["name"]}.json'] = render.emit_json(cfg)

    # AWG outputs: per-device awg-<dev>.conf + awg-server config. AWG
    # identities are per-device (post-2026-04-27); each device has its own
    # keypair, /32 address, and [Peer] block. Only emitted when at least
    # one user has 'awg' in protocols, so non-AWG fixture runs produce
    # nothing here and existing goldens stay clean.
    awg_state = manifest.get('_awg')
    if awg_state:
        for uname, user in manifest['users'].items():
            if 'awg' not in user.get('protocols', []):
                continue
            for dev in user['devices']:
                text = render._render_awg_client_conf(
                    uname,
                    dev['name'],
                    dev,
                    awg_state['block'],
                    awg_state['addresses'][uname][dev['name']],
                )
                out[f'{uname}-{dev["name"]}-awg.conf'] = text
        out['awg-server.conf'] = render._render_awg_server_config(manifest, awg_state)

    return out


def _compare(rendered, update):
    """
    Diff rendered output vs goldens. Return (passes, fails) counts.
    When `update=True`, overwrite goldens instead of failing.

    Keys in `rendered` are full filenames (e.g. test_alice-pixel.json,
    test_dave-awg.conf). Goldens live at tests/goldens/<filename>. The
    extension differentiates sing-box JSON outputs from AWG wg-quick .conf
    outputs without forcing two parallel comparison loops.
    """
    passes = fails = 0
    GOLDENS.mkdir(exist_ok=True)

    # Orphan goldens (a file present in tests/goldens/ that no rendered
    # output matches) signal renamed/removed devices or a fixture user that
    # dropped a protocol. Reported but don't fail the run — the reporter
    # spots them so the operator can clean them up explicitly.
    existing_goldens = {p.name for p in GOLDENS.glob('*') if p.is_file()}
    rendered_keys = set(rendered.keys())
    orphans = existing_goldens - rendered_keys

    for key in sorted(rendered.keys()):
        golden = GOLDENS / key
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
                fromfile=f'goldens/{key}',
                tofile=f'rendered/{key}',
                n=3,
            )
            sys.stdout.writelines(diff)
            fails += 1

    for orphan in sorted(orphans):
        print(f'  ⚠ orphan golden: {orphan} (no matching output in fixtures)')

    return passes, fails


def _test_x25519_derivation(render):
    """
    Unit test: render._x25519_public_from_private must produce the canonical
    WG public key for a known private key. Uses the WireGuard upstream test
    vector (RFC-7748 / wg's `tools/embedded-test/test-key`) so a regression
    in the cryptography lib or a parameter mishap in render.py shows up
    immediately rather than at handshake-fail time on a real client.

    Vector source: cryptography library's own test suite + RFC-7748 test
    vectors. Private key 0x77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a
    (base64 d3B20Kcxi...) -> public 0x8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a.
    """
    import base64
    priv_b = bytes.fromhex(
        '77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a'
    )
    expected_pub_b = bytes.fromhex(
        '8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a'
    )
    priv_b64 = base64.b64encode(priv_b).decode()
    got_pub = render._x25519_public_from_private(priv_b64)
    expected_pub = base64.b64encode(expected_pub_b).decode()
    if got_pub == expected_pub:
        print('  ✓ x25519-derivation: matches RFC-7748 test vector')
        return 1, 0
    print(f'  ✗ x25519-derivation: expected {expected_pub!r}, got {got_pub!r}')
    return 0, 1


def _test_awg_negative(render):
    """
    Negative test: an AWG-enabled device with no `awg_private_key` under
    .secrets.yaml.users.<n>.devices.<dev> must cause _validate_awg_block
    to exit with a message that names both the field and the offending
    user/device. Uses the validation function directly with a synthetic
    minimal manifest + sfile, so it doesn't need its own fixture tree on
    disk and doesn't pollute the goldens.

    Returns (passed, failed) — printed as a single line by main().
    """
    awg_block = {
        'subnet': '10.66.66.0/24', 'port': 51820,
        'endpoint_host': 'vpn.example.com',
        'server_private_key': 'X', 'server_public_key': 'Y',
        'Jc': 8, 'Jmin': 40, 'Jmax': 80, 'S1': 75, 'S2': 110,
        'H1': 1, 'H2': 2, 'H3': 3, 'H4': 4,
    }
    # Device exists in profiles.yaml but its slot in .secrets.yaml is empty —
    # _autogen_missing would normally fill this in; the validator is the last
    # line of defence if autogen was bypassed (e.g. an operator hand-edit).
    sfile = {'users': {'someone': {'devices': {'phone': {}}}}}
    manifest = {
        'users': {
            'someone': {
                'protocols': ['awg'],
                'devices': [{'type': 'mobile', 'name': 'phone'}],
            }
        }
    }
    try:
        render._validate_awg_block(awg_block, sfile, manifest)
    except SystemExit as e:
        msg = str(e)
        if 'awg_private_key' in msg and 'someone/phone' in msg:
            print('  ✓ awg-negative: missing per-device awg_private_key fails loud')
            return 1, 0
        print(f'  ✗ awg-negative: SystemExit message did not mention '
              f'awg_private_key + the offending uname/dev: {msg!r}')
        return 0, 1
    print('  ✗ awg-negative: validation did NOT exit when awg_private_key '
          'was missing — regression of the per-device hard check')
    return 0, 1


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

    # Unit + negative tests run even in --update mode (they're not goldens,
    # they exercise renderer-internal checks). Skipping under --update
    # would silently lose the regression coverage.
    x_pass, x_fail = _test_x25519_derivation(render)
    neg_pass, neg_fail = _test_awg_negative(render)
    extra_pass = x_pass + neg_pass
    extra_fail = x_fail + neg_fail
    passes += extra_pass
    fails += extra_fail

    total = passes + fails
    print()
    if update:
        print(f'regenerated {passes - extra_pass} golden(s); '
              f'{extra_pass}/{extra_pass + extra_fail} unit+negative test(s) passed')
        sys.exit(0 if extra_fail == 0 else 1)
    print(f'{passes}/{total} passed, {fails} failed')
    sys.exit(0 if fails == 0 else 1)


if __name__ == '__main__':
    main()
