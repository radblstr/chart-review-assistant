# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Chart-check audit rules that flag Site and Course objects.

- icc_audit flags initial chart-check status on a Site.
- wcc_audit flags weekly chart-check completion across a Course.
- fcc_audit flags final chart-check status for a completed Course.
- cc_charge_audit flags weekly CC charges that do not match the allowed count.
"""

# Toggleable check catalog and the single source for every finding. The top level is keyed by
# category (icc/wcc/fcc/cc_charge), which supplies the chart-check each finding belongs to and
# drives the settings UI sections; under each category, findings are keyed by their stable id.
# Each entry is the finding itself -- id (repeated so an appended entry identifies itself), label
# (settings UI), field, level, message -- so an audit only tests the condition and appends its
# entry. The one finding whose message varies at runtime (wcc_missed) appends a dict() copy
# carrying the override. Each machine profile stores the subset of ids enabled; build_table drops a
# row's findings whose id is disabled for that row's machine.
CHECKS = dict(
    icc=dict(
        icc_due=dict(
            id='icc_due',
            label='ICC due',
            field='icc_completion_dt',
            level='info',
            message='An ICC is due.'),
        sbrt_icc_due=dict(
            id='sbrt_icc_due',
            label='SBRT ICC due',
            field='icc_completion_dt',
            level='info',
            message='An SBRT ICC is due.'),
        icc_missed=dict(
            id='icc_missed',
            label='ICC missed',
            field='icc_completion_dt',
            level='error',
            message='An ICC was missed.')),
    wcc=dict(
        wcc_due=dict(
            id='wcc_due',
            label='WCC due',
            field='n_fxs_since_last_cc',
            level='info',
            message='A WCC is due.'),
        wcc_missed=dict(
            id='wcc_missed',
            label='WCC(s) missed',
            field='last_wcc_completion_dt',
            level='error',
            message='WCCs have been missed in this course.'),
        wcc_next_fx=dict(
            id='wcc_next_fx',
            label='WCC due before next fraction',
            field='n_fxs_since_last_cc',
            level='warning',
            message='A WCC must be completed before the next fraction is treated.'),
        wcc_overdue=dict(
            id='wcc_overdue',
            label='WCC overdue (5-fx window)',
            field='n_fxs_since_last_cc',
            level='error',
            message='A WCC was not completed in the last 5 fraction window.')),
    fcc=dict(
        fcc_due=dict(
            id='fcc_due',
            label='FCC due',
            field='fcc_completion_dt',
            level='info',
            message='A FCC is due.'),
        fcc_missed=dict(
            id='fcc_missed',
            label='FCC missed',
            field='fcc_completion_dt',
            level='error',
            message='A FCC was missed.')),
    cc_charge=dict(
        cc_too_many=dict(
            id='cc_too_many',
            label='Too many CC charges',
            field='allowed_cc_charges',
            level='warning',
            message='Too many CC charges billed.'),
        cc_too_few=dict(
            id='cc_too_few',
            label='CC charges missing',
            field='allowed_cc_charges',
            level='warning',
            message='CC charges are missing for completed chart checks.')))


def icc_audit(site):
    """Check ICC status.

    Note ICC counts as the first WCC in a course when the machine's icc_counts_as_wcc setting is
    on (applied in Course, not here). The ICC is considered missed once the machine's missed
    threshold fraction has been delivered without a completed ICC note, when the first note was
    logged after that fraction, or when the course completes with no note.

    Return statuses:
        - info: An ICC is due.
        - info: An SBRT ICC is due.
        - error: An ICC was missed.

    Args:
        site (Site): The site to audit

    Returns:
        list[dict[str, str]]: CHECKS entries flagged for the site
    """
    status_list = []
    icc_checks = CHECKS['icc']

    # ICC completion status
    if site.icc_due:
        if site.sbrt:
            status_list.append(icc_checks['sbrt_icc_due'])
        else:
            status_list.append(icc_checks['icc_due'])
    if site.icc_missed:
        status_list.append(icc_checks['icc_missed'])
    return status_list


def wcc_audit(course):
    """Check WCC status.

    WCC completion status depends on a CC note being logged in each five-fraction window. The
    first window spans fractions 1-6 and each later window the next five (6-11, 11-16); a window
    is missed once its closing fraction (6th, 11th, 16th) is treated with no note.

    Return statuses:
        - info: A WCC is due.
        - error: {course.n_wcc_missed} WCCs have been missed in this course.
        - warning: A WCC must be completed before the next fraction is treated.
        - error: A WCC was not completed in the last 5 fraction window.

    Args:
        course (Course): The course to audit

    Returns:
        list[dict[str, str]]: CHECKS entries flagged for the course
    """
    status_list = []
    wcc_checks = CHECKS['wcc']

    # WCC completion status
    if course.wcc_due:
        status_list.append(wcc_checks['wcc_due'])
    if course.n_wcc_missed > 0:
        status_list.append(dict(
            wcc_checks['wcc_missed'],
            message=f'{course.n_wcc_missed} WCCs have been missed in this course.'))

    # Fractions since the last WCC-countable note, or all treated fractions when none has been
    # completed
    n_fxs = course.n_fxs_since_last_cc
    if n_fxs == 5:
        status_list.append(wcc_checks['wcc_next_fx'])
    if n_fxs >= 6:
        status_list.append(wcc_checks['wcc_overdue'])
    return status_list


def fcc_audit(course):
    """Check FCC status.

    The FCC is due after the course is complete and considered missed if 5 business days pass.

    Return statuses:
        - info: A FCC is due.
        - error: A FCC was missed.

    Args:
        course (Course): The course to audit

    Returns:
        list[dict[str, str]]: CHECKS entries flagged for the course
    """
    status_list = []
    fcc_checks = CHECKS['fcc']

    # FCC completion status
    if course.fcc_due:
        status_list.append(fcc_checks['fcc_due'])
    if course.fcc_missed:
        status_list.append(fcc_checks['fcc_missed'])
    return status_list


def cc_charge_audit(course):
    """Audit a course's billed CC charges.

    Allowed charges are one per opened 5-fraction window, except a final window of only 1-2
    fractions (course.allowed_cc_charges). Charges are missing when fewer are billed than the
    lesser of chart checks completed (course.n_wcc_completed) and allowed charges, so a check in
    a short final window needs no charge. Charges are too many when more are billed than allowed.

    Return statuses:
        - warning: CC charges are missing for completed chart checks.
        - warning: Too many CC charges billed.

    Args:
        course (Course): The course to audit

    Returns:
        list[dict[str, str]]: CHECKS entries flagged for the course
    """
    status_list = []
    cc_charge_checks = CHECKS['cc_charge']
    if len(course.cc_charges) > course.allowed_cc_charges:
        status_list.append(cc_charge_checks['cc_too_many'])
    if len(course.cc_charges) < min(course.n_wcc_completed, course.allowed_cc_charges):
        status_list.append(cc_charge_checks['cc_too_few'])
    return status_list
