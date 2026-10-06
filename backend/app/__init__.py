"""Application package."""

import os
import tempfile

_matplotlib_cache = os.path.join(tempfile.gettempdir(), "control-combination-matplotlib")
os.makedirs(_matplotlib_cache, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", _matplotlib_cache)
os.environ.setdefault("MPLBACKEND", "Agg")
