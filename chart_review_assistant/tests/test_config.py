# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the layered config: _merge, load_config, active_machines, the settings maps,
toml_text, and save_settings; one test per Settings control (general, location, each check,
per-machine thresholds, room, and each database field); the settings_apply and db_settings_apply
callbacks, driven with the dialog's control values; clinic_config_problem; the Save/Load config
buttons; plus demo-mode and de-id state path isolation. Every test
writes temp files only; the live clinic_config.toml, user_config.toml and db_config.toml are never
written (tests that build the Dash app read the live files through app's import-time defaults).

How the config works (see config.py):
- load_config layers code defaults < clinic_config.toml < user_config.toml.
- The Settings dialog never writes clinic_config.toml. save_settings writes to user_config.toml
  only the values that differ from the clinic baseline, so reverting a control removes its
  override.
- Machines are matched across layers by room; the active flag decides which are on the board.

Test layout, in file order:
- _merge, load_config, active_machines, settings maps, toml_text: the config building blocks
- save_settings: what the Settings dialog writes to user_config.toml
- Settings controls: one test per control, through save_settings / save_db_config
- settings_apply / db_settings_apply: the Dash callbacks the dialog actually fires
- Demo mode: path redirects, checked in a subprocess

Fixtures redirect the config file paths to pytest's tmp_path. Helpers are at the end of the file.
Run: python -m pytest chart_review_assistant/tests -q
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import chart_review_assistant
from chart_review_assistant import app, config
from chart_review_assistant.checks import CHECKS

# Every check id, in CHECKS order: the enabled_checks value of a machine with all boxes ticked
ALL_CHECKS = [cid for g in CHECKS.values() for cid in g]

# Per-machine Settings controls and a non-default value for each
MACHINE_CONTROLS = [
    ('label', 'Linac 1'),
    ('icc_missed_fx', 4),
    ('sbrt_fx_dose_cgy', 800),
    ('sbrt_missed_fx', 2),
    ('icc_counts_as_wcc', not config.ICC_COUNTS_AS_WCC_DEFAULT),
]

# Repo root: the subprocess imports the package from here.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Not valid TOML: used to check that a malformed file is never overwritten
BAD_TOML = 'not = [valid'


@pytest.fixture
def cfg_files(tmp_path, monkeypatch):
    """Point the clinic and user config files at a temp dir and return their paths.

    Neither file exists until a test writes it, so a test starts from the code defaults.
    """
    clinic = tmp_path / 'clinic_config.toml'
    user = tmp_path / 'user_config.toml'
    monkeypatch.setattr(config, 'CLINIC_CONFIG_FILE', str(clinic))
    monkeypatch.setattr(config, 'USER_CONFIG_FILE', str(user))
    return clinic, user


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    """Point db_config.toml at a temp dir and return its path."""
    path = tmp_path / 'db_config.toml'
    monkeypatch.setattr(config, 'DB_CONFIG_FILE', str(path))
    return path


@pytest.fixture
def callbacks(monkeypatch):
    """Build the Dash app and return its Settings callbacks by name.

    The callbacks are unwrapped from Dash's context wrapper. Logging setup is skipped so no log
    file is opened.

    The callbacks are closures inside create_app, so they are looked up in the app's callback_map
    by an output id. Dash wraps each with functools.wraps; __wrapped__ is the plain function.
    """
    monkeypatch.setattr(app, 'configure_logging', lambda: None)
    callback_map = app.create_app().callback_map

    def find(output):
        """The callback whose Output list contains exactly output.

        Dash keys a multi-output callback as '..id.prop...id.prop..', a single one as 'id.prop'.
        """
        return next(v['callback'].__wrapped__ for k, v in callback_map.items()
                    if output in k.strip('.').split('...'))

    return dict(
        settings_apply=find('settings_applied.data'),
        db_settings_apply=find('db_settings_status.children'),
        config_file_buttons=find('config_file_status.children'),
    )


# _merge: how one config layer is laid over another


def test_merge_general_keywise():
    """Override general keys replace base keys; other base keys are kept."""
    out = config._merge(dict(general=dict(a=1, b=2)), dict(general=dict(b=3)))
    assert out['general'] == dict(a=1, b=3)


def test_merge_machines_by_room():
    """Override machines update the base machine with the same room, in override order.

    The unmatched base machines follow them.
    """
    base = dict(machines=[
        dict(room='A', label='a', icc_missed_fx=6),
        dict(room='B', label='b', icc_missed_fx=6)])
    out = config._merge(base, dict(machines=[dict(room='B', label='bb')]))
    assert out['machines'] == [
        dict(room='B', label='bb', icc_missed_fx=6),
        dict(room='A', label='a', icc_missed_fx=6)]


def test_merge_machine_without_room_matches_index():
    """A legacy override machine without a room updates the base machine at its index."""
    base = dict(machines=[dict(room='A', label='a'), dict(room='B', label='b')])
    out = config._merge(base, dict(machines=[dict(label='x')]))
    assert out['machines'] == [dict(room='A', label='x'), dict(room='B', label='b')]


def test_merge_new_room_added():
    """An override machine whose room is not in the base is added."""
    base = dict(machines=[dict(room='A')])
    out = config._merge(base, dict(machines=[dict(room='C', label='c')]))
    assert out['machines'] == [dict(room='C', label='c'), dict(room='A')]


def test_merge_caps_machines():
    """Merged machines are capped at MAX_MACHINES."""
    over = dict(machines=[dict(room=f'R{i}') for i in range(config.MAX_MACHINES + 2)])
    out = config._merge(dict(machines=[]), over)
    assert len(out['machines']) == config.MAX_MACHINES


def test_merge_does_not_mutate_base():
    """The base config is deep-copied, not modified."""
    base = dict(general=dict(a=1), machines=[dict(room='A', label='a')])
    out = config._merge(base, dict(general=dict(a=2)))

    # Editing a merged machine must not reach the base's machine dict
    out['machines'][0]['label'] = 'changed'
    assert base == dict(general=dict(a=1), machines=[dict(room='A', label='a')])


# load_config: the merged config the app reads


def test_load_config_layers(cfg_files):
    """Code defaults < clinic_config.toml < user_config.toml"""
    clinic, user = cfg_files
    _write(clinic, dict(general=dict(refresh_seconds=30, cc_cpt_code='X1')))
    _write(user, dict(general=dict(refresh_seconds=90)))
    general = config.load_config()['general']

    # User beats clinic, clinic beats code defaults, untouched keys keep the code default
    assert general['refresh_seconds'] == 90
    assert general['cc_cpt_code'] == 'X1'
    assert general['track_cc_qcls'] == config.TRACK_CC_QCLS_DEFAULT


def test_load_config_missing_files(cfg_files):
    """With no config files the code defaults are returned."""
    assert config.load_config() == config.default_config()


def test_load_config_unreadable_file(cfg_files):
    """A malformed config file is treated as empty."""
    clinic, _ = cfg_files
    clinic.write_text(BAD_TOML, encoding='utf-8')
    assert config.load_config() == config.default_config()


def test_load_config_keeps_order(cfg_files):
    """Machines keep their config order; the active flag alone picks the board rooms."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[
        dict(room='A', active=False),
        dict(room='B', active=True),
        dict(room='C', active=True)]))
    cfg = config.load_config()
    assert [m['room'] for m in cfg['machines']] == ['A', 'B', 'C']
    assert [m['room'] for m in config.active_machines(cfg)] == ['B', 'C']


def test_load_config_clamps_hand_edits(cfg_files):
    """Hand-edited refresh_seconds and thresholds are clamped as the Settings dialog clamps them.

    Zero falls back to the default, a negative value clamps to the floor.
    """
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    _write(user, dict(
        general=dict(refresh_seconds=1),
        machines=[dict(room='R1', icc_missed_fx=0, sbrt_fx_dose_cgy=-100, sbrt_missed_fx=-2)]))
    cfg = config.load_config()
    assert cfg['general']['refresh_seconds'] == config.REFRESH_SECONDS_MIN
    th = config.settings_thresholds_map(cfg)['R1']
    assert th['icc_missed_fx'] == config.ICC_MISSED_FX_DEFAULT
    assert th['sbrt_fx_dose_cgy'] == 1
    assert th['sbrt_missed_fx'] == 1


def test_load_config_drops_clinic_deleted_machine(cfg_files):
    """A user entry for a room the clinic deleted is dropped; a user-added location is kept."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[
        dict(room='R1', label='Room 1', active=True),
        dict(room='R2', label='Room 2', active=True)]))

    # R3 is a user-added location with no label; it is written with its label
    config.save_settings(dict(), [_slot('R1', 'Room 1'), _slot('R2', 'Room 2'), _slot('R3', '')])
    assert [m['room'] for m in config.active_machines(config.load_config())] == ['R1', 'R2', 'R3']

    # The clinic deletes R2
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    assert [m['room'] for m in config.active_machines(config.load_config())] == ['R1', 'R3']


# active_machines and the settings maps: which rooms are on the board and with what settings


def test_active_machines_by_flag():
    """Machines with a true or absent active flag are active; false is inactive."""
    cfg = dict(general=dict(), machines=[
        dict(room='A', active=True), dict(room='B'), dict(room='C', active=False)])
    assert [m['room'] for m in config.active_machines(cfg)] == ['A', 'B']


def test_settings_enabled_map():
    """Active rooms map to their enabled checks; no enabled_checks key enables every check."""
    cfg = dict(machines=[
        dict(room='A', active=True, enabled_checks=['icc_due']),
        dict(room='B', active=True),
        dict(room='C', active=False, enabled_checks=['icc_due'])])
    assert config.settings_enabled_map(cfg) == dict(A={'icc_due'}, B=set(ALL_CHECKS))


def test_settings_thresholds_map_defaults():
    """Thresholds fall back to the code defaults when a machine omits them."""
    cfg = dict(machines=[dict(room='A', active=True, icc_missed_fx=4)])
    assert config.settings_thresholds_map(cfg) == dict(A=dict(
        icc_missed_fx=4,
        sbrt_fx_dose_cgy=config.SBRT_FX_DOSE_CGY_DEFAULT,
        sbrt_missed_fx=config.SBRT_MISSED_FX_DEFAULT,
        icc_counts_as_wcc=config.ICC_COUNTS_AS_WCC_DEFAULT))


# toml_text: the in-house TOML writer


def test_toml_text_round_trip():
    """toml_text output parses back to the same config, including escaped strings."""
    cfg = dict(
        general=dict(
            refresh_seconds=60,
            ratio=1.5,
            track_cc_qcls=False,
            cc_cpt_code='A"B\\C',
            note='line1\nline2\ttab',
            qcl_task_types=['Initial Physics Check', 'Weekly Physics Check'],
            skip_mrns=[]),
        machines=[
            dict(room='A', label='Room "A"', active=True, enabled_checks=['icc_due']),
            dict(room='B', label='', active=False, icc_missed_fx=4)])
    assert tomllib.loads(config.toml_text(cfg)) == cfg


# save_settings: what the Settings dialog writes to user_config.toml. _slot builds one location
# slot as the dialog passes it, with every value at the code default unless overridden


def test_save_settings_general_overrides_only(cfg_files):
    """General values equal to the clinic baseline are not written; changed ones are.

    Setting one back to the baseline removes it.
    """
    clinic, user = cfg_files
    _write(clinic, dict(general=dict(refresh_seconds=60, track_cc_qcls=True)))

    # Only the changed value is written
    config.save_settings(dict(refresh_seconds=60, track_cc_qcls=False), [])
    assert _read(user)['general'] == dict(track_cc_qcls=False)

    # Changing it back removes the override
    config.save_settings(dict(refresh_seconds=60, track_cc_qcls=True), [])
    assert _read(user).get('general', {}) == {}


def test_save_settings_keeps_unmanaged_keys(cfg_files):
    """User keys the dialog does not manage are kept, in general and per machine."""
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1')]))

    # feedback_file and note are hand-edited keys the dialog has no control for
    _write(user, dict(
        general=dict(feedback_file='fb.log'),
        machines=[dict(room='R1', note='x')]))
    config.save_settings(dict(refresh_seconds=60), [_slot('R1', 'Room 1')])
    saved = _read(user)
    assert saved['general'] == dict(feedback_file='fb.log')
    assert saved['machines'] == [dict(room='R1', active=True, note='x')]


def test_save_settings_keeps_other_tables(cfg_files):
    """A hand-added top-level table in user_config.toml survives a Settings save."""
    _, user = cfg_files
    user.write_text('[general]\nrefresh_seconds = 90\n\n[ui]\ntheme = "dark"\nscale = 1.5\n',
                    encoding='utf-8')
    config.save_settings(dict(refresh_seconds=120), [])
    saved = _read(user)
    assert saved['ui'] == dict(theme='dark', scale=1.5)
    assert saved['general'] == dict(refresh_seconds=120)


def test_toml_text_long_lists_one_per_line():
    """A list of more than three items is written one item per line; shorter lists stay inline."""
    text = config.toml_text(dict(general=dict(short=[1, 2, 3], long=['a', 'b', 'c', 'd'])))
    assert 'short = [1, 2, 3]\n' in text
    assert 'long = [\n    "a",\n    "b",\n    "c",\n    "d",\n]\n' in text
    assert tomllib.loads(text)['general'] == dict(short=[1, 2, 3], long=['a', 'b', 'c', 'd'])


def test_save_clinic_config_freezes_effective_settings(cfg_files):
    """Save Clinic Config writes the clinic baseline with the user's overrides applied, every key
    included, beside the live clinic file and never over it; the result loads as a clinic file.
    """
    clinic, user = cfg_files
    _write(clinic, dict(
        general=dict(cc_cpt_code='X1', skip_mrns=['1', '2', '3', '4']),
        machines=[dict(room='R1', label='Room 1', active=True)]))
    config.save_settings(dict(refresh_seconds=120),
                         [_slot('R1', 'Linac 1', icc_missed_fx=4, enabled_checks=['wcc_due'])])
    path = config.save_clinic_config()
    assert path == str(clinic.parent / 'clinic_config.saved.toml')
    assert _read(clinic) == dict(
        general=dict(cc_cpt_code='X1', skip_mrns=['1', '2', '3', '4']),
        machines=[dict(room='R1', label='Room 1', active=True)])
    saved = _read(path)
    assert saved['general']['cc_cpt_code'] == 'X1'
    assert saved['general']['skip_mrns'] == ['1', '2', '3', '4']
    assert saved['general']['refresh_seconds'] == 120
    assert saved['machines'] == [dict(
        room='R1', label='Linac 1', active=True, icc_missed_fx=4, enabled_checks=['wcc_due'])]

    # Used as the clinic file with no user file, it reproduces the same board settings
    user.unlink()
    (clinic.parent / 'clinic_config.saved.toml').replace(clinic)
    cfg = config.load_config()
    assert config.settings_enabled_map(cfg) == dict(R1={'wcc_due'})
    assert config.settings_thresholds_map(cfg)['R1']['icc_missed_fx'] == 4
    assert cfg['general']['refresh_seconds'] == 120


def test_save_settings_machine_overrides_only(cfg_files):
    """Machine values equal to the clinic baseline are omitted; room and active are always written.

    enabled_checks compares as a set.
    """
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', icc_missed_fx=6)]))

    # Same checks in a different order: no override
    slot = _slot('R1', 'Room 1', enabled_checks=list(reversed(ALL_CHECKS)))
    config.save_settings(dict(), [slot])
    assert _read(user)['machines'] == [dict(room='R1', active=True)]

    # Changed label and threshold: only those are written
    config.save_settings(dict(), [_slot('R1', 'Linac 1', icc_missed_fx=4)])
    assert _read(user)['machines'] == [
        dict(room='R1', active=True, label='Linac 1', icc_missed_fx=4)]


def test_save_settings_skips_empty_slots(cfg_files):
    """Slots with no room, no label and not active are not written."""
    _, user = cfg_files
    config.save_settings(dict(), [_slot('R1', 'Room 1'), _slot('', '', active=False)])
    assert [m['room'] for m in _read(user)['machines']] == ['R1']


def test_save_settings_renamed_room_inactive(cfg_files):
    """A clinic room no slot carries any more is written inactive, so it stays off the board."""
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))

    # The only slot's room is changed from R1 to R9
    config.save_settings(dict(), [_slot('R9', 'Room 1')])
    assert _read(user)['machines'] == [
        dict(room='R9', active=True, label='Room 1'),
        dict(room='R1', active=False)]
    assert [m['room'] for m in config.active_machines(config.load_config())] == ['R9']


def test_save_settings_round_trip(cfg_files):
    """After save_settings, load_config returns the dialog values."""
    clinic, _ = cfg_files
    _write(clinic, dict(
        general=dict(refresh_seconds=60),
        machines=[dict(room='R1', label='Room 1'), dict(room='R2', label='Room 2')]))

    # R2 moved first with changed settings, R1 removed from the board
    slots = [
        _slot('R2', 'Room 2', sbrt_missed_fx=2, enabled_checks=['wcc_due']),
        _slot('R1', 'Room 1', active=False)]
    config.save_settings(dict(refresh_seconds=120), slots)
    cfg = config.load_config()
    assert cfg['general']['refresh_seconds'] == 120
    assert config.settings_enabled_map(cfg) == dict(R2={'wcc_due'})
    assert config.settings_thresholds_map(cfg)['R2']['sbrt_missed_fx'] == 2


@pytest.mark.parametrize('bad', ['clinic', 'user'])
def test_save_settings_malformed_file_not_overwritten(cfg_files, bad):
    """A config file that exists but cannot be parsed raises and the user file is unchanged."""
    clinic, user = cfg_files
    if bad == 'clinic':
        clinic.write_text(BAD_TOML, encoding='utf-8')
        _write(user, dict(general=dict(refresh_seconds=90)))
    else:
        user.write_text(BAD_TOML, encoding='utf-8')
    before = user.read_text(encoding='utf-8')
    with pytest.raises(tomllib.TOMLDecodeError):
        config.save_settings(dict(refresh_seconds=30), [])
    assert user.read_text(encoding='utf-8') == before


# Settings controls: one test per control, through save_settings / save_db_config. Each sets the
# control, checks the value loads back, and where it applies, checks that reverting it removes
# the override


def test_settings_location_toggle(cfg_files):
    """Removing a location (active off) takes it off the board."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[
        dict(room='R1', label='Room 1', active=True),
        dict(room='R2', label='Room 2', active=True)]))

    # Remove Location: the first slot goes inactive
    config.save_settings(dict(), [
        _slot('R1', 'Room 1', active=False), _slot('R2', 'Room 2')])
    cfg = config.load_config()
    assert [m['room'] for m in config.active_machines(cfg)] == ['R2']


@pytest.mark.parametrize('cid', ALL_CHECKS)
def test_settings_check_toggle(cfg_files, cid):
    """Unchecking each check removes only that check from its room; rechecking clears the override.
    """
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))

    # Untick this check only
    off = [c for c in ALL_CHECKS if c != cid]
    config.save_settings(dict(), [_slot('R1', 'Room 1', enabled_checks=off)])
    assert config.settings_enabled_map(config.load_config()) == dict(R1=set(off))

    # Tick it again: all checks back on and no enabled_checks override left
    config.save_settings(dict(), [_slot('R1', 'Room 1')])
    assert config.settings_enabled_map(config.load_config()) == dict(R1=set(ALL_CHECKS))
    assert 'enabled_checks' not in _read(user)['machines'][0]


def test_settings_ignores_unknown_clinic_checks(cfg_files):
    """Clinic check ids this version lacks do not make matching boxes an enabled_checks override."""
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True,
                                       enabled_checks=['wcc_due', 'no_such_check'])]))
    config.save_settings(dict(), [_slot('R1', 'Room 1', enabled_checks=['wcc_due'])])
    assert 'enabled_checks' not in _read(user)['machines'][0]


@pytest.mark.parametrize('key, value', MACHINE_CONTROLS)
def test_settings_machine_toggle(cfg_files, key, value):
    """Each per-machine Settings control saves an override and clears when reverted.

    The override reaches the merged machine and the thresholds map.
    """
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))

    # Set the control. Set directly, since label is also a _slot positional argument
    slot = _slot('R1', 'Room 1')
    slot[key] = value
    config.save_settings(dict(), [slot])
    cfg = config.load_config()
    assert cfg['machines'][0][key] == value

    # label is not a threshold, so only the thresholds are checked in the map
    thresholds = config.settings_thresholds_map(cfg)['R1']
    if key in thresholds:
        assert thresholds[key] == value

    # Revert: only room and active remain
    config.save_settings(dict(), [_slot('R1', 'Room 1')])
    assert _read(user)['machines'] == [dict(room='R1', active=True)]


def test_settings_room_toggle(cfg_files):
    """Changing a location's room moves the board and its thresholds to the new room."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    config.save_settings(dict(), [_slot('R5', 'Room 1', icc_missed_fx=4)])
    cfg = config.load_config()
    assert set(config.settings_enabled_map(cfg)) == {'R5'}
    assert config.settings_thresholds_map(cfg)['R5']['icc_missed_fx'] == 4


@pytest.mark.parametrize('field, value', [
    ('host', 'db-host'),
    ('database', 'db-name'),
    ('port', 1500),
    ('username', 'db-user'),
    ('password', 'p"w\\d'),
])
def test_settings_db_field(db_file, field, value):
    """Each database Settings field is written to db_config.toml and loads back."""
    fields = dict(host='h', database='d', port=1433, username='', password='')
    fields[field] = value
    chart_review_assistant.save_db_config(**fields)
    assert chart_review_assistant.load_db_config()[field] == value


def test_settings_db_blank_port_default(db_file, monkeypatch):
    """A blank port is not written; the connection uses 1433."""

    # Stub the ODBC driver lookup so the test does not need a driver installed
    monkeypatch.setattr(chart_review_assistant, '_get_sql_server_driver', lambda: 'TEST')
    chart_review_assistant.save_db_config('h', 'd', None)
    assert 'port' not in chart_review_assistant.load_db_config()
    assert chart_review_assistant._build_url().port == 1433


def test_settings_db_windows_auth(db_file, monkeypatch):
    """Blank username/password are not written and select Windows authentication."""

    # Stub the ODBC driver lookup so the test does not need a driver installed
    monkeypatch.setattr(chart_review_assistant, '_get_sql_server_driver', lambda: 'TEST')
    chart_review_assistant.save_db_config('h', 'd', 1433, '', '')
    assert set(chart_review_assistant.load_db_config()) == {'host', 'port', 'database'}
    url = chart_review_assistant._build_url()
    assert url.query['trusted_connection'] == 'yes'
    assert url.username is None


def test_settings_db_sql_auth(db_file, monkeypatch):
    """A username and password select SQL authentication."""

    # Stub the ODBC driver lookup so the test does not need a driver installed
    monkeypatch.setattr(chart_review_assistant, '_get_sql_server_driver', lambda: 'TEST')
    chart_review_assistant.save_db_config('h', 'd', 1433, 'u', 'p')
    url = chart_review_assistant._build_url()
    assert 'trusted_connection' not in url.query
    assert (url.username, url.password) == ('u', 'p')


def test_settings_db_unconfigured(db_file):
    """A missing db_config.toml or a blank host or database reads as not configured."""
    assert not chart_review_assistant.db_configured()
    chart_review_assistant.save_db_config('', 'd', 1433)
    assert not chart_review_assistant.db_configured()
    chart_review_assistant.save_db_config('h', 'd', 1433)
    assert chart_review_assistant.db_configured()


def test_settings_db_malformed_file_tolerated(db_file, monkeypatch):
    """A malformed db_config.toml reads as {} / not configured instead of raising.

    The layout still builds; the strict connection path still raises.
    """
    db_file.write_text(BAD_TOML, encoding='utf-8')
    assert chart_review_assistant.load_db_config() == {}
    assert not chart_review_assistant.db_configured()
    with pytest.raises(tomllib.TOMLDecodeError):
        chart_review_assistant._load_db_config()

    # The Settings dialog (and so the whole layout) builds from the empty config
    monkeypatch.setattr(app, 'configure_logging', lambda: None)
    assert app.create_app().layout() is not None


def test_settings_db_save_is_atomic(db_file):
    """save_db_config swaps a temp file in: the file is complete and no .tmp is left behind."""
    chart_review_assistant.save_db_config('h', 'd', 1433)
    assert chart_review_assistant.load_db_config() == dict(host='h', port=1433, database='d')
    assert not db_file.with_suffix('.toml.tmp').exists()


def test_clinic_config_problem(cfg_files):
    """A missing, unparseable or machine-less clinic_config.toml is reported for the banner; a
    usable one is not.
    """
    clinic, _ = cfg_files
    assert 'not found' in app.clinic_config_problem()
    clinic.write_text(BAD_TOML, encoding='utf-8')
    assert 'could not be read' in app.clinic_config_problem()
    _write(clinic, dict(general=dict(refresh_seconds=60)))
    assert 'no [[machines]]' in app.clinic_config_problem()
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    assert app.clinic_config_problem() == ''


# settings_apply / db_settings_apply: the Dash callbacks the Settings dialog fires on every
# change. _apply_vals builds the callback's positional arguments from machine config dicts, the
# same way the dialog seeds its controls, so these also check how the callback reads the controls


def test_settings_apply_baseline(cfg_files, callbacks):
    """Applying the baseline control values writes no machine overrides and bumps settings_applied.
    """
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))

    # Returns (status message, settings_applied): no error, counter 0 -> 1
    out = callbacks['settings_apply'](*_apply_vals([dict(room='R1', label='Room 1')], 1))
    assert out == ('', 1)
    assert _read(user)['machines'] == [dict(room='R1', active=True)]


@pytest.mark.parametrize('cid', ALL_CHECKS)
def test_settings_apply_check(cfg_files, callbacks, cid):
    """Unchecking each check's box removes only that check for its room."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    off = [c for c in ALL_CHECKS if c != cid]
    callbacks['settings_apply'](
        *_apply_vals([dict(room='R1', label='Room 1', enabled_checks=off)], 1))
    assert config.settings_enabled_map(config.load_config()) == dict(R1=set(off))


@pytest.mark.parametrize('key, raw, expected', [
    ('icc_missed_fx', None, config.ICC_MISSED_FX_DEFAULT),
    ('icc_missed_fx', 0, config.ICC_MISSED_FX_DEFAULT),
    ('icc_missed_fx', -3, 1),
    ('sbrt_fx_dose_cgy', None, config.SBRT_FX_DOSE_CGY_DEFAULT),
    ('sbrt_fx_dose_cgy', -3, 1),
    ('sbrt_missed_fx', None, config.SBRT_MISSED_FX_DEFAULT),
    ('sbrt_missed_fx', -3, 1),
])
def test_settings_apply_threshold_bounds(cfg_files, callbacks, key, raw, expected):
    """A blank or zero threshold box falls back to the default; a negative one clamps to 1."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))

    # raw is the number box's value; None is a cleared box
    m = dict(room='R1', label='Room 1')
    m[key] = raw
    callbacks['settings_apply'](*_apply_vals([m], 1))
    assert config.settings_thresholds_map(config.load_config())['R1'][key] == expected


def test_settings_apply_strips_label_room(cfg_files, callbacks):
    """Label and room are saved stripped of surrounding whitespace."""
    callbacks['settings_apply'](*_apply_vals([dict(room='  R5 ', label=' Linac 5  ')], 1))
    m = config.load_config()['machines'][0]
    assert (m['room'], m['label']) == ('R5', 'Linac 5')


@pytest.mark.parametrize('raw, expected', [
    (120, 120),
    (None, config.REFRESH_SECONDS_DEFAULT),
    (1, config.REFRESH_SECONDS_MIN),
    (1000, config.REFRESH_SECONDS_MAX),
])
def test_settings_apply_refresh_seconds(cfg_files, callbacks, raw, expected):
    """The refresh box is saved, a blank box falls back to the default, and it clamps to the
    10 s .. 5 min range.
    """
    callbacks['settings_apply'](*_apply_vals([dict(room='R1')], 1, refresh_seconds=raw))
    assert config.load_config()['general']['refresh_seconds'] == expected


@pytest.mark.parametrize('raw, expected', [(['on'], True), ([], False)])
def test_settings_apply_track_qcls(cfg_files, callbacks, raw, expected):
    """The Track pending QCLs checkbox saves track_cc_qcls."""

    # A one-option dcc.Checklist: ['on'] when ticked, [] when not
    callbacks['settings_apply'](*_apply_vals([dict(room='R1')], 1, track_qcls=raw))
    assert config.load_config()['general']['track_cc_qcls'] is expected


def test_settings_apply_slot_flags(cfg_files, callbacks):
    """Each slot's active flag decides whether it is on the board, regardless of position."""

    # Fill every slot so the active flag, not an empty slot, decides what is on the board
    machines = [dict(room=f'R{i}', label=f'Room {i}') for i in range(config.MAX_MACHINES)]
    flags = [i in (0, 2) for i in range(config.MAX_MACHINES)]
    callbacks['settings_apply'](*_apply_vals(machines, flags))
    cfg = config.load_config()
    assert 'n_locations' not in cfg['general']
    assert [m['room'] for m in config.active_machines(cfg)] == ['R0', 'R2']


def test_settings_apply_counter(cfg_files, callbacks):
    """settings_applied counts up from its current value, starting at 1 when unset.

    The board re-applies settings whenever this counter changes.
    """
    vals = _apply_vals([dict(room='R1')], 1, applied=None)
    assert callbacks['settings_apply'](*vals)[1] == 1
    vals = _apply_vals([dict(room='R1')], 1, applied=4)
    assert callbacks['settings_apply'](*vals)[1] == 5


def test_settings_apply_save_failure(cfg_files, callbacks):
    """A malformed user_config.toml shows an error and leaves settings_applied unchanged.

    The file is not overwritten.
    """
    _, user = cfg_files
    user.write_text(BAD_TOML, encoding='utf-8')
    status, applied = callbacks['settings_apply'](*_apply_vals([dict(room='R1')], 1))

    # The error message is an html.Span; no_update tells Dash to leave the counter alone
    assert isinstance(status, app.html.Span)
    assert applied is app.no_update
    assert user.read_text(encoding='utf-8') == BAD_TOML


def test_settings_apply_rejects_duplicate_rooms(cfg_files, callbacks):
    """Two active locations with the same room show an error and nothing is saved."""
    _, user = cfg_files
    machines = [dict(room='R1', label='Room 1'), dict(room='R1', label='Room 1 again')]
    status, applied = callbacks['settings_apply'](*_apply_vals(machines, 2))
    assert isinstance(status, app.html.Span)
    assert applied is app.no_update
    assert not user.exists()

    # The same room on an inactive slot is fine
    assert callbacks['settings_apply'](*_apply_vals(machines, 1)) == ('', 1)


def test_db_settings_apply(db_file, callbacks):
    """The database fields are saved to db_config.toml with no error shown."""

    # Arguments: host, database, port, username, password; '' is no error message
    assert callbacks['db_settings_apply']('h', 'd', 1500, 'u', 'p') == ''
    assert chart_review_assistant.load_db_config() == dict(
        host='h', port=1500, database='d', username='u', password='p')


def test_db_settings_apply_failure(db_file, callbacks):
    """An invalid port shows an error and writes nothing."""
    status = callbacks['db_settings_apply']('h', 'd', 'abc', '', '')
    assert isinstance(status, app.html.Span)
    assert not db_file.exists()


def test_save_clinic_config_button(cfg_files, callbacks, monkeypatch):
    """With no desktop window the button writes clinic_config.saved.toml; no status on success."""
    clinic, _ = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    monkeypatch.setattr(app, 'ctx', SimpleNamespace(triggered_id='save_clinic_btn'))
    status, *_ = callbacks['config_file_buttons'](1, 0, 0)
    assert (clinic.parent / 'clinic_config.saved.toml').exists()
    assert status == ''


def test_load_clinic_config_button(cfg_files, callbacks, monkeypatch, tmp_path):
    """Load Clinic Config copies the picked file over clinic_config.toml; no status on success."""
    clinic, _ = cfg_files
    src = tmp_path / 'picked.toml'
    src.write_text('[general]\n')
    monkeypatch.setattr(app, 'ctx', SimpleNamespace(triggered_id='load_clinic_btn'))
    monkeypatch.setattr(app, '_ask_open_path', lambda: str(src))
    status, *_ = callbacks['config_file_buttons'](0, 0, 1)
    assert clinic.read_text() == '[general]\n'
    assert status == ''


def test_load_clinic_config_button_refills_settings(cfg_files, callbacks, monkeypatch, tmp_path):
    """Load Clinic Config returns the loaded locations and General values, not user overrides."""
    clinic, user = cfg_files
    _write(clinic, dict(machines=[dict(room='R1', label='Room 1', active=True)]))
    _write(user, dict(general=dict(refresh_seconds=300),
                      machines=[dict(room='R2', label='Mine', active=True)]))
    src = tmp_path / 'picked.toml'
    _write(src, dict(general=dict(refresh_seconds=120, track_cc_qcls=False),
                     machines=[dict(room='R2', label='Room 2', active=True)]))
    monkeypatch.setattr(app, 'ctx', SimpleNamespace(triggered_id='load_clinic_btn'))
    monkeypatch.setattr(app, '_ask_open_path', lambda: str(src))
    monkeypatch.setattr(app, '_confirm_replace', lambda path: True)
    result = callbacks['config_file_buttons'](0, 0, 1)
    flags, label, room = result[6:9]
    assert flags[0] and not any(flags[1:])
    assert (label, room) == ('Room 2', 'R2')
    assert result[-2:] == (120, [])


def test_load_db_config_button_refills_fields(db_file, callbacks, monkeypatch, tmp_path):
    """Load DB Config replaces db_config.toml and returns its values for the Database fields."""
    db_file.write_text('[database]\nhost = "h"\nport = 1433\ndatabase = "d"\n')
    src = tmp_path / 'picked'
    src.mkdir()
    src = src / 'db_config.toml'
    src.write_text('# site db\n[database]\nhost = "h2"\nport = 10\ndatabase = "d2"\n')
    monkeypatch.setattr(app, 'ctx', SimpleNamespace(triggered_id='load_db_btn'))
    monkeypatch.setattr(app, '_ask_open_path', lambda: str(src))
    monkeypatch.setattr(app, '_confirm_replace', lambda path: True)
    result = callbacks['config_file_buttons'](0, 1, 0)
    assert db_file.read_text() == src.read_text()
    assert result[:6] == ('', 'h2', 'd2', 10, '', '')

    # The refilled fields trigger db_settings_apply; matching the file, they leave it as copied
    assert callbacks['db_settings_apply'](*result[1:6]) is app.no_update
    assert db_file.read_text() == src.read_text()


# Demo mode: the import-time redirects that keep demo runs out of the live config files


def test_demo_mode_isolation(tmp_path):
    """In demo mode the config, state and DB paths resolve into demo_data, not the data dir."""
    paths = _app_paths(tmp_path, demo=True)
    demo_dir = Path(paths['demo_dir'])
    for key in ('clinic', 'user', 'db_config', 'state'):
        assert Path(paths[key]).parent == demo_dir, key
    assert Path(paths['engine_db']) == demo_dir / 'demo.db'

    # Nothing was written to the live data dir
    assert not any((tmp_path / 'data').glob('*.toml'))
    assert not (tmp_path / 'data' / 'app_state.json').exists()


def test_live_mode_uses_data_dir(tmp_path):
    """Without demo mode the config and state paths resolve into the data dir."""
    paths = _app_paths(tmp_path, demo=False)
    for key in ('clinic', 'user', 'db_config', 'state'):
        assert Path(paths[key]).parent == tmp_path / 'data', key
    assert Path(paths['state']).name == 'app_state.json'
    assert paths['engine_db'] is None


def test_deid_mode_uses_own_state_file(tmp_path):
    """De-id mode reads the live config but keeps its UI state in app_state.deid.json.

    This keeps a de-id session from overwriting the live user's saved state.
    """
    paths = _app_paths(tmp_path, demo=False, deid=True)
    for key in ('clinic', 'user', 'db_config'):
        assert Path(paths[key]).parent == tmp_path / 'data', key
    assert Path(paths['state']) == tmp_path / 'data' / 'app_state.deid.json'


def _app_paths(tmp_path, demo, deid=False):
    """Import app in a fresh interpreter and return its resolved config, state and DB paths.

    The demo/de-id redirects run at import time, so a subprocess keeps them out of this test
    session. CRA_DATA_DIR points the live data dir at a temp dir.

    Args:
        tmp_path (Path): Temp dir; its data subdir is the data dir.
        demo (bool): Whether CRA_DEMO_MODE is set.
        deid (bool): Whether CRA_DEID_MODE is set.

    Returns:
        dict[str, str | None]: Paths keyed clinic, user, db_config, state, engine_db (None when
            no engine is created at import) and demo_dir.
    """
    data = tmp_path / 'data'
    data.mkdir()

    # Start from a clean mode: drop any demo/de-id flags inherited from the shell
    env = dict(os.environ, CRA_DATA_DIR=str(data))
    env.pop('CRA_DEMO_MODE', None)
    env.pop('CRA_DEID_MODE', None)
    if demo:
        env['CRA_DEMO_MODE'] = '1'
    if deid:
        env['CRA_DEID_MODE'] = '1'

    # Child script: import the app and print the paths it resolved as one JSON line
    code = '\n'.join([
        'import json',
        'import chart_review_assistant',
        'from chart_review_assistant import app, config, demo_data',
        'eng = chart_review_assistant._engine',
        'print(json.dumps(dict(',
        '    clinic=config.CLINIC_CONFIG_FILE,',
        '    user=config.USER_CONFIG_FILE,',
        '    db_config=config.DB_CONFIG_FILE,',
        '    state=app.STATE_FILE,',
        '    engine_db=eng.url.database if eng is not None else None,',
        '    demo_dir=str(demo_data.DEMO_DIR))))',
    ])
    proc = subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr

    # The JSON is the last stdout line; earlier lines may be log output
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _slot(room, label, active=True, **kw):
    """A Settings dialog machine slot with baseline values, overridden by kw

    Matches the dict shape settings_apply passes to config.save_settings for one location.
    """
    slot = dict(
        label=label,
        room=room,
        active=active,
        enabled_checks=list(ALL_CHECKS),
        icc_missed_fx=config.ICC_MISSED_FX_DEFAULT,
        sbrt_fx_dose_cgy=config.SBRT_FX_DOSE_CGY_DEFAULT,
        sbrt_missed_fx=config.SBRT_MISSED_FX_DEFAULT,
        icc_counts_as_wcc=config.ICC_COUNTS_AS_WCC_DEFAULT,
    )
    slot.update(kw)
    return slot


def _apply_vals(machines, n, refresh_seconds=60, track_qcls=('on',), applied=0):
    """Positional settings_apply arguments, in the order of its Dash Inputs and State

    Each slot's control values come from app.baseline_values, as the Settings layout seeds them;
    slots past len(machines) are blank.

    Args:
        machines (list[dict[str, str | int | bool | list[str] | None]]): Machine config dicts
            for the first slots.
        n (int | list[bool]): Number of leading slots flagged active, or the slot_active_store
            flags.
        refresh_seconds (int | None): cfg_refresh_seconds value.
        track_qcls (list[str] | tuple[str, ...]): cfg_track_qcls checklist value.
        applied (int | None): settings_applied State value.

    Returns:
        list[str | int | list[str] | None]: The callback arguments.
    """

    # Per slot: label, room, one value per check checklist, the three threshold boxes, the WCC
    # checkbox. baseline_values returns the checklists as one list, so it is unpacked here
    vals = []
    for i in range(config.MAX_MACHINES):
        m = machines[i] if i < len(machines) else dict()
        label, room, checks, *rest = app.baseline_values(m)
        vals += [label, room, *checks, *rest]

    # Then the general controls, the slot flags, and the settings_applied State
    flags = n if isinstance(n, list) else [i < n for i in range(config.MAX_MACHINES)]
    return vals + [refresh_seconds, list(track_qcls), flags, applied]


def _write(path, cfg):
    """Write a config dict as TOML."""
    path.write_text(config.toml_text(cfg), encoding='utf-8')


def _read(path):
    """Parse a TOML file."""
    with open(path, 'rb') as f:
        return tomllib.load(f)
