"""Start a local Arize Phoenix server for t2pbi's traces, with its usage telemetry off.

    python scripts/phoenix_server.py          # then open http://localhost:6006
    # in another terminal, with T2PBI_PHOENIX=true in .env:
    t2pbi migrate samples\\Book1\\Book1.twb --out out\\run --refine --reference-images samples\\Book1\\screenshots

Phoenix's server sends usage telemetry by default (FullStory session recording and Scarf
pixels in its web UI). Traces of a migration carry the prompts, the report JSON and the
screenshots, client data for a client workbook, so this launcher always starts it with
``PHOENIX_TELEMETRY_ENABLED=false`` and ``PHOENIX_ALLOW_EXTERNAL_RESOURCES=false`` (no requests
to the public internet on its own initiative). It listens on localhost only, and keeps its
database in ``out/phoenix/`` (gitignored). Stop it with Ctrl+C.

Needs the ``trace`` extra: ``pip install -e ".[trace]"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from t2pbi.config import load_phoenix_config

_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    config = load_phoenix_config()
    exe = shutil.which("phoenix", path=str(Path(sys.executable).parent)) or shutil.which("phoenix")
    if exe is None:
        print('Phoenix isn\'t installed: pip install -e ".[trace]"', file=sys.stderr)
        return 2
    url = urlparse(config.endpoint)
    work = _ROOT / "out" / "phoenix"
    work.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "PHOENIX_TELEMETRY_ENABLED": "false",
        "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
        "PHOENIX_HOST": "127.0.0.1",
        "PHOENIX_PORT": str(url.port or 6006),
        "PHOENIX_WORKING_DIR": str(work),
    }
    print(f"Phoenix: {config.endpoint}  (telemetry off; data in {work})", file=sys.stderr)
    try:
        return subprocess.call([exe, "serve"], env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
