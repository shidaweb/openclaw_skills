#!/usr/bin/env python3
"""Wrapper: show pipeline progress (run from this skill directory)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _outreach_core.helpers.pipeline_status import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "linkedin-outreach", *sys.argv[1:]]
    main()
