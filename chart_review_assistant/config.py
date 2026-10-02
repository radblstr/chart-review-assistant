# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
User settings: layered TOML config for per-machine check toggles and general options.

- clinic_config.toml holds the clinic baseline (machines, refresh, feedback/bug paths). Not
  shipped. Template: clinic_config.example.toml
- user_config.toml holds the user's overrides. Not shipped. Written by the Settings dialog
  through save_settings, which keeps only values that differ from the clinic baseline.
- load_config() layers code defaults < clinic_config.toml < user_config.toml.
- settings_enabled_map turns the active machines into a {room: set(check ids)} lookup for
  build_table; settings_thresholds_map maps each room to its thresholds and toggles
  (machine_thresholds).
- clamp holds numeric settings inside their limits for load_config and the Settings dialog.

Both files sit next to app_state.json, as does db_config.toml (DB_CONFIG_FILE). tomllib reads
TOML; toml_text serializes the known config shape (tomli_w is not available).
"""

import copy
import logging
import os
import tomllib

from chart_review_assistant.checks import CHECKS

logger = logging.getLogger('cra')


def data_dir():
    """Per-user data directory (config, state, logs)

    CRA_DATA_DIR overrides, else the package dir
    """
    return os.environ.get('CRA_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))


def bug_report_dir():
    """Root dir for bug-report folders

    The config general.bug_report_dir if set (a central network share for the clinic deployment),
    else bug_reports under the data dir
    """
    path = load_config()['general'].get('bug_report_dir')
    return path or os.path.join(data_dir(), 'bug_reports')


# clinic_config.toml, user_config.toml and db_config.toml live in the data dir.
CLINIC_CONFIG_FILE = os.path.join(data_dir(), 'clinic_config.toml')
USER_CONFIG_FILE = os.path.join(data_dir(), 'user_config.toml')
DB_CONFIG_FILE = os.path.join(data_dir(), 'db_config.toml')

# Fallback refresh period (seconds) when no config file sets it
REFRESH_SECONDS_DEFAULT = 60

# Auto-refresh limits (seconds): the Settings box and clamp() keep refresh_seconds in this range.
REFRESH_SECONDS_MIN = 10
REFRESH_SECONDS_MAX = 300

# Up to this many locations may be configured.
MAX_MACHINES = 8

# Per-fraction RBE dose (cGy) at or above which a site is flagged SBRT
SBRT_FX_DOSE_CGY_DEFAULT = 500

# Treated fraction at or after which an initial chart check is considered missed
ICC_MISSED_FX_DEFAULT = 6

# Treated fraction at or after which an SBRT course's initial chart check is considered missed
SBRT_MISSED_FX_DEFAULT = 3

# Whether an on-time initial chart check counts as the course's first weekly chart check
ICC_COUNTS_AS_WCC_DEFAULT = True

# Whether the Pending QCLs column is shown on the board
TRACK_CC_QCLS_DEFAULT = True

# Ident field holding the patient MRN shown on the board, and the fields it may name
MRN_FIELD_DEFAULT = 'IDB'
MRN_FIELDS = ('IDA', 'IDB', 'IDC', 'IDD', 'IDE', 'IDF')

# Notes.Note_Type values read as chart-check notes
CC_NOTE_TYPES_DEFAULT = [50]

# Schedule.Activity LIKE patterns excluded as holds
HOLD_ACTIVITIES_DEFAULT = ['HLD%', 'HOLD%', 'DONOTSCHED']


def default_config():
    """Build the code-level default config.

    Machines and the clinic-specific paths live in clinic_config.toml (copied from the example
    template and distributed out of band); the code defaults carry only the general fallbacks so
    the app still starts when clinic_config.toml is absent.

    Returns:
        dict[str, dict | list[dict]]: {'general': {...}, 'machines': []}
    """
    return dict(
        general=dict(
            refresh_seconds=REFRESH_SECONDS_DEFAULT,
            skip_mrns=[],
            feedback_file='',
            bug_report_dir='',
            qcl_task_types=[],
            cc_cpt_code='',
            track_cc_qcls=TRACK_CC_QCLS_DEFAULT,
            mrn_field=MRN_FIELD_DEFAULT,
            cc_note_types=list(CC_NOTE_TYPES_DEFAULT),
            hold_activities=list(HOLD_ACTIVITIES_DEFAULT),
        ),
        machines=[],
    )


def load_config():
    """Return the merged config: code defaults < clinic_config.toml < user_config.toml.

    A user entry for a room the clinic baseline no longer has and with no label (a machine the
    clinic deleted) is dropped. refresh_seconds and the per-machine thresholds are clamped as the
    Settings dialog clamps them, so hand-edited values cannot fall outside its limits.

    Returns:
        dict[str, dict | list[dict]]: Merged config with 'general' and 'machines'
    """
    cfg = clinic_defaults()
    clinic_rooms = {m.get('room') for m in cfg.get('machines') or []}
    cfg = _merge(cfg, _read_toml(USER_CONFIG_FILE))
    machines = [m for m in cfg.get('machines') or []
                if m.get('room') in clinic_rooms or 'label' in m]
    cfg['machines'] = machines

    general = cfg['general']
    general['refresh_seconds'] = clamp('refresh_seconds', general['refresh_seconds'])
    for m in machines:
        for k in ('icc_missed_fx', 'sbrt_fx_dose_cgy', 'sbrt_missed_fx'):
            if k in m:
                m[k] = clamp(k, m[k])
    return cfg


def clinic_defaults():
    """Return the clinic baseline (code defaults < clinic_config.toml), without user overrides."""
    return _merge(default_config(), _read_toml(CLINIC_CONFIG_FILE))


def active_machines(cfg):
    """The machines active on the board: those whose active flag is true or absent

    The flag is the source of truth; activation does not depend on list order. Inactive machines
    are kept in the config only for the hidden Settings boxes and do not appear in the app.

    Args:
        cfg (dict[str, dict | list[dict]]): Config with 'general' and 'machines'

    Returns:
        list[dict]: Active machine entries
    """
    return [m for m in cfg.get('machines') or [] if m.get('active', True)]


def settings_enabled_map(cfg):
    """Map each active location's room to the set of check ids enabled for it.

    Active machines are those flagged active (see active_machines); extras kept in the config (for
    the hidden Settings boxes) do not affect the board. A machine without enabled_checks enables
    every check, matching the Settings boxes.

    Args:
        cfg (dict[str, dict | list[dict]]): Config with 'general' and 'machines'

    Returns:
        dict[str, set[str]]: Room to enabled check ids
    """
    all_checks = {cid for g in CHECKS.values() for cid in g}
    out = {}
    for m in active_machines(cfg):
        room = m.get('room')
        if room:
            enabled = m.get('enabled_checks')
            out[room] = all_checks if enabled is None else set(enabled)
    return out


def settings_thresholds_map(cfg):
    """Map each active location's room to its ICC-missed/SBRT thresholds and icc_counts_as_wcc.

    Values fall back to the code defaults when a machine omits them. Active machines are those
    flagged active (see active_machines).

    Args:
        cfg (dict[str, dict | list[dict]]): Config with 'general' and 'machines'

    Returns:
        dict[str, dict[str, int | bool]]: Room to machine_thresholds output
    """
    out = {}
    for m in active_machines(cfg):
        room = m.get('room')
        if room:
            out[room] = machine_thresholds(m)
    return out


def machine_thresholds(m):
    """A machine's ICC-missed/SBRT thresholds and icc_counts_as_wcc

    Absent keys take the code defaults.

    Args:
        m (dict[str, str | bool | int | list[str]]): Machine config entry

    Returns:
        dict[str, int | bool]: icc_missed_fx, sbrt_fx_dose_cgy, sbrt_missed_fx, icc_counts_as_wcc
    """
    return dict(
        icc_missed_fx=m.get('icc_missed_fx', ICC_MISSED_FX_DEFAULT),
        sbrt_fx_dose_cgy=m.get('sbrt_fx_dose_cgy', SBRT_FX_DOSE_CGY_DEFAULT),
        sbrt_missed_fx=m.get('sbrt_missed_fx', SBRT_MISSED_FX_DEFAULT),
        icc_counts_as_wcc=m.get('icc_counts_as_wcc', ICC_COUNTS_AS_WCC_DEFAULT))


def clamp(key, value):
    """Clamp a numeric setting to its limits; blank or 0 falls back to its default.

    Args:
        key (str): refresh_seconds, icc_missed_fx, sbrt_fx_dose_cgy or sbrt_missed_fx
        value (int | str | None): Raw value

    Returns:
        int: The clamped value
    """
    low, high, default = dict(
        refresh_seconds=(REFRESH_SECONDS_MIN, REFRESH_SECONDS_MAX, REFRESH_SECONDS_DEFAULT),
        icc_missed_fx=(1, None, ICC_MISSED_FX_DEFAULT),
        sbrt_fx_dose_cgy=(1, None, SBRT_FX_DOSE_CGY_DEFAULT),
        sbrt_missed_fx=(1, None, SBRT_MISSED_FX_DEFAULT))[key]
    out = max(low, int(value or default))
    return out if high is None else min(high, out)


def save_settings(general, machines):
    """Write the Settings dialog values to user_config.toml as overrides of the clinic baseline.

    Only values that differ from the clinic baseline are written, so later clinic_config.toml
    changes still reach settings the user never changed. User keys the dialog does not manage are
    kept. Machines match the clinic baseline by room, like _merge, so room and active are always
    written; active is the source of truth for which locations are on the board. A location the
    clinic baseline lacks also always writes its label (see load_config). Raises when
    either config file exists but cannot be parsed, so a hand-edited file is never overwritten.

    Args:
        general (dict[str, int | bool]): General settings from the dialog
        machines (list[dict[str, str | bool | int | list[str]]]): One settings dict per location
            slot, each with room, label and active
    """
    clinic = _merge(default_config(), _read_toml(CLINIC_CONFIG_FILE, strict=True))
    user = _read_toml(USER_CONFIG_FILE, strict=True)

    user_general = user.get('general') or {}
    for k, v in general.items():
        if v == clinic['general'].get(k):
            user_general.pop(k, None)
        else:
            user_general[k] = v
    user['general'] = user_general

    # Key the clinic and existing user machines by room; legacy user entries without a room
    # override the clinic machine at their index.
    cmachines = clinic.get('machines') or []
    cby_room = {m.get('room', ''): m for m in cmachines}
    uby_room = {}
    for i, um in enumerate(user.get('machines') or []):
        room = um.get('room', cmachines[i].get('room', '') if i < len(cmachines) else '')
        uby_room.setdefault(room, um)

    # Check ids this version does not know (from a clinic file of another version) have no box,
    # so they are left out of the baseline the dialog's checks are compared against.
    all_checks = [cid for g in CHECKS.values() for cid in g]
    out = []
    for m in machines:
        if not m['room'] and not m['label'] and not m['active']:
            continue
        cm = cby_room.get(m['room'], {})
        base = dict(
            label=cm.get('label', ''),
            enabled_checks=[cid for cid in cm.get('enabled_checks', all_checks)
                            if cid in all_checks],
            **machine_thresholds(cm))
        um = dict(uby_room.get(m['room'], {})) if m['room'] else {}
        for k, v in m.items():
            if k in ('room', 'active') or (
                    set(v) == set(base[k]) if k == 'enabled_checks' else v == base[k]):
                um.pop(k, None)
            else:
                um[k] = v

        # A location the clinic baseline lacks always carries its label, so load_config can tell
        # it from a machine the clinic deleted.
        if m['room'] not in cby_room:
            um['label'] = m['label']
        out.append(dict(room=m['room'], active=m['active'], **um))

    # A clinic location no slot carries any more (its room was renamed) stays off the board.
    slot_rooms = {m['room'] for m in machines}
    out += [dict(room=r, active=False) for r in cby_room if r and r not in slot_rooms]
    user['machines'] = out
    with open(USER_CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write(toml_text(user))


def saved_clinic_config_path():
    """Where Save Clinic Config writes: clinic_config.saved.toml beside the live clinic file.

    Resolved at call time so the demo-mode redirect of CLINIC_CONFIG_FILE is honoured.
    """
    return os.path.join(os.path.dirname(CLINIC_CONFIG_FILE), 'clinic_config.saved.toml')


def save_clinic_config(path=None):
    """Write the effective config as a complete clinic_config.toml for handing out.

    The effective config is the clinic baseline with the user's Settings overrides applied
    (load_config). Every key is written, Fixed ones included, so the file stands alone: rename it
    clinic_config.toml and it reproduces this install's board. Defaults to clinic_config.saved.toml
    beside the live file; the save dialog can pick any path.

    Args:
        path (str | None): Destination; default saved_clinic_config_path().

    Returns:
        str: The written path.
    """
    path = path or saved_clinic_config_path()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(toml_text(load_config()))
    os.replace(tmp, path)
    return path


def toml_text(cfg):
    """Serialize a {general, machines} config as TOML text (tomli_w is not available).

    Any other top-level table in cfg (a hand-added section in user_config.toml) is written after
    [general] so a Settings save never drops it.
    """
    lines = []
    general = cfg.get('general') or {}
    if general:
        lines.append('[general]')
        for k, v in general.items():
            lines.append(f'{k} = {_fmt(v)}')
        lines.append('')
    for name, table in cfg.items():
        if name in ('general', 'machines') or not isinstance(table, dict):
            continue
        lines.append(f'[{name}]')
        for k, v in table.items():
            lines.append(f'{k} = {_fmt(v)}')
        lines.append('')
    for m in cfg.get('machines') or []:
        lines.append('[[machines]]')
        for k, v in m.items():
            lines.append(f'{k} = {_fmt(v)}')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _read_toml(path, strict=False):
    """Parse a TOML file, returning {} when it is missing.

    A file that exists but cannot be read or parsed is logged and treated as empty, or re-raised
    when strict.

    Args:
        path (str | Path): TOML file path
        strict (bool): Re-raise a read or parse error instead of returning {}

    Returns:
        dict: Parsed TOML, or {} when missing or unreadable
    """
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error('could not read %s: %s', path, e)
        if strict:
            raise
        return {}


def _merge(base, over):
    """Layer over onto base: general merges key-wise, machines merge by room.

    Machines are capped at MAX_MACHINES. An override machine with a room key updates the base
    machine with that room; one without (legacy files) updates the base machine at its index.
    Merged machines keep the override order, followed by the unmatched base machines.

    Args:
        base (dict[str, dict | list[dict]]): Base config
        over (dict[str, dict | list[dict]]): Override config

    Returns:
        dict[str, dict | list[dict]]: Merged deep copy of base
    """
    out = copy.deepcopy(base)
    out.setdefault('general', {}).update(over.get('general') or {})
    omachines = over.get('machines')
    if omachines is not None:
        bmachines = out.get('machines') or []
        brooms = [bm.get('room') for bm in bmachines]
        used = set()
        merged = []
        for i, om in enumerate(omachines):
            if 'room' in om:
                j = brooms.index(om['room']) if om['room'] in brooms else None
            else:
                j = i if i < len(bmachines) else None
            m = {}
            if j is not None and j not in used:
                used.add(j)
                m = dict(bmachines[j])
            m.update(om)
            merged.append(m)
        merged += [dict(bm) for j, bm in enumerate(bmachines) if j not in used]
        out['machines'] = merged[:MAX_MACHINES]
    return out


def _fmt(v):
    """Serialize a scalar or list of scalars as a TOML value.

    A list of more than three items is written one item per line.
    """
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if len(v) > 3:
            return '[\n' + ''.join(f'    {_fmt(x)},\n' for x in v) + ']'
        return '[' + ', '.join(_fmt(x) for x in v) + ']'
    s = str(v).replace('\\', '\\\\').replace('"', '\\"')
    s = s.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    return f'"{s}"'

