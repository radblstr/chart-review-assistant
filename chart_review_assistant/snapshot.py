# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Snapshots are json dumps of app data at 2 processing points:

db_pull holds all unprocessed SQL queried data.
row_data holds all processed data just before rendering.

These are created by user initiated UI bug reports and by the test-corpus generator
(tests/make_snapshots.py). A test case is a self-contained directory: the baseline json, the mock
Mosaiq DB built from it, clinic_config.toml, and the state file. tests/test_snapshots.py replays
each case through the real pull and compares against the baseline json.

deid_pull de-identifies a live pull for deid mode: surrogate ids and names, canonical site labels
(generic_site), and dates shifted back by a per-process whole-week offset (session_delta).
"""

import hashlib
import json
import random
import re
import sqlite3
from io import StringIO
from pathlib import Path

import pandas as pd

from chart_review_assistant import config

WEEKS_BACK_MIN = 100
WEEKS_BACK_MAX = 200

# Process-stable deid date shift, chosen once by session_delta so every poll and the header rewind
# by the same amount
_session_delta = None


def session_delta():
    """Return the process-stable deid date shift.

    Chosen once (a random whole-week offset in WEEKS_BACK_MIN..WEEKS_BACK_MAX) and reused, so the
    shifted board and the header window agree and dates do not jump between refreshes

    Returns:
        pd.Timedelta: Negative whole-week offset
    """
    global _session_delta
    if _session_delta is None:
        _session_delta = pd.Timedelta(days=-7 * random.randint(WEEKS_BACK_MIN, WEEKS_BACK_MAX))
    return _session_delta


# Regression snapshot case directories live under tests/snapshots, one per scenario (committed;
# the shipped test corpus).
SNAPSHOT_DIR = Path(__file__).parent / 'tests' / 'snapshots'


def write_snapshot(out_dir, db_pull, row_data, snapshot_id, stamp=True):
    """Write one {db_pull, row_data} baseline to out_dir as {snapshot_id}[_<pull clock>].json.

    Each db_pull DataFrame is serialized as pandas table-schema JSON. The baseline holds
    expectations only; the run parameters (clock, window, rooms, config) live in the case
    directory's app_state.json and clinic_config.toml (write_test_case).

    Args:
        out_dir (str | Path): Destination dir, created if missing
        db_pull (dict[str, pd.DataFrame | object]): Whole-board pull; DataFrame values are
            serialized as table-schema JSON and 'now' supplies the filename clock.
        row_data (list[dict]): Rendered dashboard rows, written unchanged
        snapshot_id (str): Filename stem, used verbatim when stamp is False
        stamp (bool): Append the pull clock to the stem (bug reports); case writers pass a
            pre-built stem and False.

    Returns:
        Path: The written JSON file
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = db_pull['now']
    pull = {k: ({'table': v.reset_index(drop=True).to_json(orient='table')}
                if isinstance(v, pd.DataFrame) else v)
            for k, v in db_pull.items()}
    out = dict(db_pull=pull, row_data=row_data)
    stem = f'{snapshot_id}_{now.strftime("%Y%m%d_%H%M%S")}' if stamp else str(snapshot_id)
    path = out_dir / f'{stem}.json'
    path.write_text(json.dumps(out, indent=2, default=str), encoding='utf-8')
    return path


# Raw Site_Name -> canonical deid label. Ordered most-specific first so disambiguating entries win
# (Craniospinal before Spine/Brain, Breast/CW before Lymph Nodes, cervical spine before Cervix).
# Each value is a list of case-insensitive regexes; an entry matches when any pattern is found in
# the name, so a canonical is only assigned from text physically present in the label.
SITE_PATTERNS = {
    'Craniospinal': [r'craniospinal', r'\bcsi\b'],
    'Head and Neck': [
        r'\bh\s*&?\s*n\b', r'head.?neck', r'\bneck\b', r'oropharyn', r'\bopx\b',
        r'nasopharyn', r'nasoph', r'hypopharyn', r'\blaryn', r'glottis', r'tonsil',
        r'\btongue\b', r'parotid', r'salivary', r'oral\s*cavity', r'\bthyroid\b',
        r'skull\s*base', r'\bskbs\b', r'\bbos\b', r'sinus', r'sinonasal', r'\bsino',
        r'ethmoid', r'sphenoid', r'maxillary', r'palate', r'clivus', r'\borbit',
    ],
    'Breast/CW': [
        r'\bbreast', r'mastectomy', r'lumpectomy', r'\bapbi\b', r'chest\s*wall',
        r'\bcw\b', r'supraclav', r'\bscv\b', r'axilla',
    ],
    'Spine': [
        r'\bspine\b', r'\bspinal\b', r'vertebra', r'thoracic', r'lumbar',
        r'cervical\s*spine', r'chordoma',
    ],
    'Brain': [
        r'\bbrain\b', r'whole\s*brain', r'\bwbrt\b', r'\bwb\b', r'\bsrs\b', r'\bglioma\b',
        r'\bgbm\b', r'cerebell', r'frontal', r'parietal', r'occipital', r'temporal',
        r'pineal', r'suprasellar', r'pituitary', r'intracran',
    ],
    'Lung': [r'\blung', r'nsclc', r'\bsclc\b', r'\blobe\b', r'pulmonary', r'hilum'],
    'Esophagus': [r'esophag', r'\beso', r'\bgej\b'],
    'Mediastinum': [r'mediastin'],
    'Prostate': [r'\bprost'],
    'Bladder': [r'bladder'],
    'Rectum': [r'rectum', r'rectal'],
    'Anus': [r'\banus\b', r'\banal\b'],
    'Cervix': [r'cervix', r'cervical(?!\s*spine)'],
    'Uterus': [r'uterus', r'uterine', r'endometri'],
    'Ovary': [r'ovary', r'ovarian'],
    'Vulva': [r'vulva', r'vagina'],
    'Pancreas': [r'pancrea'],
    'Liver': [r'\bliver', r'hepatic', r'\bhcc\b'],
    'Stomach': [r'stomach', r'gastric'],
    'Kidney': [r'kidney', r'renal'],
    'Adrenal': [r'adrenal'],
    'Bowel': [r'\bbowel\b', r'\bcolon', r'sigmoid'],
    'Skin': [r'\bskin\b', r'\bbcc\b', r'\bscc\b', r'melanoma', r'cutaneous'],
    'Sarcoma': [r'sarcoma', r'\bsarc', r'\bewing\b', r'\basps\b'],
    'Ocular': [r'\beye\b'],
    'Extremity': [
        r'extremit', r'\barm\b', r'\bleg\b', r'thigh', r'\bhand\b', r'\bfoot\b',
        r'\bknee\b', r'\bshoulder\b', r'\b[rl][lu]e\b',
    ],
    'Lymph Nodes': [
        r'lymph', r'\bnodal\b', r'\bnode', r'\bln\b', r'\blns\b', r'inguinal',
        r'\bgroin\b', r'iliac',
    ],
    'Bone': [
        r'\bbone', r'\brib\b', r'femur', r'humerus', r'sacrum', r'sternum', r'calvarium',
        r'ilium', r'pubis', r'\bhip\b', r'\bskull\b',
    ],
    'Pelvis': [r'pelvi'],
    'Abdomen': [r'abdom'],
}


def generic_site(site_name):
    """Map a raw site name to a canonical SITE_PATTERNS label, or 'Other' when nothing matches.

    Entries are tried most-specific first; the first entry with any regex found in the name wins.

    Args:
        site_name (str): Raw Site_Name value

    Returns:
        str: Canonical site label, or 'Other'
    """
    name = str(site_name)
    for canon, pats in SITE_PATTERNS.items():
        if any(re.search(p, name, re.I) for p in pats):
            return canon
    return 'Other'


def deid_pull(db_pull):
    """De-identify a db_pull and shift every date back by a random number of whole weeks

    The weeks fall in WEEKS_BACK_MIN..WEEKS_BACK_MAX; weekday and time-of-day are preserved, so the
    audit outcome is invariant. The offset is chosen once per process (session_delta) and applied
    to every frame.

    Surrogate maps are built from the pull's own ids so a repeated id maps consistently across
    every frame. What changes:

      - Pat_ID1          -> a same-digit-count hash of the real id (pat_map)
      - SIT_ID, PCP_ID   -> a same-digit-count hash of the real id (key_map)
      - Notes Subject    -> a check token ('Initial eChart Check'/'Weekly eChart Check')
      - patients frame   -> surrogate labels ('Pat <sur>', blank first name, IDB '<sur>'), no
                            real names or MRNs remain
      - Site_Name        -> a canonical SITE_PATTERNS label, or 'Other'
      - *_DtTm columns and now -> shifted back by the random number of whole weeks

    What passes through untouched:

      - session/field ids (PCI_ID, PTC_ID) and plan keys (MED_ID, Course): internal row links,
        not patient identifiers
      - room / machine Last_Name, diagnosis (Diag_Code, Diag_Desc), and prescription: not
        identifiers

    Args:
        db_pull (dict[str, pd.DataFrame | object]): A flat whole-board or single-patient pull

    Returns:
        dict[str, pd.DataFrame | object]: The de-identified pull. The input is not mutated.
    """
    delta = session_delta()

    def _hash_id(v):
        """Hash an id to a stable surrogate integer with the same digit count as the real id

        Deterministic across runs, unlike the built-in hash
        """
        s = str(int(v))
        lo = 10 ** (len(s) - 1)
        h = int(hashlib.sha256(s.encode()).hexdigest(), 16)
        return lo + h % (10 ** len(s) - lo)

    def _frame_vals(col):
        """Collect the non-null values of column `col` across every DataFrame in the pull."""
        vals = set()
        for v in db_pull.values():
            if isinstance(v, pd.DataFrame) and col in v.columns:
                vals.update(x for x in v[col].tolist() if pd.notna(x))
        return vals

    pat_map = {pid: _hash_id(pid) for pid in _frame_vals('Pat_ID1')}
    key_vals = sorted(_frame_vals('SIT_ID') | _frame_vals('PCP_ID'))
    key_map = {v: _hash_id(v) for v in key_vals}

    def _deid_frame(df):
        """Return a copy of one pull DataFrame with its identifier, subject, site and date columns
        de-identified via the surrogate maps, generic_site and the delta shift.
        """
        df = df.copy()
        for col in df.columns:
            if col == 'Pat_ID1':
                df[col] = df[col].map(lambda v: pat_map.get(v, v))
            elif col in ('SIT_ID', 'PCP_ID'):
                df[col] = df[col].map(lambda v: key_map.get(v, v) if pd.notna(v) else v)
            elif col == 'Subject':
                df[col] = df[col].map(
                    lambda v: 'Initial eChart Check' if 'initial' in str(v).lower()
                    else 'Weekly eChart Check')
            elif col == 'Site_Name':
                df[col] = df[col].map(generic_site)
            elif col.endswith('_DtTm'):
                df[col] = pd.to_datetime(df[col], errors='coerce') + delta
        return df

    def _deid_patients(df):
        """Return a copy of the patients frame with surrogate ids and 'Pat <sur>' labels: no real
        name or MRN remains. Handled apart from _deid_frame because Last_Name is a room column
        in the other frames.
        """
        df = _deid_frame(df)
        df['Last_Name'] = df['Pat_ID1'].map(lambda v: f'Pat {v}')
        df['First_Name'] = ''
        df['IDB'] = df['Pat_ID1'].map(str)
        return df

    out_pull = {}
    for k, v in db_pull.items():
        if k == 'patients':
            out_pull[k] = _deid_patients(v)
        elif isinstance(v, pd.DataFrame):
            out_pull[k] = _deid_frame(v)
        elif k == 'now':
            out_pull[k] = pd.Timestamp(v) + delta if v is not None else v
        else:
            out_pull[k] = v
    return out_pull


def strip_session_keys(rows):
    """Remove the session UI key (the Hide checkbox) from rendered rows.

    Rendered rows must not carry it in a baseline comparison.
    """
    for r in rows:
        r.pop('hide', None)
    return rows


def case_state(locations, window, now):
    """The app_state.json content for a test case: rooms, custom window, pinned clock

    Weeks is blanked so the custom window wins.
    """
    return dict(
        locations=list(locations or []),
        weeks=None,
        direction='surrounding',
        custom_start=str(window[0])[:10],
        custom_end=str(window[1])[:10],
        now=str(now),
    )


def load_baseline(path):
    """Load a baseline JSON: the raw snapshot dict plus its db_pull frames as DataFrames."""
    snap = json.loads(Path(path).read_text(encoding='utf-8'))
    frames = {k: pd.read_json(StringIO(v['table']), orient='table')
              for k, v in snap['db_pull'].items() if isinstance(v, dict) and 'table' in v}
    return snap, frames


def write_test_case(case_dir, stem, db_pull, row_data, state, clinic_cfg):
    """Write one self-contained test-case directory.

    The directory is the test's whole input plus its baseline: <stem>.db (mock Mosaiq DB),
    app_state.json (locations, custom window, pinned clock), clinic_config.toml, and <stem>.json
    (the {db_pull, row_data} baseline, read only at compare time).

    Args:
        case_dir (str | Path): The case directory, created if missing
        stem (str): Case name; names the .json/.db pair and normally the directory
        db_pull (dict[str, pd.DataFrame | object]): Whole-board pull (the baseline's raw half)
        row_data (list[dict]): Rendered dashboard rows (the baseline's rendered half)
        state (dict[str, str | list[str] | None]): App state to write as app_state.json. Must
            carry 'locations', 'custom_start', 'custom_end', and 'now' (ISO string).
        clinic_cfg (dict[str, dict | list[dict]]): Config to serialize as the case's
            clinic_config.toml ('general' and 'machines')

    Returns:
        Path: The case directory
    """
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    path = write_snapshot(case_dir, db_pull, row_data, stem, stamp=False)
    (case_dir / 'clinic_config.toml').write_text(config.toml_text(clinic_cfg), encoding='utf-8')
    (case_dir / 'app_state.json').write_text(json.dumps(state, indent=2, default=str),
                                             encoding='utf-8')

    # The case's merged settings: code defaults < its clinic_config.toml < its user_config.toml
    cfg = config._merge(config.default_config(),
                        config._read_toml(case_dir / 'clinic_config.toml'))
    cfg = config._merge(cfg, config._read_toml(case_dir / 'user_config.toml'))
    build_snapshot_db(path, state.get('locations'), cfg)
    return case_dir


def build_snapshot_db(snapshot_path, locations, cfg):
    """Build a mock Mosaiq test DB from a baseline JSON.

    Inverts each db_pull frame back into the Mosaiq tables query_db selects from, generating
    the join keys (Staff_ID, FLD_ID, SIT_SET_ID, TSK_ID, PRS_ID) and filter columns (Version,
    System_Act, Note_Type, Activity) the SQL requires, so the real pull runs unmodified against
    the SQLite file. Emits <snapshot stem>.db next to the JSON (its test pair) and returns its
    path.

    Args:
        snapshot_path (str | Path): A {db_pull, row_data} baseline JSON
        locations (list[str]): Selected rooms the rows were rendered under
        cfg (dict[str, dict | list[dict]]): Config the rows were rendered under;
            the first general.cc_cpt_code fills the CPT table and the first general.cc_note_types
            entry tags the Notes rows.

    Returns:
        Path: The written .db file
    """
    snapshot_path = Path(snapshot_path)
    _, frames = load_baseline(snapshot_path)
    general = (cfg or {}).get('general') or {}
    cc_cpt_code = general.get('cc_cpt_code') or ''
    if not isinstance(cc_cpt_code, str):
        cc_cpt_code = cc_cpt_code[0]
    cc_note_type = (general.get('cc_note_types') or config.CC_NOTE_TYPES_DEFAULT)[0]

    # Generated Staff ids for every room name seen anywhere in the pull (or selected)
    rooms = set(locations or [])
    for k in ('schedule', 'dose_history', 'calendar'):
        if k in frames and 'Last_Name' in frames[k].columns:
            rooms.update(x for x in frames[k]['Last_Name'].tolist() if pd.notna(x))
    staff_ids = {room: 9001 + i for i, room in enumerate(sorted(rooms))}

    def staff_of(series):
        """Map a room-name column to its generated Staff_ID (None for a blank room)."""
        return series.map(lambda r: staff_ids.get(r) if pd.notna(r) else None)

    # The fallback frames, backfilled columns, and generated spine rows below are deliberate
    # armor: the generator always supplies full frames, but a hand-built or trimmed baseline must
    # still produce a queryable DB.
    patients = frames.get('patients', pd.DataFrame(
        columns=['Pat_ID1', 'Last_Name', 'First_Name', 'IDB']))
    sites = frames.get('sites', pd.DataFrame(
        columns=['Pat_ID1', 'SIT_ID', 'Site_Name', 'PCP_ID', 'Course', 'MED_ID',
                 'Diag_Code', 'Diag_Desc']))
    schedule = frames.get('schedule', pd.DataFrame(
        columns=['Pat_ID1', 'App_DtTm', 'Last_Name']))
    calendar = frames.get('calendar', pd.DataFrame(
        columns=['Pat_ID1', 'SIT_ID', 'PCI_ID', 'PTC_ID', 'Due_DtTm', 'Act_DtTm',
                 'Status_Enum', 'Seq', 'Last_Name']))
    dose_history = frames.get('dose_history', pd.DataFrame(
        columns=['Pat_ID1', 'SIT_ID', 'PCI_ID', 'PTC_ID', 'Fractions_Tx',
                 'Fx_Number_InEffect', 'Tx_DtTm', 'Last_Name']))
    qcls = frames.get('qcls', pd.DataFrame(
        columns=['Pat_ID1', 'Description', 'Complete', 'Act_DtTm', 'Due_DtTm']))
    cc_charges = frames.get('cc_charges', pd.DataFrame(columns=['Pat_ID1', 'Proc_DtTm']))
    cc_notes = frames.get('cc_notes', pd.DataFrame(
        columns=['Pat_ID1', 'Create_DtTm', 'Subject']))
    for col, default in (('Status_Enum', 0), ('Seq', None)):
        if col not in calendar.columns:
            calendar[col] = default
    if 'Fractions_Tx' not in dose_history.columns:
        dose_history['Fractions_Tx'] = None

    # Treatment spine: one PatTxCal/TxField row per calendar entry (FLD ids reuse PTC_ID), one
    # PatCItem per session. Dose_Hst rows missing a calendar entry get a generated spine row so
    # the join resolves.
    spine = calendar[['Pat_ID1', 'SIT_ID', 'PCI_ID', 'PTC_ID', 'Due_DtTm', 'Act_DtTm',
                      'Status_Enum', 'Seq', 'Last_Name']].copy()
    missing = dose_history[~dose_history['PTC_ID'].isin(spine['PTC_ID'])]
    if len(missing):
        spine = pd.concat([spine, pd.DataFrame(dict(
            Pat_ID1=missing['Pat_ID1'],
            SIT_ID=missing['SIT_ID'],
            PCI_ID=missing['PCI_ID'],
            PTC_ID=missing['PTC_ID'],
            Due_DtTm=missing['Tx_DtTm'],
            Act_DtTm=missing['Tx_DtTm'],
            Status_Enum=0,
            Seq=None,
            Last_Name=missing['Last_Name']))], ignore_index=True)

    plans = sites.drop_duplicates('PCP_ID')
    diags = plans[plans['MED_ID'].notna()].drop_duplicates('MED_ID')
    qcl_tasks = sorted(x for x in qcls['Description'].unique().tolist() if pd.notna(x))
    tsk_ids = {desc: 5001 + i for i, desc in enumerate(qcl_tasks)}
    tables = dict(
        Staff=pd.DataFrame(dict(
            Staff_ID=list(staff_ids.values()),
            Last_Name=list(staff_ids.keys()))),
        Patient=pd.DataFrame(dict(
            Pat_ID1=patients['Pat_ID1'],
            Last_Name=patients['Last_Name'],
            First_Name=patients['First_Name'])),
        Ident=pd.DataFrame(dict(
            Pat_Id1=patients['Pat_ID1'],
            IDB=patients['IDB'])),
        Schedule=pd.DataFrame(dict(
            Pat_ID1=schedule['Pat_ID1'],
            App_DtTm=schedule['App_DtTm'],
            Location=staff_of(schedule['Last_Name']),
            Activity='TX')),
        Site=pd.DataFrame(dict(
            Pat_ID1=sites['Pat_ID1'],
            SIT_ID=sites['SIT_ID'],
            SIT_SET_ID=sites['SIT_ID'],
            Site_Name=sites['Site_Name'],
            PCP_ID=sites['PCP_ID'],
            Version=0)),
        PatCPlan=pd.DataFrame(dict(
            PCP_ID=plans['PCP_ID'],
            Course=plans['Course'],
            MED_ID=plans['MED_ID'])),
        Medical=pd.DataFrame(dict(
            MED_ID=diags['MED_ID'],
            TPG_ID=diags['MED_ID'])),
        Topog=pd.DataFrame(dict(
            TPG_ID=diags['MED_ID'],
            Diag_Code=diags['Diag_Code'],
            Description=diags['Diag_Desc'])),
        DoseSummarySnapshot=frames.get('dss', pd.DataFrame(
            columns=['Pat_ID1', 'SIT_ID', 'Create_DtTm', 'RxFxUniformDoseInCcGE', 'RxFractions',
                     'RxTotalDoseInCcGE', 'RxFxUniformDoseIncGray', 'RxTotalDoseIncGray'])),
        QCLTask=pd.DataFrame(dict(
            TSK_ID=list(tsk_ids.values()),
            Description=list(tsk_ids.keys()))),
        Chklist=pd.DataFrame(dict(
            Pat_ID1=qcls['Pat_ID1'],
            TSK_ID=qcls['Description'].map(tsk_ids),
            Complete=qcls['Complete'],
            Act_DtTm=qcls['Act_DtTm'],
            Due_DtTm=qcls['Due_DtTm'])),
        PatTxCal=pd.DataFrame(dict(
            PTC_ID=spine['PTC_ID'],
            Pat_ID1=spine['Pat_ID1'],
            PCI_ID=spine['PCI_ID'],
            FLD_Set_ID=spine['PTC_ID'])),
        TxField=pd.DataFrame(dict(
            FLD_ID=spine['PTC_ID'],
            FLD_SET_ID=spine['PTC_ID'],
            Version=0,
            SIT_Set_ID=spine['SIT_ID'],
            Machine_ID_Staff_ID=staff_of(spine['Last_Name']))),
        PatCItem=pd.DataFrame(dict(
            PCI_ID=spine['PCI_ID'],
            Due_DtTm=spine['Due_DtTm'],
            Act_DtTm=spine['Act_DtTm'],
            Status_Enum=spine['Status_Enum'],
            Seq=spine['Seq'],
            System_Act=2)).drop_duplicates('PCI_ID'),
        Dose_Hst=pd.DataFrame(dict(
            PTC_ID=dose_history['PTC_ID'],
            Fractions_Tx=dose_history['Fractions_Tx'],
            Fx_Number_InEffect=dose_history['Fx_Number_InEffect'],
            Tx_DtTm=dose_history['Tx_DtTm'],
            Machine_ID_Staff_ID=staff_of(dose_history['Last_Name']))),
        Charge=pd.DataFrame(dict(
            Pat_ID1=cc_charges['Pat_ID1'],
            Proc_DtTm=cc_charges['Proc_DtTm'],
            PRS_ID=7001)),
        CPT=pd.DataFrame(dict(PRS_ID=[7001], CPT_Code=[cc_cpt_code])),
        Notes=pd.DataFrame(dict(
            Pat_ID1=cc_notes['Pat_ID1'],
            Create_DtTm=cc_notes['Create_DtTm'],
            Subject=cc_notes['Subject'],
            Note_Type=cc_note_type)),
    )

    path = snapshot_path.parent / f'{snapshot_path.stem}.db'
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        for name, frame in tables.items():
            frame.to_sql(name, con, index=False)
    finally:
        con.close()
    return path
