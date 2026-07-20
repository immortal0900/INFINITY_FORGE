from __future__ import annotations

import os
import sys
from pathlib import Path


repo = os.environ.get("INFINITY_FORGE_REPO")
if repo:
    sys.path.insert(0, repo)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forge.ops.subscription_setup import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
