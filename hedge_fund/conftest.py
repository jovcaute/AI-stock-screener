"""Load .env for v2 tests so FINANCIAL_DATASETS_API_KEY is available."""

import matplotlib
from dotenv import load_dotenv

# Force the non-interactive backend before any test imports pyplot — the
# default GUI backend (TkAgg on Windows) intermittently throws TclError
# under pytest, since there's no real display/event loop driving it.
matplotlib.use("Agg")

load_dotenv()
