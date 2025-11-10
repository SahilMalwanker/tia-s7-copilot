import json
import py_compile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tia_s7_copilot.api as api

PACKAGE = Path(api.__file__).resolve().parent
client = TestClient(api.app)

# Every element id the dashboard scripts rely on (kept identical through the redesign).
DASHBOARD_IDS = [
    "intelligenceList", "current-stage", "next-action", "overall-progress",
    "plcStatusList", "operationMode", "energyList", "totalPowerValue",
    "robotEnergyList", "conveyorEnergyList",
    "arm1-power", "arm1-step", "arm1-progress", "arm1-progress-text", "arm1-step-detailed", "arm1-progress-bar",
    "arm2-power", "arm2-step", "arm2-progress", "arm2-progress-text", "arm2-step-detailed", "arm2-progress-bar",
    "arm3-power", "arm3-step", "arm3-progress", "arm3-progress-text", "arm3-step-detailed", "arm3-progress-bar",
    "active-arms", "total-progress-summary", "current-operation",
    "themeSwitcher", "connectionStatus", "connectionText", "chatMessages", "smartSuggestions",
    "chatForm", "userInput", "micButton", "sendButton",
]


def set_speed(percent):
    with open(api.SPEED_FILE, "w") as f:
        json.dump({"speed_percentage": percent}, f)
    api._speed_cache["timestamp"] = 0


def test_dashboard_is_served_at_root():
    response = client.get("/")
    assert response.status_code == 200
    assert "TIA S7 Copilot" in response.text


def test_dashboard_keeps_every_element_the_scripts_use():
    html = (PACKAGE / "web" / "index.html").read_text(encoding="utf-8")
    missing = [element_id for element_id in DASHBOARD_IDS if f'id="{element_id}"' not in html]
    assert missing == []


def test_all_scenarios_lists_every_combination_with_power():
    set_speed(80)
    scenarios = client.get("/api/all_scenarios").json()
    powers = [s["power"] for s in scenarios]
    assert len(scenarios) == 32
    assert (min(powers), max(powers)) == (19, 850)


def test_custom_scenarios_filters_and_validates_input():
    set_speed(80)
    match = client.get("/api/custom_scenarios", params={"arms": "1,1,1", "conveyors": "1,1"})
    assert [s["power"] for s in match.json()] == [850]
    assert client.get("/api/custom_scenarios", params={"arms": "1,1"}).status_code == 400


def test_speed_percentage_endpoint_reads_the_shared_file():
    set_speed(65)
    assert client.get("/api/robot_speed_percentage").json() == {"speed_percentage": 65, "display_text": "65%"}


def test_robot_state_reports_idle_when_no_arm_moves():
    set_speed(80)
    body = client.get("/api/robot_state").json()
    assert body["current_speed"] == 80
    assert body["overall_mode"] == "IDLE"


def test_offline_plc_reports_safe_defaults():
    body = client.get("/api/plc_status").json()
    assert body["Start"] is False and body["Stop"] is False
    assert body["Total_Prod"] == 0


def test_tia_endpoints_degrade_gracefully_without_tia_portal():
    assert client.get("/api/tia/status").json()["connected"] is False
    assert client.get("/api/tia/tags").status_code == 503


def test_step_status_lists_all_three_arms():
    arms = client.get("/api/step_status").json()["arms"]
    assert set(arms) == {"arm_1", "arm_2", "arm_3"}


@pytest.mark.parametrize("module", ["robot_controller.py", "tia_intelligence.py", "voice_assistant.py"])
def test_hardware_modules_compile(module, tmp_path):
    # These modules talk to real hardware / Windows-only APIs on import, so only compile them here.
    py_compile.compile(str(PACKAGE / module), cfile=str(tmp_path / "out.pyc"), doraise=True)
