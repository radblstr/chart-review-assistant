# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
The user-defined case in make_snapshots.py builds and replays. The USER_* variables ship with
defaults that describe a complete, finding-free case, so this both exercises the generator path a
user follows and checks that the example they start from is itself a valid snapshot. Writes only
to pytest's tmp_path.
"""

import json

from chart_review_assistant.tests import make_snapshots
from chart_review_assistant.tests.test_snapshots import replay_case


def test_user_case_builds_and_replays(tmp_path, monkeypatch):
    """The USER_* example builds a complete case directory, and the real pull replayed against
    its mock DB renders the baseline rows.
    """
    case_dir = make_snapshots.build_case(make_snapshots.user_case(), tmp_path)
    assert case_dir == tmp_path / make_snapshots.USER_CASE_NAME
    name = make_snapshots.USER_CASE_NAME
    for filename in (f'{name}.json', f'{name}.db', 'clinic_config.toml', 'app_state.json'):
        assert (case_dir / filename).exists(), filename

    # The written clinic_config.toml carries the USER_* room and charge code
    clinic = (case_dir / 'clinic_config.toml').read_text(encoding='utf-8')
    assert f'room = "{make_snapshots.USER_ROOM}"' in clinic
    assert f'cc_cpt_code = "{make_snapshots.USER_CC_CPT_CODE}"' in clinic

    # One board row per site, with the expected findings and nothing else
    baseline = json.loads((case_dir / f'{name}.json').read_text(encoding='utf-8'))
    assert len(baseline['row_data']) == len(make_snapshots.USER_SITES)
    found = {w for r in baseline['row_data']
             for w in (r['__errors'] + r['__warnings'] + r['__notifications'])}
    assert bool(found) == bool(make_snapshots.USER_EXPECTED_FINDINGS)

    replay_case(case_dir, monkeypatch)


def test_user_case_rejects_wrong_expectation(tmp_path, monkeypatch):
    """A USER_EXPECTED_FINDINGS that disagrees with the built course stops generation with the
    case name and both finding sets in the message, so a user sees what to correct.
    """
    monkeypatch.setattr(make_snapshots, 'USER_EXPECTED_FINDINGS', {'icc_missed'})
    try:
        make_snapshots.build_case(make_snapshots.user_case(), tmp_path)
    except AssertionError as e:
        assert make_snapshots.USER_CASE_NAME in str(e) and 'icc_missed' in str(e)
    else:
        raise AssertionError('build_case accepted a wrong expectation')
