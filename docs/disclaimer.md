# Disclaimer

## Not a medical device

**This software is NOT a medical device.** It has not been reviewed, cleared, approved, certified,
or registered by the U.S. Food and Drug Administration (FDA), under the EU Medical Device Regulation
(MDR 2017/745), or by any other regulatory authority, and it carries no CE marking. It is **not
intended for diagnosis, treatment, or the prevention of disease**, and it must **not** be used as a
basis for any clinical decision.

The software is provided **"AS IS", WITHOUT WARRANTY OF ANY KIND**, express or implied, including
but not limited to the warranties of merchantability, fitness for a particular purpose, accuracy,
and non-infringement (see the [LICENSE](../LICENSE)). It is offered as an **aid** to a qualified
medical physicist's own chart review — a warning-focused audit tool — and it does **not** replace
the independent professional judgment, verification, and secondary checks required by your clinic's
quality-assurance program and applicable professional standards.

**The user is solely responsible for commissioning and validating this software** in their own
clinical environment, against their own database, workflows, and standards, before placing any
reliance on its output, and for revalidating it after any update, configuration change, or change to
the source data. The authors and contributors accept **no liability** for any loss, harm, or damage
of any kind arising from its use or misuse. **Use it entirely at your own risk.**

## No clinical reliance

The software reports whether certain records exist in a database and when they were created. It
does not read the content of those records, does not evaluate a treatment plan, a prescription, a
delivered dose, or any clinical parameter, and does not and cannot determine whether a patient's
treatment is correct, safe, or appropriate. A green row, an absent finding, or any other output
of this software is **not** a statement that a chart has been reviewed, that a check was performed
correctly, that a plan is acceptable, or that a patient may be treated. Every clinical decision,
including the decision to treat, to continue treatment, to bill, or to consider a chart check
complete, remains entirely with the qualified professionals responsible for that patient under
your institution's policies and applicable law.

## Commissioning, validation and configuration

The rules this software applies are configurable and depend on institutional conventions, on the
contents and conventions of your Mosaiq database, and on values entered by you or your clinic in
configuration files and in the Settings dialog. A configuration value that does not match your
database does not produce an error; it produces findings that are missing, duplicated, or wrong.
You are solely responsible for: verifying every configuration value against your database;
commissioning the software against known cases before any use; validating it again after every
software update, configuration change, database upgrade, change in clinical workflow, or change in
the way records are entered; and withdrawing it from use if it cannot be validated. The regression
tests and demo data shipped with the software demonstrate its behavior on synthetic data only and
do not constitute validation in your environment.

## Data, privacy and security

The software reads protected health information from your clinical database and displays it on
screen, holds it in memory, and, when a bug report is saved, writes it to disk at a location you
configure. You are solely responsible for: ensuring that your use of the software complies with
HIPAA, GDPR, and every other law, regulation, and institutional policy that applies to you; the
access rights of the account used to connect to the database; the security of the machine on which
the software runs and of every location to which it writes; and the handling, retention, and
disposal of any file it produces. The authors and contributors have no access to your data, no
control over your environment, and no responsibility for any disclosure, loss, or misuse of
information arising from your use of the software.

## Third-party components and the Mosaiq system

The software depends on third-party open-source components, each under its own license, and on
the structure and behavior of a third-party clinical information system that the authors do not
control. A change to any of them may change or break the software's behavior without notice. The
authors and contributors make no representation about the suitability, availability, or continued
compatibility of any third-party component or system, and are not affiliated with, endorsed by, or
acting on behalf of the vendor of any clinical information system.

## No obligation of support or maintenance

The software is released without any obligation to provide support, maintenance, updates,
corrections, or notice of defects. Known and unknown defects may exist in any release. Bug reports
and suggestions are welcome and may be acted on at the authors' sole discretion, and no response,
schedule, or outcome is promised.

## Modified versions

Any version of the software that has been modified, configured, rebuilt, or redistributed by
anyone other than the authors, including any fork, pull request, or local change, is the sole
responsibility of the person who made the change. Every protection this disclaimer gives the
original authors and contributors continues to apply, in full, to any such version.

## Limitation of liability

To the fullest extent permitted by applicable law, in no event shall the authors, contributors,
copyright holders, or their employers be liable for any claim, damages, or other liability,
whether in an action of contract, tort, negligence, strict liability, or otherwise, arising from,
out of, or in connection with the software, its documentation, its configuration, or the use or
other dealings in the software, including without limitation any direct, indirect, incidental,
special, exemplary, consequential, or punitive damages, any personal injury, any clinical outcome,
any loss of data, any regulatory finding, any billing error, or any loss of revenue or profit, even
if advised of the possibility of such damages. Where applicable law does not permit the exclusion
of certain liabilities, the liability of the authors and contributors is limited to the greatest
extent that law permits.

## Indemnity

By using the software you agree to indemnify, defend, and hold harmless the authors, contributors,
copyright holders, and their employers from and against any and all claims, liabilities, damages,
losses, costs, and expenses, including reasonable legal fees, arising from or related to your use,
configuration, or distribution of the software, your handling of any data it reads or writes, or
your breach of this disclaimer or of the license.

## Acceptance

Installing, running, configuring, modifying, or distributing the software constitutes acceptance
of this disclaimer in its entirety and of the terms of the [LICENSE](../LICENSE). If you do not
accept them, do not use the software.
