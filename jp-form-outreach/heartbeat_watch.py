#!/usr/bin/env python3
"""Start background Slack heartbeat for jp-form-outreach."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _outreach_core.helpers.heartbeat_watch import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "jp-form-outreach", *sys.argv[1:]]
    main()
