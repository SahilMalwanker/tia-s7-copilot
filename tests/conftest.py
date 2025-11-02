import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate tests from real hardware, credentials and the repository's data folder.
_DATA_DIR = tempfile.mkdtemp(prefix="tia-s7-copilot-tests-")
os.environ["COPILOT_DATA_DIR"] = _DATA_DIR
os.environ["PLC_IP"] = "127.0.0.1"
os.environ["PLC_RACK"] = "0"
os.environ["PLC_SLOT"] = "1"
os.environ["TIA_OPENNESS_DISABLED"] = "1"
os.environ["AZURE_OPENAI_ENDPOINT"] = ""
os.environ["AZURE_OPENAI_API_KEY"] = ""


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA_DIR, ignore_errors=True)
