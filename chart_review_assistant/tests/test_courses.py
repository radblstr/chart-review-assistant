# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for Course/Site construction edge cases that the snapshot corpus cannot express. Each
test authors a db_pull with make_snapshots.make_pull, alters it where needed, and builds
the courses with no DB access.
"""

import pandas as pd
import pytest

from chart_review_assistant import app, config
from chart_review_assistant.query_db import build_courses
from chart_review_assistant.tests.make_snapshots import (ROOM, SLOTS, make_cfg, simple_site,
                                                          make_pull, note)


@pytest.fixture(autouse=True)
def case_config(monkeypatch):
    """Pin config.load_config to the corpus default config, so build_courses (which reads the
    per-room thresholds from config) never sees the live clinic/user files.
    """
    merged = config._merge(config.default_config(), make_cfg())
    monkeypatch.setattr(config, 'load_config', lambda: dict(merged))


def test_build_courses_uses_current_thresholds(monkeypatch):
    """A rebuild applies the Settings thresholds in force now, read from config, not anything in
    the pull: lowering icc_missed_fx to 1 flags a 1-fraction site with no note as missed.
    """
    db_pull, _, _ = make_pull(SLOTS[0] + pd.Timedelta(hours=3), [simple_site(1)])
    assert [w['id'] for w in build_courses(db_pull, [1])[0].sites[0].warnings
            if w['id'].startswith('icc')] == ['icc_due']
    cfg = make_cfg([dict(label='Test 1', room=ROOM, active=True, icc_missed_fx=1)])
    monkeypatch.setattr(config, 'load_config', lambda: config._merge(config.default_config(), cfg))
    ids = [w['id'] for w in app.courses_from_pull(db_pull)[0].sites[0].warnings]
    assert 'icc_missed' in ids and 'icc_due' not in ids
    assert 'thresholds' not in db_pull


def _lr_pair(n_tx, note_after=None):
    """The first built site of a date-alternating Prostate R (day 1) / L (day 2) pair

    n_tx fractions are treated in total, optionally with one ICC note two hours after treated
    fraction index note_after.
    """
    right = dict(sit=11, pcp=21, name='Prostate R', rx=(200.0, 5, 1000.0),
                 tx=SLOTS[0:n_tx:2], cal=SLOTS[0:10:2])
    left = dict(sit=12, pcp=21, name='Prostate L', rx=(200.0, 5, 1000.0),
                tx=SLOTS[1:n_tx:2], cal=SLOTS[1:10:2])
    notes = () if note_after is None else (note(note_after, 'Initial eChart Check'),)
    db_pull, _, _ = make_pull(SLOTS[n_tx - 1] + pd.Timedelta(hours=3), [right, left], notes)
    return build_courses(db_pull, [1])[0].sites[0]


def test_lr_merge_counts_note_after_right_side_fraction():
    """An ICC note logged after the R side's first fraction completes the merged site's ICC.

    The note falls before the L side treats; the ICC is not left due with a completion date.
    """
    site = _lr_pair(2, note_after=0)
    assert site.site_name == 'Prostate L/R (merged)'
    assert site.icc_completion_dt == SLOTS[0] + pd.Timedelta(hours=2)
    assert site.icc_due is False and site.icc_missed is False
    assert len(site.tx_hst_dts) == 2


def test_lr_merge_counts_both_sides_for_icc_missed():
    """Six fractions across both sides with no note is an ICC missed at the default threshold.

    Before the merge rebuilt its state the L side alone (3 fractions) was still only due.
    """
    site = _lr_pair(6)
    assert len(site.tx_hst_dts) == 6
    assert site.icc_missed is True and site.icc_due is False


def test_null_rx_fractions_does_not_raise():
    """A DSS row with NULL RxFractions is skipped like any other incomplete Rx instead of raising.

    Raising would blank the board. courses.py Course.__init__ drops sites with a zeroed or absent
    Rx.
    """
    db_pull, _, _ = make_pull(SLOTS[0] + pd.Timedelta(hours=3), [simple_site(1)])
    db_pull['dss']['RxFractions'] = pd.Series([float('nan')], dtype='float64')
    assert build_courses(db_pull, [1]) == []


def test_all_null_dose_column_does_not_raise():
    """An all-NULL dose column (object dtype of None) reads as 0 and falls back to the other dose.
    """
    db_pull, _, _ = make_pull(SLOTS[0] + pd.Timedelta(hours=3), [simple_site(1)])
    db_pull['dss']['RxFxUniformDoseInCcGE'] = pd.Series([None], dtype='object')
    db_pull['dss']['RxTotalDoseInCcGE'] = pd.Series([None], dtype='object')
    courses = build_courses(db_pull, [1])
    site = courses[0].sites[0]
    assert site.rx_fx_dose_rbe == 0.0
    assert site.rx == '200 cGy x 15 = 3000 cGy'
