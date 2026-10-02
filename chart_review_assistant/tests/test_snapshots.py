# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Regression: each case is a self-contained directory named for its scenario. The inputs are the
mock Mosaiq DB (<case>.db), clinic_config.toml, and the state file (app_state.json); the baseline
(<case>.json) is read only at compare time. The test points the app's own readers at the case
directory, runs the real query_db pull mid-run, compares the raw pulled data against the
baseline's db_pull, then renders build_table and compares the rows against the baseline's
row_data. No live database and no PHI
"""

import json

import pandas as pd
import pytest
import sqlalchemy

import chart_review_assistant
from chart_review_assistant import app, config, snapshot
from chart_review_assistant.checks import CHECKS
from chart_review_assistant.query_db import query_db, selection_window


def _cases():
    """List case directories (a baseline JSON with its sibling DB) under tests/snapshots.

    tests/snapshots may be absent.
    """
    if not snapshot.SNAPSHOT_DIR.exists():
        return []
    return [p for p in sorted(snapshot.SNAPSHOT_DIR.iterdir())
            if p.is_dir() and any(f.with_suffix('.json').exists() for f in p.glob('*.db'))]


def _norm(frame):
    """Normalize a frame for comparison.

    Common datetime resolution (the SQLite round trip yields microseconds, the table-schema
    baseline nanoseconds), rows sorted by all columns, index dropped
    """
    frame = frame.copy()
    for col in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[col]):
            frame[col] = frame[col].astype('datetime64[ns]')
    if len(frame):
        frame = frame.sort_values(list(frame.columns))
    return frame.reset_index(drop=True)


@pytest.mark.parametrize('case', _cases() or [None], ids=lambda p: getattr(p, 'name', 'no-cases'))
def test_snapshot_matches_render(case, monkeypatch):
    """Point the app's readers at the case directory and run the real pull on its mock Mosaiq DB.

    Asserts, mid-run, that the raw db_pull equals the baseline's, then that the rendered rows
    equal the baseline's row_data. Skips when no case directories exist.
    """
    if case is None:
        pytest.skip('no snapshots')
    replay_case(case, monkeypatch)


def test_blank_cc_cpt_code_skips_charge_findings(tmp_path, monkeypatch):
    """A blank cc_cpt_code reports no charge findings, not CC charges missing on every course."""
    case = snapshot.SNAPSHOT_DIR / 'cc_missed'
    db = case / 'cc_missed.db'
    user = tmp_path / 'user_config.toml'
    user.write_text('[general]\ncc_cpt_code = ""\n')
    monkeypatch.setattr(chart_review_assistant, '_engine',
                        sqlalchemy.create_engine(f'sqlite:///{db.as_posix()}'))
    monkeypatch.setattr(config, 'CLINIC_CONFIG_FILE', str(case / 'clinic_config.toml'))
    monkeypatch.setattr(config, 'USER_CONFIG_FILE', str(user))
    monkeypatch.setattr(app, 'STATE_FILE', str(case / 'app_state.json'))
    state = app.load_state()
    state['now'] = pd.Timestamp(state['now'])
    courses, _ = query_db(state)
    ids = {w['id'] for c in courses for s in c.sites for w in s.warnings}
    assert courses
    assert not ids & set(CHECKS['cc_charge'])


def replay_case(case, monkeypatch):
    """Replay one case directory through the real pull and compare it to its baseline.

    Shared with test_make_snapshots.py, which builds a case into a temp directory first.

    Args:
        case (Path): The case directory.
        monkeypatch (pytest.MonkeyPatch): Used to point the app's readers at the case.
    """
    db = case / f'{case.name}.db'

    # The whole input is the case directory: the engine reads its mock DB, config resolves from
    # its clinic_config.toml, and the run state (rooms, window, pinned clock) from its state file.
    monkeypatch.setattr(chart_review_assistant, '_engine',
                        sqlalchemy.create_engine(f'sqlite:///{db.as_posix()}'))
    monkeypatch.setattr(config, 'CLINIC_CONFIG_FILE', str(case / 'clinic_config.toml'))
    monkeypatch.setattr(config, 'USER_CONFIG_FILE', str(case / 'user_config.toml'))
    monkeypatch.setattr(app, 'STATE_FILE', str(case / 'app_state.json'))
    state = app.load_state()
    state['now'] = pd.Timestamp(state['now'])
    window = selection_window(state)
    courses, fresh = query_db(state)

    # Mid-run raw-data comparison: every baseline pull frame must come back identical from the
    # mock DB (row order normalized away; SQLite loses exact dtypes, values must still match).
    # The baseline is the DB's stem-mate, opened here for the first time.
    snap, expected_frames = snapshot.load_baseline(db.with_suffix('.json'))
    for k, expected in expected_frames.items():
        pd.testing.assert_frame_equal(
            _norm(fresh[k]), _norm(expected), check_dtype=False,
            obj=f'{case.name}: db_pull[{k}]')
    assert pd.Timestamp(fresh['now']) == pd.Timestamp(snap['db_pull']['now'])

    # Render comparison, in JSON form -- the row contract is what Dash serializes to the browser.
    got = snapshot.strip_session_keys(app.build_table(courses, window, state['locations']))
    got = json.loads(json.dumps(got, default=str))
    assert got == snap['row_data'], f'{case.name}: render differs from snapshot'
