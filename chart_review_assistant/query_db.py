# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Read-only Mosaiq queries feeding the courses.py Course/Site objects.

- query_db runs the whole-board pulls concurrently and builds the Course list.
- get_scheduled_patients lists patients (and their names) with a scheduled or treated fraction in
  the window.
- selection_window computes the date window from app state; week_span is its window helper.
- build_courses slices the whole-board pull per patient and builds the Course list.
- get_* functions each run one SELECT returning a DataFrame (sites, dose summaries, QCLs,
  charges, dose history, calendar, notes, schedule); the QCL and charge pulls return an empty
  frame without querying when their config key is blank.
"""

import datetime
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from sqlalchemy import bindparam, text

from chart_review_assistant import get_engine, config
from chart_review_assistant.courses import Course

logger = logging.getLogger('cra')


def query_db(state):
    """Pull DB data for all patients with a scheduled or treated fraction in the date range.

    Builds a list of Course objects, which contain a list of Site objects. Each Site object
    constitutes a dashboard row.

    Args:
        state (dict[str, object]): App state. Requires 'locations' (list[str]); optional 'now'
            (pd.Timestamp), 'weeks', 'direction', 'custom_start', 'custom_end'

    Returns:
        tuple[list[Course], dict[str, pd.DataFrame | pd.Timestamp]]: (courses, db_pull). courses
            is at most one Course per (Pat_ID1, PCP_ID); both are empty when nothing is
            scheduled. db_pull is the full whole-board pull for every patient: one DataFrame per
            query and 'now' (the pull clock).
    """

    # Patients with a scheduled or treated fraction in the date range. Must be pulled first since
    # later pulls depend on its Pat_ID1 list.
    patients = get_scheduled_patients(state)
    pat_ids = patients['Pat_ID1'].unique().tolist()
    if not pat_ids:
        return [], {}

    # Run remaining pulls concurrently since they are independent of each other.
    with ThreadPoolExecutor(max_workers=8) as ex:
        sites = ex.submit(get_sites, pat_ids)
        dss = ex.submit(get_dose_summary_snapshots, pat_ids)
        qcls = ex.submit(get_qcls, pat_ids)
        cc_charges = ex.submit(get_weekly_cc_charge_dates, pat_ids)
        dose_history = ex.submit(get_dose_history, pat_ids)
        calendar = ex.submit(get_calendar, pat_ids)
        cc_notes = ex.submit(get_cc_notes, pat_ids)
        schedule = ex.submit(get_schedule, pat_ids, state)

    db_pull = dict(
        patients=patients,
        sites=sites.result(),
        dss=dss.result(),
        cc_charges=cc_charges.result(),
        dose_history=dose_history.result(),
        calendar=calendar.result(),
        qcls=qcls.result(),
        cc_notes=cc_notes.result(),
        schedule=schedule.result(),
        now=state.get('now') or pd.Timestamp.now(),
    )
    if logger.isEnabledFor(logging.DEBUG):
        start, end = selection_window(state)
        logger.debug('pull pats=%d window=%s..%s', len(pat_ids), start, end)

    courses = build_courses(db_pull, pat_ids)
    if not courses:
        logger.warning('pull %d patients but no courses built', len(pat_ids))
    return courses, db_pull


def build_courses(db_pull, pat_ids):
    """Build the Course list from the whole-board pull, with no DB access.

    Outer loop over pat_ids (slicing each to its own pull), inner over each patient's PCP_ID
    (course) groups.

    Args:
        db_pull (dict[str, pd.DataFrame | pd.Timestamp]): Whole-board pull from query_db
        pat_ids (list[int]): Patient ids (Pat_ID1) to build courses for

    Returns:
        list[Course]: One Course per (Pat_ID1, PCP_ID) with sites
    """
    courses = []
    for pat_id in pat_ids:

        # Slice the whole-board pull to this patient (same keys): DataFrames are filtered to
        # Pat_ID1, other values passed through
        patient_pull = {}
        for k, v in db_pull.items():
            if isinstance(v, pd.DataFrame):
                patient_pull[k] = v[v['Pat_ID1'] == pat_id]
            else:
                patient_pull[k] = v
        for pcp_id, _ in patient_pull['sites'].groupby('PCP_ID'):
            course = Course(pcp_id, patient_pull, pat_id)
            if course.sites:
                courses.append(course)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug('pull courses [pat:sites] %s',
                     ', '.join(f'{c.pat_id}:{len(c.sites)}' for c in courses))
    return courses


def week_span(today, weeks, direction):
    """Return the (start, end) date window of `weeks` weeks around `today` in `direction`."""
    delta = datetime.timedelta(weeks=weeks)
    if direction == 'past':
        return today - delta, today
    if direction == 'next':
        return today, today + delta
    return today - delta, today + delta


def selection_window(state):
    """Return the (start, end) date window from state.

    [custom_start, custom_end] when both are given, else `weeks` weeks in `direction`
    ('past' | 'next' | else surrounding) around the clock date ('now', else the current time)

    Args:
        state (dict[str, object]): App state. Optional 'now' (pd.Timestamp), 'weeks', 'direction',
            'custom_start', 'custom_end'

    Returns:
        tuple[datetime.date, datetime.date]: (start, end)
    """
    custom_start = state.get('custom_start')
    custom_end = state.get('custom_end')
    if custom_start and custom_end:
        return (datetime.date.fromisoformat(custom_start[:10]),
                datetime.date.fromisoformat(custom_end[:10]))
    today = pd.Timestamp(state.get('now') or pd.Timestamp.now()).date()
    return week_span(today, state.get('weeks') or 4,
                     state.get('direction', 'surrounding'))


def _schedule_filter(state):
    """Shared Schedule WHERE fragment: selected rooms only, holds excluded.

    The rooms are the expanding bind parameter :rooms (see _schedule_sql) and each
    general.hold_activities pattern is its own bind parameter :hold_<i>. Room names and patterns
    come from the browser checklist and config, so they are never interpolated into the SQL.

    Args:
        state (dict[str, object]): App state. Uses 'locations' (list[str])

    Returns:
        tuple[str, dict[str, list[str] | str]]: (fragment, params)
    """
    holds = config.load_config()['general'].get('hold_activities') or []
    clauses = ['RTRIM(stf.Last_Name) IN :rooms']
    params = dict(rooms=list(state['locations']))
    for i, pattern in enumerate(holds):
        clauses.append(f'sch.Activity NOT LIKE :hold_{i}')
        params[f'hold_{i}'] = str(pattern)
    return '\n          AND '.join(clauses), params


def _schedule_sql(sql):
    """Wrap a Schedule query using _schedule_filter as text with the :rooms list bound."""
    return text(sql).bindparams(bindparam('rooms', expanding=True))


def get_scheduled_patients(state):
    """Deduped patient IDs (and names) with a scheduled or treated fraction in the window

    Holds are excluded. The window is selection_window(state).

    Args:
        state (dict[str, object]): App state. Uses 'locations' (list[str]); optional 'now'
            (pd.Timestamp), 'weeks', 'direction', 'custom_start', 'custom_end'

    Returns:
        pd.DataFrame: Columns [Pat_ID1, Last_Name, First_Name, IDB], one row per patient. IDB
            holds the MRN from the Ident field named by the config general.mrn_field.
    """
    start, end = selection_window(state)
    end_excl = end + datetime.timedelta(days=1)

    # MRN field is interpolated into the SQL, so only the Ident ID fields are accepted
    mrn_field = str(config.load_config()['general'].get('mrn_field') or '').upper()
    if mrn_field not in config.MRN_FIELDS:
        logger.warning('mrn_field %r is not one of %s; using %s', mrn_field,
                       ', '.join(config.MRN_FIELDS), config.MRN_FIELD_DEFAULT)
        mrn_field = config.MRN_FIELD_DEFAULT
    schedule_filter, params = _schedule_filter(state)
    sql = f'''
        SELECT DISTINCT sch.Pat_ID1, p.Last_Name, p.First_Name, i.{mrn_field} AS IDB
        FROM Schedule AS sch
        JOIN Staff AS stf ON sch.Location = stf.Staff_ID
        JOIN Ident AS i ON sch.Pat_ID1 = i.Pat_Id1
        JOIN Patient AS p ON sch.Pat_ID1 = p.Pat_ID1
        WHERE {schedule_filter}
          AND sch.App_DtTm >= '{start:%Y-%m-%d} 00:00:00'
          AND sch.App_DtTm < '{end_excl:%Y-%m-%d} 00:00:00'
    '''
    df = pd.read_sql(_schedule_sql(sql), get_engine(), params=params)
    df = df.dropna(subset=['Pat_ID1'])
    df['Pat_ID1'] = df['Pat_ID1'].astype(int)
    return df


def get_schedule(patient_ids, state):
    """Treatment appointments (Schedule) in the selected rooms for the given patients

    Args:
        patient_ids (list[int]): Pat_ID1 values
        state (dict[str, object]): App state. Uses 'locations' (list[str])

    Returns:
        pd.DataFrame: Columns [Pat_ID1, App_DtTm, Last_Name]. Last_Name is the room's Location
            staff name.
    """
    schedule_filter, params = _schedule_filter(state)
    sql = f'''
        SELECT sch.Pat_ID1, sch.App_DtTm, RTRIM(stf.Last_Name) AS Last_Name
        FROM Schedule AS sch
        JOIN Staff AS stf ON sch.Location = stf.Staff_ID
        WHERE {schedule_filter}
          AND sch.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
    '''
    return pd.read_sql(_schedule_sql(sql), get_engine(), parse_dates=['App_DtTm'],
                       params=params)


def get_sites(patient_ids):
    """Current (Version 0) treatment sites for the given patients, with their course

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, SIT_ID, Site_Name, PCP_ID, Course, MED_ID, Diag_Code,
            Diag_Desc]. PCP_ID is the course grouping key; Course is the plan's course number
            (not unique per patient); MED_ID/Diag_Code/Diag_Desc are the parent diagnosis
            (Medical -> Topog), null when the plan has no linked diagnosis.
    """
    sql = f'''
        SELECT s.Pat_ID1, s.SIT_ID, s.Site_Name, s.PCP_ID,
               p.Course, p.MED_ID, t.Diag_Code, t.Description AS Diag_Desc
        FROM Site AS s
        LEFT JOIN PatCPlan AS p ON s.PCP_ID = p.PCP_ID
        LEFT JOIN Medical AS m ON p.MED_ID = m.MED_ID
        LEFT JOIN Topog AS t ON m.TPG_ID = t.TPG_ID
        WHERE s.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          AND s.SIT_ID > 0
          AND s.Version = 0
    '''
    return pd.read_sql(sql, get_engine())


def get_dose_summary_snapshots(patient_ids):
    """Dose summary snapshots (per-session Rx cache) for the given patients

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, SIT_ID, Create_DtTm, RxFxUniformDoseInCcGE,
            RxFractions, RxTotalDoseInCcGE, RxFxUniformDoseIncGray, RxTotalDoseIncGray]
    """
    sql = f'''
        SELECT Pat_ID1, SIT_ID, Create_DtTm, RxFxUniformDoseInCcGE, RxFractions,
               RxTotalDoseInCcGE, RxFxUniformDoseIncGray, RxTotalDoseIncGray
        FROM DoseSummarySnapshot
        WHERE Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
    '''
    return pd.read_sql(sql, get_engine(), parse_dates=['Create_DtTm'])


def get_qcls(patient_ids):
    """Physics QCL tasks matching the clinic's qcl_task_types, for the given patients

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, Description, Complete, Act_DtTm, Due_DtTm]
    """
    types = config.load_config()['general'].get('qcl_task_types') or []
    if not types:
        return pd.DataFrame(columns=['Pat_ID1', 'Description', 'Complete', 'Act_DtTm', 'Due_DtTm'])
    sql = f'''
        SELECT c.Pat_ID1, RTRIM(q.Description) AS Description, c.Complete, c.Act_DtTm, c.Due_DtTm
        FROM Chklist AS c
        JOIN QCLTask AS q ON c.TSK_ID = q.TSK_ID
        WHERE c.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          AND RTRIM(q.Description) IN :types
        ORDER BY c.Pat_ID1, c.Act_DtTm
    '''
    stmt = text(sql).bindparams(bindparam('types', expanding=True))
    return pd.read_sql(stmt, get_engine(), parse_dates=['Act_DtTm', 'Due_DtTm'],
                       params=dict(types=list(types)))


# Shared join/filter resolving a PatTxCal row to its session (PatCItem) and the field's current
# (Version 0) site via TxField.SIT_Set_ID (the canonical SIT_ID). Used by get_dose_history and
# get_calendar so the resolution cannot drift between the two.
_SITE_RESOLVE_JOIN = ('JOIN PatCItem AS pci ON ptc.PCI_ID = pci.PCI_ID\n'
                      '        JOIN TxField AS fld ON ptc.FLD_Set_ID = fld.FLD_SET_ID\n'
                      '            AND fld.Version = 0\n'
                      '        JOIN Site AS s ON fld.SIT_Set_ID = s.SIT_SET_ID')
_SITE_RESOLVE_FILTER = ('AND pci.System_Act = 2\n'
                        '          AND pci.Status_Enum <> 1\n'
                        '          AND s.Version = 0\n'
                        '          AND s.SIT_ID > 0')


def get_dose_history(patient_ids):
    """Delivered dose history: one row per treated field-fraction

    Spine is Dose_Hst (one row per beam delivery), joined through PatTxCal/PatCItem to the field's
    current (Version 0) site via TxField.SIT_Set_ID (for the canonical SIT_ID) and to the
    treatment machine (staff). Scheduled-only field-fractions are in get_calendar.

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, SIT_ID, PCI_ID, PTC_ID, Fractions_Tx, Fx_Number_InEffect,
            Tx_DtTm, Last_Name]. Tx_DtTm is the per-beam delivery time, Fx_Number_InEffect the
            per-site fraction number, Last_Name the treatment machine's staff name.
    """
    sql = f'''
        SELECT ptc.Pat_ID1, s.SIT_ID, pci.PCI_ID, ptc.PTC_ID, dh.Fractions_Tx,
               dh.Fx_Number_InEffect, dh.Tx_DtTm, RTRIM(stf.Last_Name) AS Last_Name
        FROM Dose_Hst AS dh
        JOIN PatTxCal AS ptc ON dh.PTC_ID = ptc.PTC_ID
        {_SITE_RESOLVE_JOIN}
        LEFT JOIN Staff AS stf ON dh.Machine_ID_Staff_ID = stf.Staff_ID
        WHERE ptc.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          {_SITE_RESOLVE_FILTER}
        ORDER BY ptc.Pat_ID1, dh.Tx_DtTm
    '''
    return pd.read_sql(sql, get_engine(), parse_dates=['Tx_DtTm'])


def get_calendar(patient_ids):
    """Treatment calendar: one row per scheduled field-fraction (treated or not)

    Spine is PatTxCal (one row per field per session), joined to its session (PatCItem, for
    scheduled/actual dates and status) and to the field's current (Version 0) site via
    TxField.SIT_Set_ID (for the canonical SIT_ID). Untreated (future) field-fractions are
    included; delivery data lives in get_dose_history.

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, SIT_ID, PCI_ID, PTC_ID, Due_DtTm, Act_DtTm, Status_Enum,
            Seq, Last_Name]. Due_DtTm scheduled date, Act_DtTm treated date, Last_Name machine
    """
    sql = f'''
        SELECT ptc.Pat_ID1, s.SIT_ID, pci.PCI_ID, ptc.PTC_ID, pci.Due_DtTm, pci.Act_DtTm,
               pci.Status_Enum, pci.Seq, RTRIM(stf.Last_Name) AS Last_Name
        FROM PatTxCal AS ptc
        {_SITE_RESOLVE_JOIN}
        LEFT JOIN Staff AS stf ON fld.Machine_ID_Staff_ID = stf.Staff_ID
        WHERE ptc.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          {_SITE_RESOLVE_FILTER}
        ORDER BY ptc.Pat_ID1, pci.Due_DtTm
    '''
    return pd.read_sql(sql, get_engine(), parse_dates=['Due_DtTm', 'Act_DtTm'])


def get_weekly_cc_charge_dates(patient_ids):
    """Weekly chart-check charge dates for the given patients, matching the clinic's cc_cpt_code

    cc_cpt_code is one code or a list of codes.

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, Proc_DtTm]
    """
    code = config.load_config()['general'].get('cc_cpt_code') or ''
    codes = [code] if isinstance(code, str) else [str(c) for c in code]
    codes = [c for c in codes if c]
    if not codes:
        return pd.DataFrame(columns=['Pat_ID1', 'Proc_DtTm'])
    sql = f'''
        SELECT ch.Pat_ID1, ch.Proc_DtTm
        FROM Charge AS ch
        JOIN CPT AS cpt ON ch.PRS_ID = cpt.PRS_ID
        WHERE ch.Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          AND cpt.CPT_Code IN :codes
    '''
    stmt = text(sql).bindparams(bindparam('codes', expanding=True))
    return pd.read_sql(stmt, get_engine(), parse_dates=['Proc_DtTm'], params=dict(codes=codes))


def get_cc_notes(patient_ids):
    """Chart-check notes (Note_Type in general.cc_note_types) for the given patients

    Args:
        patient_ids (list[int]): Pat_ID1 values

    Returns:
        pd.DataFrame: Columns [Pat_ID1, Create_DtTm, Subject]
    """
    note_types = config.load_config()['general'].get('cc_note_types') or []
    sql = f'''
        SELECT Pat_ID1, Create_DtTm, Subject
        FROM Notes
        WHERE Pat_ID1 IN ({', '.join(str(i) for i in patient_ids)})
          AND Note_Type IN :note_types
    '''
    stmt = text(sql).bindparams(bindparam('note_types', expanding=True))
    return pd.read_sql(stmt, get_engine(), parse_dates=['Create_DtTm'],
                       params=dict(note_types=[int(t) for t in note_types]))
