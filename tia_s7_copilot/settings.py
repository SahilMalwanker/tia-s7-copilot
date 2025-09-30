"""Central configuration for TIA S7 Copilot.

Every value can be overridden with an environment variable or a ``.env`` file in the
project root (see ``.env.example``). Nothing secret is stored in the source code.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional; plain environment variables still work
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name, default):
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


# ============================================
# RUNTIME STATE (shared JSON files between both services)
# ============================================

DATA_DIR = Path(os.getenv("COPILOT_DATA_DIR") or PROJECT_ROOT / "data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

WEB_DIR = PACKAGE_DIR / "web"


def data_path(filename):
    """Absolute path of a shared state file inside the data directory."""
    return str(DATA_DIR / filename)


# ============================================
# PLC CONNECTION SETTINGS
# ============================================

PLC_IP = os.getenv("PLC_IP", "192.168.0.1")
PLC_RACK = _env_int("PLC_RACK", 0)
PLC_SLOT = _env_int("PLC_SLOT", 1)

# TIA Portal project used by TIA Openness (optional, Windows only)
TIA_PROJECT_PATH = os.getenv("TIA_PROJECT_PATH", r"C:\path\to\your\project.ap19")

# Connection timeout (seconds)
CONNECTION_TIMEOUT = 5

# ============================================
# DATA BLOCK CONFIGURATION
# ============================================

# Main control data block
MAIN_DB_NUMBER = 1
MAIN_DB_SIZE = 100  # bytes

# Tag addresses (will be auto-discovered by TIA Openness)
# You can manually add known addresses here for quick access
TAG_ADDRESSES = {
    # Example format:
    # "TagName": {"db": 1, "byte": 0, "bit": 0, "type": "bool"},
    # "Speed": {"db": 1, "byte": 2, "type": "int"},
}

# ============================================
# ROBOT ARM SETTINGS
# ============================================

ROBOT_ARM_PORTS = {
    "arm_1": os.getenv("ARM1_PORT", "COM3"),
    "arm_2": os.getenv("ARM2_PORT", "COM5"),
    "arm_3": os.getenv("ARM3_PORT", "COM4"),
}
ROBOT_ARM_BAUDRATE = 9600

# ============================================
# TIA OPENNESS SETTINGS
# ============================================

TIA_VERSION = os.getenv("TIA_VERSION", "V19")
TIA_PATH = os.getenv("TIA_OPENNESS_PATH", r"C:\Program Files\Siemens\Automation\Portal V19\PublicAPI\V19")

# ============================================
# LLM (Azure OpenAI chat completions)
# ============================================

# Full chat-completions URL, e.g.
# https://<resource>.openai.azure.com/openai/deployments/<deployment>/chat/completions?api-version=<version>
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")

# ============================================
# WEB SERVER
# ============================================

CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if origin.strip()
]

# ============================================
# HELPER FUNCTIONS
# ============================================

def get_plc_config():
    """Get PLC configuration"""
    return {
        "ip": PLC_IP,
        "rack": PLC_RACK,
        "slot": PLC_SLOT,
        "timeout": CONNECTION_TIMEOUT
    }

def get_tia_config():
    """Get TIA Openness configuration"""
    return {
        "project_path": TIA_PROJECT_PATH,
        "tia_version": TIA_VERSION,
        "tia_path": TIA_PATH
    }

def get_robot_config():
    """Get robot arm configuration"""
    return {
        "ports": ROBOT_ARM_PORTS,
        "baudrate": ROBOT_ARM_BAUDRATE
    }

# ============================================
# VALIDATION
# ============================================

def validate_config():
    """Validate configuration"""
    issues = []
    warnings = []

    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        warnings.append("⚠️ AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY not set")
        warnings.append("   AI answers will return a configuration error until they are set")

    # Check TIA project exists
    if not os.path.exists(TIA_PROJECT_PATH):
        warnings.append(f"⚠️ TIA project not found: {TIA_PROJECT_PATH}")
        warnings.append("   TIA Openness features will be disabled")

    # Check TIA path exists
    if not os.path.exists(TIA_PATH):
        warnings.append(f"⚠️ TIA Openness path not found: {TIA_PATH}")
        warnings.append("   TIA Openness features will be disabled")

    return issues, warnings

if __name__ == "__main__":
    print("Configuration Validation:")
    print("=" * 60)

    issues, warnings = validate_config()

    if issues:
        print("\n❌ Configuration Issues:")
        for issue in issues:
            print(f"  {issue}")

    if warnings:
        print("\n⚠️ Configuration Warnings:")
        for warning in warnings:
            print(f"  {warning}")
        print("\n  System will run in Snap7-only mode (PLC control still available)")

    if not issues and not warnings:
        print("✅ Configuration Valid - TIA Openness ready!")

    print("\n" + "=" * 60)
    print("\nPLC Config:", get_plc_config())
    print("TIA Config:", get_tia_config())
    print("Robot Config:", get_robot_config())
    print("Data directory:", DATA_DIR)
