# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Bundled demo the board runs against in demo mode (app.DEMO_MODE)

No Mosaiq access or patient data needed.

- demo.db: mock Mosaiq DB, one made-up patient per test-corpus scenario (built by the
  maintainers from tests/snapshots; the generator is not part of the release)
- clinic_config.toml: the demo clinic's rooms, QCL task types and charge code
- db_config.toml: dummy blank DB config read and written in place of the live one
- app_state.json, user_config.toml: demo UI state and Settings, created at runtime (not committed)
"""

from pathlib import Path

import pandas as pd

DEMO_DIR = Path(__file__).parent
DEMO_DB = DEMO_DIR / 'demo.db'

# Pinned board clock: the demo pull, findings and date window are all computed as of this time.
DEMO_NOW = pd.Timestamp('2024-03-22 12:00:00')
