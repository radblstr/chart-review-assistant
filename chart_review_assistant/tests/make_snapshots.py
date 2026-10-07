# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Builds the snapshot test corpus: one directory per chart-check scenario under tests/snapshots,
each holding the test's whole input (<case>.db mock Mosaiq DB, clinic_config.toml,
app_state.json) and its baseline (<case>.json, read only at compare time).

Each case authors a db_pull exactly as query_db returns it (same frames, same columns), asserts
the intended findings fire on the built course (and nothing unintended), renders the baseline
rows, and writes the case directory via write_test_case. test_snapshots.py then replays every
case through the real SQL pull.

Rebuild the built-in corpus after any audit-rule change and review the resulting diff:

    python -m chart_review_assistant.tests.make_snapshots

Add your own case: fill in the USER_* variables in the "Your own case" block below, then

    python -m chart_review_assistant.tests.make_snapshots --user

writes tests/snapshots/<USER_CASE_NAME>/ and the test suite picks it up on the next run.
"""

import contextlib
import datetime
import sys

import pandas as pd

from chart_review_assistant import config, snapshot
from chart_review_assistant.app import build_table
from chart_review_assistant.query_db import build_courses

ROOM = 'TEST1'
ROOM2 = 'TEST2'
CPT = 'TESTCC'
WINDOW = (datetime.date(2024, 2, 26), datetime.date(2024, 4, 5))

# 30 weekday fraction slots at 09:00 starting Monday 2024-03-04. Fraction N of a case is SLOTS[N-1];
# a case may prescribe at most this many fractions.
SLOTS = [pd.Timestamp(d) + pd.Timedelta(hours=9) for d in pd.bdate_range('2024-03-04', periods=30)]

# ---------------------------------------------------------------------------------------------
# Your own case. Edit these values, then run this module with --user (see the module docstring).
# Everything a case needs is here; nothing else in this file has to change. The defaults make a
# complete, passing case: one 15-fraction photon site with 8 fractions treated, the initial chart
# check noted after fraction 2, a weekly check after fraction 8, and charges at fractions 5 and 7,
# so the board shows no findings.
# ---------------------------------------------------------------------------------------------

# Directory name under tests/snapshots (letters, digits, underscores).
USER_CASE_NAME = 'user_example'

# Treatment room exactly as the machine is named in Mosaiq (Staff.Last_Name), and its board label.
USER_ROOM = 'ROOM1'
USER_ROOM_LABEL = 'Room 1'

# Chart-check charge code (CPT.CPT_Code). Blank disables the charge findings.
USER_CC_CPT_CODE = 'TESTCC'

# Per-room Settings thresholds. None keeps the code default (6, 500 cGy, 3, true).
USER_ICC_MISSED_FX = None
USER_SBRT_FX_DOSE_CGY = None
USER_SBRT_MISSED_FX = None
USER_ICC_COUNTS_AS_WCC = None

# When the board is "viewed": this many hours after the last treated fraction (fractions are at
# 09:00 on consecutive weekdays). With nothing treated yet, the clock is the day before fraction 1.
USER_HOURS_AFTER_LAST_FRACTION = 3

# The course's sites. One dict each:
#   name          site name as it appears in Mosaiq (Site.Site_Name)
#   fx_dose_cgy   prescribed dose per fraction (cGy)
#   n_fractions   prescribed number of fractions (at most len(SLOTS))
#   n_treated     fractions delivered so far; the first n_treated scheduled fractions are treated
#   rbe           True writes the dose into the RBE columns too (protons); False leaves them 0
USER_SITES = [
    dict(name='Prostate', fx_dose_cgy=200.0, n_fractions=15, n_treated=8, rbe=False),
]

# eChart Check notes as (fraction number the note follows, hours after that fraction, subject).
# A subject containing "Initial" is the initial chart check; anything else is a weekly check.
USER_NOTES = [
    (2, 2, 'Initial eChart Check'),
    (8, 2, 'Weekly eChart Check'),
]

# Chart-check charges: the fraction number each charge was posted at (same time as the fraction).
USER_CHARGES = [5, 7]

# Finding ids the board must show for this case, and nothing else (ids are the keys in
# checks.py CHECKS, e.g. 'icc_due', 'wcc_missed', 'cc_too_few'). Generation stops with a message
# if the built course disagrees, so this is the case's assertion.
USER_EXPECTED_FINDINGS = set()


def make_cfg(machines=None):
    """The case's config: charge CPT code set, no QCL task types, one default machine profile"""
    return dict(
        general=dict(qcl_task_types=[], cc_cpt_code=CPT),
        machines=machines if machines is not None else [
            dict(label='Test 1', room=ROOM, active=True)])


def make_pull(now, site_specs, notes=(), charges=(), cfg=None):
    """Author a whole-board db_pull exactly as query_db returns it.

    Args:
        now (pd.Timestamp): Pinned pull clock.
        site_specs (list[dict]): One per site: sit (id), pcp (course key), name, rx
            ((fx_dose_cgy, n_fxs, total_cgy)), tx (list[pd.Timestamp] treated), cal
            (list[pd.Timestamp] scheduled incl. treated), rooms (str | list[str] per cal slot),
            rbe (bool, default True; False writes 0 to IsDoseInCcGE, as for photons).
        notes (tuple[tuple[pd.Timestamp, str], ...]): CC notes as (Create_DtTm, Subject).
        charges (tuple[pd.Timestamp, ...]): CC charge Proc_DtTm values.
        cfg (dict[str, dict | list[dict]] | None): Case config; defaults to make_cfg().

    Returns:
        tuple[dict[str, pd.DataFrame | pd.Timestamp], dict[str, dict | list[dict]],
            dict[str, dict | list[dict]]]: (db_pull, cfg, merged) where merged is cfg layered
            over the code defaults, for pinned_config when the courses are built.
    """
    cfg = cfg if cfg is not None else make_cfg()
    sites_rows, dh_rows, cal_rows, sched_rows = [], [], [], []
    next_id = 100
    for spec in site_specs:
        rooms = spec.get('rooms', ROOM)
        fx_dose, n_fxs, total = spec['rx']
        rbe = spec.get('rbe', True)
        sites_rows.append(dict(
            Pat_ID1=1,
            SIT_ID=spec['sit'],
            Site_Name=spec['name'],
            PCP_ID=spec['pcp'],
            Dose_Tx=float(fx_dose),
            Dose_Ttl=float(total),
            Fractions=int(n_fxs),
            IsDoseInCcGE=int(rbe),
            Course=1,
            MED_ID=31,
            Diag_Code='C61',
            Diag_Desc='Test diagnosis'))
        treated = set(spec['tx'])
        for i, ts in enumerate(spec['cal']):
            room = rooms[i] if isinstance(rooms, list) else rooms
            pci = next_id
            next_id += 1
            cal_rows.append(dict(
                Pat_ID1=1,
                SIT_ID=spec['sit'],
                PCI_ID=pci,
                PTC_ID=pci,
                Due_DtTm=ts,
                Act_DtTm=ts if ts in treated else pd.NaT,
                Status_Enum=0,
                Seq=1,
                Last_Name=room))
            sched_rows.append(dict(Pat_ID1=1, App_DtTm=ts, Last_Name=room))
            if ts in treated:
                dh_rows.append(dict(
                    Pat_ID1=1,
                    SIT_ID=spec['sit'],
                    PCI_ID=pci,
                    PTC_ID=pci,
                    Fractions_Tx=1,
                    Fx_Number_InEffect=sorted(treated).index(ts) + 1,
                    Tx_DtTm=ts,
                    Last_Name=room))
    merged = config._merge(config.default_config(), cfg)
    db_pull = dict(
        sites=pd.DataFrame(sites_rows),
        cc_charges=pd.DataFrame(dict(
            Pat_ID1=pd.Series([1] * len(charges), dtype='int64'),
            Proc_DtTm=pd.Series(list(charges), dtype='datetime64[ns]'))),
        dose_history=pd.DataFrame(dh_rows, columns=[
            'Pat_ID1', 'SIT_ID', 'PCI_ID', 'PTC_ID', 'Fractions_Tx', 'Fx_Number_InEffect',
            'Tx_DtTm', 'Last_Name']),
        calendar=pd.DataFrame(cal_rows),
        qcls=pd.DataFrame(dict(
            Pat_ID1=pd.Series(dtype='int64'),
            Description=pd.Series(dtype='object'),
            Complete=pd.Series(dtype='int64'),
            Act_DtTm=pd.Series(dtype='datetime64[ns]'),
            Due_DtTm=pd.Series(dtype='datetime64[ns]'))),
        cc_notes=pd.DataFrame(dict(
            Pat_ID1=pd.Series([1] * len(notes), dtype='int64'),
            Create_DtTm=pd.Series([n[0] for n in notes], dtype='datetime64[ns]'),
            Subject=pd.Series([n[1] for n in notes], dtype='object'))),
        schedule=pd.DataFrame(sched_rows, columns=['Pat_ID1', 'App_DtTm', 'Last_Name']),
        patients=pd.DataFrame(dict(
            Pat_ID1=pd.Series([1], dtype='int64'),
            Last_Name=['Pat 1'],
            First_Name=[''],
            IDB=['1'])),
        now=now,
    )
    return db_pull, cfg, merged


def simple_site(tx_n, n_rx=15, fx_dose=200.0, rooms=ROOM):
    """One-site spec: the first tx_n SLOTS treated, all n_rx scheduled"""
    return dict(sit=11, pcp=21, name='Test site', rx=(fx_dose, n_rx, fx_dose * n_rx),
                tx=SLOTS[:tx_n], cal=SLOTS[:n_rx], rooms=rooms)


def note(i, subject='Weekly eChart Check', hours=2):
    """A CC note `hours` after fraction slot i (0-based)"""
    return (SLOTS[i] + pd.Timedelta(hours=hours), subject)


def cases():
    """Yield (case_name, now, site_specs, notes, charges, cfg, locations, expected finding ids)."""
    icc = (note(1, 'Initial eChart Check'),)
    yield ('icc_due', SLOTS[0] + pd.Timedelta(hours=3), [simple_site(1)],
           (), (), None, [ROOM], {'icc_due', 'wcc_due'})
    yield ('icc_missed_no_note', SLOTS[5] + pd.Timedelta(hours=3), [simple_site(6)],
           (), (), None, [ROOM],
           {'icc_missed', 'wcc_due', 'wcc_missed', 'wcc_overdue'})
    yield ('icc_missed_late_note', SLOTS[5] + pd.Timedelta(hours=3), [simple_site(6)],
           (note(5, 'Initial eChart Check'),), (), None, [ROOM],
           {'icc_missed', 'wcc_missed', 'cc_too_few'})
    yield ('icc_custom_threshold', SLOTS[2] + pd.Timedelta(hours=3), [simple_site(3)],
           (), (),
           make_cfg([dict(label='Test 1', room=ROOM, active=True, icc_missed_fx=3)]),
           [ROOM], {'icc_missed', 'wcc_due'})
    yield ('sbrt_icc_due', SLOTS[0] + pd.Timedelta(hours=3),
           [simple_site(1, n_rx=5, fx_dose=800.0)],
           (), (), None, [ROOM], {'sbrt_icc_due', 'wcc_due'})
    yield ('sbrt_icc_missed', SLOTS[2] + pd.Timedelta(hours=3),
           [simple_site(3, n_rx=5, fx_dose=800.0)],
           (), (), None, [ROOM], {'icc_missed', 'wcc_due'})
    yield ('sbrt_photon', SLOTS[0] + pd.Timedelta(hours=3),
           [dict(simple_site(1, n_rx=5, fx_dose=800.0), rbe=False)],
           (), (), None, [ROOM], {'sbrt_icc_due', 'wcc_due'})
    yield ('icc_missed_on_complete', SLOTS[4] + pd.Timedelta(hours=3),
           [simple_site(5, n_rx=5)],
           (), (), None, [ROOM], {'icc_missed', 'fcc_due'})
    yield ('sbrt_icc_missed_on_complete', SLOTS[1] + pd.Timedelta(hours=3),
           [simple_site(2, n_rx=2, fx_dose=800.0)],
           (), (), None, [ROOM], {'icc_missed', 'fcc_due'})
    yield ('wcc_due', SLOTS[5] + pd.Timedelta(hours=3), [simple_site(6)],
           icc, (), None, [ROOM], {'wcc_due', 'cc_too_few'})
    yield ('wcc_next_fx', SLOTS[6] + pd.Timedelta(hours=3), [simple_site(7)],
           icc, (), None, [ROOM], {'wcc_due', 'wcc_next_fx', 'cc_too_few'})
    yield ('wcc_overdue_missed', SLOTS[10] + pd.Timedelta(hours=3), [simple_site(11)],
           icc, (), None, [ROOM],
           {'wcc_due', 'wcc_missed', 'wcc_overdue', 'cc_too_few'})
    yield ('icc_not_counted_as_wcc', SLOTS[5] + pd.Timedelta(hours=3), [simple_site(6)],
           icc, (),
           make_cfg([dict(label='Test 1', room=ROOM, active=True, icc_counts_as_wcc=False)]),
           [ROOM], {'wcc_due', 'wcc_missed', 'wcc_overdue'})
    yield ('fcc_due', SLOTS[4] + pd.Timedelta(hours=3), [simple_site(5, n_rx=5)],
           icc, (SLOTS[4],), None, [ROOM], {'fcc_due'})
    yield ('fcc_missed', pd.Timestamp('2024-03-18 12:00:00'), [simple_site(5, n_rx=5)],
           icc, (SLOTS[4],), None, [ROOM], {'fcc_missed'})
    yield ('cc_underbilled_active', SLOTS[7] + pd.Timedelta(hours=3), [simple_site(8)],
           icc + (note(7),), (SLOTS[4],), None, [ROOM], {'cc_too_few'})
    yield ('cc_missed', pd.Timestamp('2024-03-18 12:00:00'), [simple_site(5, n_rx=5)],
           icc + (note(4, hours=4),), (), None, [ROOM],
           {'cc_too_few'})
    yield ('cc_too_many', SLOTS[7] + pd.Timedelta(hours=3), [simple_site(8)],
           icc + (note(7),), (SLOTS[2], SLOTS[4], SLOTS[6]), None, [ROOM], {'cc_too_many'})
    yield ('clean_green', SLOTS[7] + pd.Timedelta(hours=3), [simple_site(8)],
           icc + (note(7),), (SLOTS[4], SLOTS[6]), None, [ROOM], set())
    yield ('cc_remainder_2_over', pd.Timestamp('2024-03-20 12:00:00'),
           [simple_site(7, n_rx=7)], icc + (note(5), note(6)), (SLOTS[4], SLOTS[6]), None,
           [ROOM], {'cc_too_many'})
    yield ('cc_remainder_2_matched', pd.Timestamp('2024-03-20 12:00:00'),
           [simple_site(7, n_rx=7)], icc + (note(5), note(6)), (SLOTS[4],), None,
           [ROOM], set())
    yield ('cc_remainder_3_matched', pd.Timestamp('2024-03-21 12:00:00'),
           [simple_site(8, n_rx=8)], icc + (note(5), note(7)), (SLOTS[4], SLOTS[7]), None,
           [ROOM], set())
    yield ('cc_remainder_3_missed', pd.Timestamp('2024-03-21 12:00:00'),
           [simple_site(8, n_rx=8)], icc + (note(5), note(7)), (SLOTS[4],), None,
           [ROOM], {'cc_too_few'})
    yield ('cc_short_course_matched', pd.Timestamp('2024-03-18 12:00:00'),
           [simple_site(6, n_rx=6)], icc + (note(5),), (SLOTS[4],), None, [ROOM], set())
    yield ('cc_icc_charged_fx1', SLOTS[0] + pd.Timedelta(hours=3), [simple_site(1)],
           (note(0, 'Initial eChart Check'),), (SLOTS[0] + pd.Timedelta(hours=2),), None,
           [ROOM], set())
    yield ('not_started', pd.Timestamp('2024-03-01 12:00:00'), [simple_site(0)],
           (), (), None, [ROOM], set())
    lr_tx_l, lr_tx_r = SLOTS[0:5:2], SLOTS[1:6:2]
    yield ('prostate_lr_merged', SLOTS[5] + pd.Timedelta(hours=3),
           [dict(sit=11, pcp=21, name='Prostate L', rx=(200.0, 5, 1000.0),
                 tx=lr_tx_l, cal=SLOTS[0:10:2], rooms=ROOM),
            dict(sit=12, pcp=21, name='Prostate R', rx=(200.0, 5, 1000.0),
                 tx=lr_tx_r, cal=SLOTS[1:11:2], rooms=ROOM)],
           (note(0, 'Initial eChart Check'),), (), None, [ROOM],
           {'wcc_due', 'wcc_next_fx', 'cc_too_few'})
    yield ('multi_room_site', SLOTS[0] + pd.Timedelta(hours=3),
           [dict(sit=11, pcp=21, name='Test site', rx=(200.0, 15, 3000.0),
                 tx=SLOTS[:1], cal=SLOTS[:15],
                 rooms=[ROOM if i % 2 == 0 else ROOM2 for i in range(15)])],
           (), (), make_cfg([dict(label='Test 1', room=ROOM, active=True),
                             dict(label='Test 2', room=ROOM2, active=True)]),
           [ROOM, ROOM2], {'icc_due', 'wcc_due'})


def user_case():
    """The case the USER_* variables describe, in the tuple form cases() yields.

    Returns:
        tuple: (case_name, now, site_specs, notes, charges, cfg, locations, expected finding ids).
    """
    specs = []
    for i, site in enumerate(USER_SITES):
        n = site['n_fractions']
        specs.append(dict(
            sit=11 + i,
            pcp=21,
            name=site['name'],
            rx=(site['fx_dose_cgy'], n, site['fx_dose_cgy'] * n),
            tx=SLOTS[:site['n_treated']],
            cal=SLOTS[:n],
            rooms=USER_ROOM,
            rbe=site.get('rbe', True)))
    last_treated = max(site['n_treated'] for site in USER_SITES)
    if last_treated:
        now = SLOTS[last_treated - 1] + pd.Timedelta(hours=USER_HOURS_AFTER_LAST_FRACTION)
    else:
        now = SLOTS[0] - pd.Timedelta(days=1)
    notes = tuple(note(fx - 1, subject, hours) for fx, hours, subject in USER_NOTES)
    charges = tuple(SLOTS[fx - 1] for fx in USER_CHARGES)
    machine = dict(label=USER_ROOM_LABEL, room=USER_ROOM, active=True)
    for key, value in (('icc_missed_fx', USER_ICC_MISSED_FX),
                       ('sbrt_fx_dose_cgy', USER_SBRT_FX_DOSE_CGY),
                       ('sbrt_missed_fx', USER_SBRT_MISSED_FX),
                       ('icc_counts_as_wcc', USER_ICC_COUNTS_AS_WCC)):
        if value is not None:
            machine[key] = value
    cfg = dict(general=dict(qcl_task_types=[], cc_cpt_code=USER_CC_CPT_CODE), machines=[machine])
    return (USER_CASE_NAME, now, specs, notes, charges, cfg, [USER_ROOM],
            set(USER_EXPECTED_FINDINGS))


def build_case(case, out_root):
    """Verify one case's findings and write its directory under out_root.

    Args:
        case (tuple): A cases() / user_case() tuple.
        out_root (Path): Parent directory; the case directory is out_root / case_name.

    Returns:
        Path: The written case directory.
    """
    name, now, specs, notes, charges, cfg, locations, expected = case
    db_pull, cfg, merged = make_pull(now, specs, notes, charges, cfg)

    # Pin config for the render so generation is machine-independent.
    with pinned_config(merged):
        courses = build_courses(db_pull, [1])
        found = {w['id'] for c in courses for s in c.sites for w in s.warnings}
        assert found == expected, f'{name}: findings {sorted(found)} != {sorted(expected)}'
        rows = build_table(courses, WINDOW, locations)
    assert rows, f'{name}: no rows rendered'
    state = snapshot.case_state(locations, WINDOW, now)
    return snapshot.write_test_case(out_root / name, name, db_pull,
                                    snapshot.strip_session_keys(rows), state, clinic_cfg=cfg)


def main(argv=None):
    """Build the case directories under tests/snapshots.

    With --user, builds only the case the USER_* variables describe; otherwise every built-in
    case from cases().
    """
    argv = sys.argv[1:] if argv is None else argv
    todo = [user_case()] if '--user' in argv else list(cases())
    written = []
    for case in todo:
        build_case(case, snapshot.SNAPSHOT_DIR)
        written.append(case[0])
    print('\n'.join(written))
    print(f'{len(written)} case(s) written to {snapshot.SNAPSHOT_DIR}')


@contextlib.contextmanager
def pinned_config(merged):
    """Pin config.load_config and config.clinic_defaults to merged for the block.

    The live functions are restored on exit, even on error.

    Args:
        merged (dict[str, dict | list[dict]]): Case config layered over the code defaults.
    """
    live_load, live_defaults = config.load_config, config.clinic_defaults
    config.load_config = lambda m=merged: dict(m)
    config.clinic_defaults = lambda m=merged: dict(m)
    try:
        yield
    finally:
        config.load_config, config.clinic_defaults = live_load, live_defaults


if __name__ == '__main__':
    main()
