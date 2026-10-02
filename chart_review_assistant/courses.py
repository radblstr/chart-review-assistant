# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Dashboard-row data objects built from the query_db.py pulls.

- Course groups a single course's sites and slices the per-patient DB pulls to it.
- Site builds one dashboard row per room, prescription, treated fractions, and ICC timing.
- Course.__init__ runs the checks.py chart-check audits on each Site.
- merge_lr_prostate combines concurrent bilateral prostate sites into one row.
- count_wccs counts completed and missed weekly chart checks.
"""
import operator
import re

import pandas as pd

from chart_review_assistant import config
from chart_review_assistant.checks import icc_audit, wcc_audit, fcc_audit, cc_charge_audit


class Course:
    """Calculates and holds all course-level data needed for its constituent Site dashboard rows.

    List of courses is built by query_db.py.

    Warning tests are run by init on constituent Site objects.

    Attributes:
        pcp_id (int): Course grouping key (table: Site, col: PCP_ID)
        now (pd.Timestamp): Pull clock for time comparisons
        course_n (int): Course number, not unique per patient (table: PatCPlan, col: Course)
        med_id (int): Medical/diagnosis record id (table: PatCPlan, col: MED_ID)
        dx (str): Diagnosis description (table: Topog, col: Description)
        tx_cal (pd.DataFrame): Scheduled and treated fractions, one row per session
            (table: PatTxCal)
        tx_cal_dts (list[pd.Timestamp]): Treated or scheduled fraction times as a list
        cc_charges (pd.DataFrame): Billed CC charges (table: Charge)
        cc_notes (pd.DataFrame): eChart Check notes (table: Notes)
        wcc_notes (pd.DataFrame): cc_notes counted toward WCC tallies; excludes the on-time ICC
            note when the room's icc_counts_as_wcc setting is off
        tx_appt_dt (pd.Timestamp | None): Today's earliest scheduled appointment time
        sites (list[Site]): List of site objects
        tx_hst (pd.DataFrame): Course treatment history (table: Dose_Hst)
        tx_hst_dts (list[pd.Timestamp]): Treated fractions as a list of timestamps
        course_complete (bool): True when every site's treated equals prescribed fractions
        n_wcc_completed (int): Weekly CCs with a completing note in their due window
        n_wcc_missed (int): Number of missed weekly CCs
        wcc_due (bool): Indicates a WCC needs to be completed; never True on a complete course
        last_wcc_completion_dt (pd.Timestamp | None): Latest WCC-countable note time (wcc_notes)
        n_fxs_since_last_cc (int): Fractions treated since the last WCC-countable note; all
            treated fractions when no such note has ever been completed
        fcc_completion_dt (pd.Timestamp | None): Final-CC eChart Check note time
        fcc_missed_dt (pd.Timestamp | None): FCC deadline (last fraction + 5 business days);
            None until the course is complete
        fcc_due (bool): Indicates a FCC needs to be completed
        fcc_missed (bool): Indicates the FCC window closed without an FCC
        expected_wccs (int): Expected weekly CCs to date, one per opened 5-fraction window
            (windows open on the 1st, 6th, 11th, ... treated fraction)
        allowed_cc_charges (int): CC charges allowed to date
        pat_id (int): Patient Mosaiq UID (Pat_ID1)
        last_name (str): Patient last name (table: Patient, col: Last_Name)
        first_name (str): Patient first name (table: Patient, col: First_Name)
        mrn (str): Patient MRN (table: Ident, col: general.mrn_field, default IDB)
        thresholds (dict[str, dict[str, int | bool]]): Per-room Settings thresholds
            (config.settings_thresholds_map), read here for icc_counts_as_wcc, by the Sites and
            by the L/R merge
    """

    def __init__(self, pcp_id, db_pull, pat_id):
        """Build the course from its patient/course slice of the DB pull and run its Site checks.

        Slices db_pull to this PCP_ID, builds the Site rows, merges bilateral prostate
        sites, derives course-wide treatment history, CC notes/charges, appointments, completeness
        and WCC/FCC timing, then runs the chart checks on each Site. Leaves self.sites empty when
        the slice yields no renderable sites (no scheduled calendar rows, or a zeroed/absent Rx).

        Args:
            pcp_id (int): Course grouping key (table: Site, col: PCP_ID)
            db_pull (dict[str, pd.DataFrame | pd.Timestamp]): Per-patient DB pull from query_db
            pat_id (int): Patient Mosaiq UID (Pat_ID1)
        """
        self.pcp_id = pcp_id
        self.now = db_pull['now']
        self.pat_id = pat_id

        # Display label from the patients frame (Patient/Ident rows of the scheduled-patients
        # query; deid surrogates in deid mode); a placeholder covers a patient with no row.
        patients = db_pull['patients']
        if len(patients):
            p = patients.iloc[0]
            self.last_name = p['Last_Name']
            self.first_name = p['First_Name']
            self.mrn = p['IDB']
        else:
            self.last_name = f'Pat {pat_id}'
            self.first_name = ''
            self.mrn = ''

        # Per-room Settings thresholds, read from config at build time so every rebuild (live
        # poll, soft refresh, deid) uses the current values.
        cfg = config.load_config()
        self.thresholds = config.settings_thresholds_map(cfg)

        # Slice DB pull by course, keeping only sites with scheduled fractions.
        sites_df = db_pull['sites'][
            (db_pull['sites']['PCP_ID'] == self.pcp_id) &
            (db_pull['sites']['SIT_ID'].isin(db_pull['calendar']['SIT_ID']))]
        if sites_df.empty:
            self.sites = []
            return
        self.course_n = sites_df['Course'].iloc[0]
        self.med_id = sites_df['MED_ID'].iloc[0]
        self.dx = sites_df['Diag_Desc'].iloc[0]

        # Build course site list. Sites with a zeroed or absent Rx are skipped.
        self.sites = []
        for _, site in sites_df.iterrows():
            site = Site(self, site, db_pull)
            if site.rx:
                self.sites.append(site)
        self.merge_lr_prostate(db_pull)
        self.sites.sort(key=operator.attrgetter('tx_cal_dts'))

        # A course whose sites were all skipped (zeroed or absent Rx) carries no board rows;
        # leave it empty for the caller to drop.
        if not self.sites:
            return

        # Build course-wise treatment histories and treatment calendars by concatenating across
        # sites, then dedup on session id (PCI_ID) so concurrent sites treated in one session count
        # as a single fraction, and sort chronologically.
        self.tx_hst = (pd.concat([s.tx_hst for s in self.sites], ignore_index=True)
                       .drop_duplicates('PCI_ID').sort_values('Tx_DtTm'))
        self.tx_hst_dts = self.tx_hst['Tx_DtTm'].to_list()
        self.tx_cal = (pd.concat([s.tx_cal for s in self.sites], ignore_index=True)
                       .drop_duplicates('PCI_ID').sort_values('dt'))
        self.tx_cal_dts = self.tx_cal['dt'].to_list()

        # Slice CC notes and CC charges by treatment calendar, both on the same window (first
        # calendar fraction through the last + 5 business days).
        cc_start = self.tx_cal_dts[0]
        cc_end = self.tx_cal_dts[-1] + pd.offsets.BusinessDay(5)
        self.cc_charges = db_pull['cc_charges'][
            db_pull['cc_charges']['Proc_DtTm'].between(cc_start, cc_end)]
        self.cc_notes = db_pull['cc_notes'][
            db_pull['cc_notes']['Create_DtTm'].between(cc_start, cc_end)]

        # Scheduled appointments span the course; today's earliest is the board's appointment time.
        schedule = db_pull['schedule'][
            db_pull['schedule']['App_DtTm'].between(self.tx_cal_dts[0], self.tx_cal_dts[-1])]
        appts_today = schedule[schedule['App_DtTm'].dt.date == self.now.date()]
        self.tx_appt_dt = appts_today['App_DtTm'].min() if len(appts_today) else None

        # Course is complete when every site is complete.
        self.course_complete = all(s.complete for s in self.sites)

        # An ICC still due when the course completes (no note before the missed threshold was
        # reached) is missed.
        if self.course_complete:
            for s in self.sites:
                if s.icc_due:
                    s.icc_missed = True
                s.icc_due = False

        # Weekly CC note view. When the on-time initial chart check's room opts out of counting it
        # as a weekly CC, drop that note so it lands in none of the WCC tallies; cc_notes is left
        # intact for the FCC check.
        self.wcc_notes = self.cc_notes
        icc_sites = [s for s in self.sites if s.icc_completion_dt is not None]
        if icc_sites:
            icc_site = min(icc_sites, key=operator.attrgetter('icc_completion_dt'))
            th = config.machine_thresholds(self.thresholds.get(icc_site.room) or {})
            if not th['icc_counts_as_wcc']:
                drop = self.wcc_notes.index[
                    self.wcc_notes['Create_DtTm'] == icc_site.icc_completion_dt]
                if len(drop):
                    self.wcc_notes = self.wcc_notes.drop(drop[0])

        # Count completed and missed weekly CCs from the eChart Check notes.
        self.n_wcc_completed = 0
        self.n_wcc_missed = 0
        self.wcc_due = False
        self.count_wccs()

        # A WCC is no longer due once the course is complete.
        if self.course_complete:
            self.wcc_due = False

        # Get last WCC completion time.
        self.last_wcc_completion_dt = (
            self.wcc_notes['Create_DtTm'].max() if len(self.wcc_notes) else None)

        # Fractions treated since the last WCC-countable note was recorded. If no such note has
        # been recorded, all treated fractions count.
        if self.last_wcc_completion_dt is None:
            self.n_fxs_since_last_cc = len(self.tx_hst_dts)
        else:
            self.n_fxs_since_last_cc = sum(
                dt > self.last_wcc_completion_dt for dt in self.tx_hst_dts)

        # Get FCC completion time.
        self.fcc_completion_dt = None
        if self.course_complete and any(self.cc_notes['Create_DtTm'] >= self.tx_hst_dts[-1]):
            self.fcc_completion_dt = self.cc_notes['Create_DtTm'].max()

        # FCC deadline: last fraction + 5 business days, computable once the course is complete
        # (a complete course always has treated fractions).
        self.fcc_missed_dt = (self.tx_hst_dts[-1] + pd.offsets.BusinessDay(5)
                         if self.course_complete else None)

        # An FCC is pending when the course is complete and no FCC note exists; it is due until
        # the missed edge passes, missed after.
        fcc_pending = self.course_complete and self.fcc_completion_dt is None
        self.fcc_due = fcc_pending and self.now <= self.fcc_missed_dt
        self.fcc_missed = fcc_pending and self.now > self.fcc_missed_dt

        # Expected weekly CCs to date: one per opened 5-fraction window, the same windows
        # count_wccs loops over (windows open on the 1st, 6th, 11th, ... treated fraction; none
        # opens on the last scheduled fraction).
        self.expected_wccs = sum(
            1 for fx_n in range(1, len(self.tx_cal_dts), 5) if fx_n <= len(self.tx_hst_dts))

        # Allowed CC charges to date: one per opened 5-fraction window, except a final window that
        # covers only 1-2 scheduled fractions. Over a full course this is one per 5 fractions plus
        # one more for a remainder of 3-4.
        n_fxs = max(len(self.tx_cal_dts), len(self.tx_hst_dts))
        self.allowed_cc_charges = sum(
            1 for fx_n in range(1, len(self.tx_hst_dts) + 1, 5) if n_fxs - fx_n + 1 >= 3)

        # Run checks. Course-level audits are computed once and shared by every site. A blank
        # cc_cpt_code pulls no charges, so the charge audit is skipped rather than flagging every
        # course as missing charges; the board's warning banner tells the user it is not set.
        wcc = wcc_audit(self)
        fcc = fcc_audit(self)
        cc_charge = cc_charge_audit(self) if cfg['general'].get('cc_cpt_code') else []
        for site in self.sites:
            icc = icc_audit(site)
            site_wcc = wcc

            # A completed site drops the warning-level WCC findings; errors and info are kept
            # (ICC findings carry no warning level).
            if site.complete:
                site_wcc = [s for s in wcc if s['level'] != 'warning']
            site.warnings = icc + site_wcc + fcc + cc_charge

    def merge_lr_prostate(self, db_pull):
        """Merge concurrent single-beam-per-day prostate site pairs into one bilateral site.

        Sites to be merged are flagged by names that mirror after an L/R flip, equal per-fraction
        doses, date-alternating treated fractions, and site name containing the string "prostate".
        Right side is merged into left side and "/R" inserted immediately after the left name's "L"
        marker, with " (merged)" appended; every attribute derived from the Rx and history is
        recomputed over both sides.

        Args:
            db_pull (dict[str, pd.DataFrame | pd.Timestamp]): Per-patient DB pull from query_db
        """

        def swap_lr(name):
            """Flip the site name's L/R laterality marker; return it unchanged when it has neither.

            The marker is an isolated L or R wherever it appears.
            """
            return re.sub(r'(?<![A-Za-z])[LR](?![A-Za-z])',
                          lambda m: 'R' if m.group() == 'L' else 'L', name or '')

        # Pair each site with the one whose name matches after flipping its L/R, whose per-fraction
        # doses are equal, AND whose fractions alternate by date -- so an L of one phase is not
        # paired with an R of another.
        stack = [s for s in self.sites if 'prostate' in (s.site_name or '').lower()]
        pairs = []
        while stack:
            a = stack.pop()
            for b in stack:
                if swap_lr(b.site_name) != a.site_name:
                    continue
                if a.rx_fx_dose != b.rx_fx_dose or a.rx_fx_dose_rbe != b.rx_fx_dose_rbe:
                    continue
                seq = ([(d, 0) for d in a.tx_hst['Tx_DtTm']]
                       + [(d, 1) for d in b.tx_hst['Tx_DtTm']])
                seq.sort(key=lambda x: x[0])
                if len(seq) < 2 or any(seq[i][1] == seq[i + 1][1] for i in range(len(seq) - 1)):
                    continue
                stack.remove(b)

                # Order the pair left side first by a's laterality marker.
                side = re.search(r'(?<![A-Za-z])[LR](?![A-Za-z])', a.site_name or '')
                pairs.append((a, b) if side and side.group() == 'L' else (b, a))
                break

        for left, right in pairs:
            left.site_name = re.sub(r'(?<![A-Za-z])L(?![A-Za-z])', 'L/R',
                                     left.site_name, count=1) + ' (merged)'
            left.n_rx_fxs = left.n_rx_fxs + right.n_rx_fxs
            left.rx_dose = left.rx_dose + right.rx_dose
            left.rx_dose_rbe = left.rx_dose_rbe + right.rx_dose_rbe
            left.rx = left.format_rx()
            left.tx_hst = pd.concat([left.tx_hst, right.tx_hst]).sort_values('Tx_DtTm')
            left.tx_cal = pd.concat([left.tx_cal, right.tx_cal]).sort_values('dt')
            left.tx_hst_dts = left.tx_hst['Tx_DtTm'].to_list()
            left.tx_cal_dts = left.tx_cal['dt'].to_list()
            left.complete = pd.notna(left.n_rx_fxs) and len(left.tx_hst) == left.n_rx_fxs

            # Rooms from the combined calendar
            rooms = left.tx_cal['Last_Name'].dropna()
            left.rooms = sorted(rooms.unique())
            left.room = rooms.iloc[0] if len(rooms) else ''

            # QCLs re-sliced over the combined span, then the oldest incomplete one
            left.qcls = db_pull['qcls'][
                db_pull['qcls']['Due_DtTm'].between(left.tx_cal_dts[0], left.tx_cal_dts[-1])]
            pending = left.qcls[left.qcls['Complete'] == 0].sort_values(
                ['Due_DtTm', 'Description'])
            if pending.empty:
                left.pending_qcls = ''
            else:
                top = pending.iloc[0]
                left.pending_qcls = (
                    f"{top['Due_DtTm'].strftime('%Y-%m-%d')} - {top['Description']}")

            # CC notes re-sliced from the combined first treated fraction. Both sides have treated
            # fractions (the pairing requires alternating treatments), so tx_hst_dts is not empty.
            left.cc_notes = db_pull['cc_notes'][
                (db_pull['cc_notes']['Create_DtTm'] > left.tx_hst_dts[0]) &
                (db_pull['cc_notes']['Create_DtTm'] <= left.tx_cal_dts[-1])]

            # SBRT status and ICC state over both sides, by the same rules as Site.__init__
            th = config.machine_thresholds(self.thresholds.get(left.room) or {})
            left.sbrt = (left.rx_fx_dose_rbe or left.rx_fx_dose) >= th['sbrt_fx_dose_cgy']
            missed_fx = th['sbrt_missed_fx'] if left.sbrt else th['icc_missed_fx']
            left.icc_completion_dt = (
                left.cc_notes['Create_DtTm'].min() if len(left.cc_notes) else None)
            left.icc_missed = len(left.tx_hst_dts) >= missed_fx and (
                left.icc_completion_dt is None
                or left.icc_completion_dt > left.tx_hst_dts[missed_fx - 1])
            if left.icc_missed:
                left.icc_completion_dt = None
            left.icc_due = left.icc_completion_dt is None and not left.icc_missed
            self.sites.remove(right)

    def count_wccs(self):
        """Count completed and missed weekly CCs.

        Loops over 5-fraction intervals starting after the first treated fraction. If a CC note is
        found in the window it is counted, duplicates are ignored, and if no note is in the
        window once its closing treated fraction (5 after its opening one) is delivered, the check
        is counted as missed. wcc_due is set True when the most recent opened
        window has no completing note.
        """

        # Loop over 5 fraction intervals. The first window opens after the first treated fraction.
        for fx_n in range(1, len(self.tx_cal_dts), 5):
            if fx_n <= len(self.tx_hst_dts):
                start = self.tx_hst_dts[fx_n - 1]

                # End of interval is:
                #   - Next 5th treated fraction or
                #   - Next 5th scheduled fraction or
                #   - Last scheduled fraction + 5 business days
                if fx_n + 5 <= len(self.tx_hst_dts):
                    end = self.tx_hst_dts[fx_n + 4]
                elif fx_n + 5 <= len(self.tx_cal_dts):
                    end = self.tx_cal_dts[fx_n + 4]
                else:
                    end = self.tx_cal_dts[-1] + pd.offsets.BusinessDay(5)
                if (
                        (self.wcc_notes['Create_DtTm'] > start) &
                        (self.wcc_notes['Create_DtTm'] < end)).any():
                    self.n_wcc_completed += 1
                    self.wcc_due = False
                else:
                    self.wcc_due = True
                    if fx_n + 5 <= len(self.tx_hst_dts):
                        self.n_wcc_missed += 1


class Site:
    """Calculates and holds all data needed for a dashboard row.

    Site-level audits run on this object; course-level audit results are copied into
    site.warnings alongside them.

    Attributes:
        course_n (int): Course number (table: PatCPlan, col: Course)
        site_name (str): Site name (table: Site, col: Site_Name)
        warnings (list[dict[str, str]]): CHECKS entries (id, label, field, level, message)
            flagged for the site
        n_rx_fxs (int | float): Prescribed fractions, NaN when NULL (table: DoseSummarySnapshot,
            col: RxFractions)
        rx_fx_dose (float): Per-fraction physical dose (cGy) (table: DoseSummarySnapshot)
        rx_fx_dose_rbe (float): Per-fraction RBE dose (cGy) (table: DoseSummarySnapshot)
        rx_dose (float): Total physical dose (cGy) (table: DoseSummarySnapshot)
        rx_dose_rbe (float): Total RBE dose (cGy) (table: DoseSummarySnapshot)
        rx (str | None): Formatted Rx string, e.g. '200 cGy x 25 = 5000 cGy'; '' when the site
            has no DSS snapshot, None when the Rx is incomplete
        tx_hst (pd.DataFrame): Treatment history (table: Dose_Hst)
        tx_hst_dts (list[pd.Timestamp]): tx_hst['Tx_DtTm'] as a list
        complete (bool): True when treated fractions equal prescribed fractions
        tx_cal (pd.DataFrame): Scheduled and treated fractions, one row per session
            (table: PatTxCal)
        tx_cal_dts (list[pd.Timestamp]): Treated, else scheduled, fraction times (table: PatCItem,
            col: Act_DtTm/Due_DtTm)
        room (str): Treatment room, '' when no scheduled field carries one (table: Staff,
            col: Last_Name)
        rooms (list[str]): All treatment rooms for this site, sorted (table: Staff, col: Last_Name)
        qcls (pd.DataFrame): QCLs sliced to this site's span (table: Chklist)
        pending_qcls (str): Most pressing incomplete QCL as '<ISO due date> - <desc>', '' if none
        cc_notes (pd.DataFrame): eChart Check notes sliced to this site's span (table: Notes)
        sbrt (bool): True when the per-fraction RBE dose (physical dose when RBE is 0) is at or
            above the machine's SBRT threshold
        icc_completion_dt (pd.Timestamp | None): Initial-CC eChart Check note time; None when
            no note exists, or when missed (the first note logged after the missed-threshold
            fraction)
        icc_due (bool): Indicates an ICC needs to be completed
        icc_missed (bool): True when no ICC note exists by the machine's missed-threshold fraction,
            or the note was logged after it, or the course completed with no note
    """

    def __init__(self, course, site, db_pull):
        """Build one dashboard row from a site record and the course's DB pull slice.

        Extracts the current Rx from the latest dose-summary snapshot, the treated/scheduled
        fraction times, room, pending QCLs and CC notes, then derives SBRT status and ICC
        completion/missed timing. Leaves self.rx empty when the site has no snapshot.

        Args:
            course (Course): Parent course
            site (pd.Series): One row of the course's sites DataFrame (table: Site)
            db_pull (dict[str, pd.DataFrame | pd.Timestamp]): Per-patient DB pull from query_db
        """
        self.course_n = course.course_n
        self.site_name = site['Site_Name']

        # Slice for most recent dose summary snapshot (DSS) to extract the current site Rx. A site
        # with no DSS snapshot has no Rx and is skipped by the caller.
        dss = db_pull['dss'][db_pull['dss']['SIT_ID'] == site['SIT_ID']]
        rx_rows = dss.sort_values('Create_DtTm').drop_duplicates('SIT_ID', keep='last')
        if rx_rows.empty:
            self.rx = ''
            return
        rx_row = rx_rows.iloc[0]

        # A NULL Rx field (NaN, or None when the whole column is NULL) must not raise and blank the
        # board: fractions become NaN for the pd.notna guards below, doses 0 so the RBE-or-physical
        # fallbacks apply.
        n_rx_fxs = rx_row['RxFractions']
        self.n_rx_fxs = int(n_rx_fxs) if pd.notna(n_rx_fxs) else float('nan')
        self.rx_fx_dose = _dose(rx_row['RxFxUniformDoseIncGray'])
        self.rx_fx_dose_rbe = _dose(rx_row['RxFxUniformDoseInCcGE'])
        self.rx_dose = _dose(rx_row['RxTotalDoseIncGray'])
        self.rx_dose_rbe = _dose(rx_row['RxTotalDoseInCcGE'])

        # Get formatted Rx string based on the prescription dose and fraction fields.
        self.rx = self.format_rx()

        # Slice dose_history by site and dedup on treated fraction number (Fx_Number_InEffect).
        dose_history = db_pull['dose_history'][
            db_pull['dose_history']['SIT_ID'] == site['SIT_ID']]
        self.tx_hst = (dose_history[dose_history['Fx_Number_InEffect'] > 0]
                       .sort_values('Tx_DtTm')
                       .drop_duplicates('Fx_Number_InEffect', keep='last')
                       .sort_values('Fx_Number_InEffect'))

        # Slice treatment calendar by site and dedup on shared session ID (PCI_ID). Note that
        # treatment calendar due times are substituted with treatment times when both are present
        # for a given record.
        self.tx_cal = (db_pull['calendar'][db_pull['calendar']['SIT_ID'] == site['SIT_ID']]
                       .sort_values('Last_Name')
                       .drop_duplicates('PCI_ID')
                       .assign(dt=lambda d: d['Act_DtTm'].fillna(d['Due_DtTm']))
                       .sort_values('dt'))
        self.tx_hst_dts = self.tx_hst['Tx_DtTm'].to_list()
        self.tx_cal_dts = self.tx_cal['dt'].to_list()

        # Site is complete when treated fractions equal the prescribed count.
        self.complete = pd.notna(self.n_rx_fxs) and len(self.tx_hst) == self.n_rx_fxs

        # Get treatment room(s), blank when no scheduled field carries a machine.
        rooms = self.tx_cal['Last_Name'].dropna()
        self.rooms = sorted(rooms.unique())
        self.room = rooms.iloc[0] if len(rooms) else ''

        # Slice QCLs to those whose due date falls within the site's scheduled treatment span.
        self.qcls = db_pull['qcls'][
            db_pull['qcls']['Due_DtTm'].between(self.tx_cal_dts[0], self.tx_cal_dts[-1])]

        # Oldest incomplete (Complete == 0) QCL; the oldest due date is the most pressing.
        # Value is '<ISO due date> - <description>' for the board formatter, '' when none pending.
        pending = self.qcls[self.qcls['Complete'] == 0].sort_values(['Due_DtTm', 'Description'])
        if pending.empty:
            self.pending_qcls = ''
        else:
            top = pending.iloc[0]
            self.pending_qcls = f"{top['Due_DtTm'].strftime('%Y-%m-%d')} - {top['Description']}"

        # Slice CC notes to the site's treatment span. A note cannot count as a chart check until
        # the site's first fraction is delivered, so the window opens at the first treated
        # fraction.
        if self.tx_hst_dts:
            self.cc_notes = db_pull['cc_notes'][
                (db_pull['cc_notes']['Create_DtTm'] > self.tx_hst_dts[0]) &
                (db_pull['cc_notes']['Create_DtTm'] <= self.tx_cal_dts[-1])]
        else:
            self.cc_notes = db_pull['cc_notes'].iloc[0:0]

        # Determine SBRT status and the machine-specific missed threshold. Thresholds are keyed by
        # room and fall back to the code defaults when the room has no profile. The RBE dose is 0
        # when BED does not apply (photons), so the physical dose is used then.
        th = config.machine_thresholds(course.thresholds.get(self.room) or {})
        self.sbrt = (self.rx_fx_dose_rbe or self.rx_fx_dose) >= th['sbrt_fx_dose_cgy']
        missed_fx = th['sbrt_missed_fx'] if self.sbrt else th['icc_missed_fx']

        # ICC completion occurs when the first CC note is logged. ICC is considered missed once the
        # missed-threshold fraction has been delivered with no note, or the first note is logged
        # after that fraction.
        self.icc_completion_dt = None
        self.icc_missed = False
        self.icc_due = False
        if len(self.tx_hst_dts) > 0:
            self.icc_due = True
            if len(self.cc_notes) == 0:
                if len(self.tx_hst_dts) >= missed_fx:
                    self.icc_missed = True
                    self.icc_due = False
            else:
                self.icc_completion_dt = self.cc_notes['Create_DtTm'].min()
                self.icc_due = False
                if (len(self.tx_hst_dts) >= missed_fx and
                        self.icc_completion_dt > self.tx_hst_dts[missed_fx - 1]):
                    self.icc_missed = True
                    self.icc_completion_dt = None

    def format_rx(self):
        """Format the prescription as '<fx dose> cGy x <n> = <total> cGy'.

        Uses RBE dose when present, else physical dose. Returns None when any of fractions,
        per-fraction dose or total dose is missing or zero.

        Returns:
            str | None: Formatted Rx string, or None when the Rx is incomplete
        """
        n = self.n_rx_fxs
        fx = self.rx_fx_dose_rbe or self.rx_fx_dose
        tot = self.rx_dose_rbe or self.rx_dose
        if pd.isna(n) or not n or not fx or not tot:
            return None
        return f'{fx:.0f} cGy x {n:.0f} = {tot:.0f} cGy'


def _dose(v):
    """A DSS dose field (cGy) as a float, 0.0 when NULL (NaN or None)"""
    return 0.0 if pd.isna(v) else float(v)
