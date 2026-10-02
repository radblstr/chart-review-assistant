# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Regression test suite for the Physics Chart Review Assistant

Run it all with:   python -m pytest chart_review_assistant/tests -q
No live database and no PHI: each snapshot test replays a self-contained case directory -- mock
Mosaiq DB, clinic_config.toml, state file, baseline JSON, committed under tests/snapshots -- so
the suite runs anywhere.
"""
