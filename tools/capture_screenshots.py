"""Capture README screenshots of the dashboard with a mocked API (no PLC, robots or LLM needed).

    pip install playwright && playwright install chromium
    python tools/capture_screenshots.py                       # bundled Chromium, headless
    python tools/capture_screenshots.py --channel msedge --headed

All /api/* calls are answered from the mock state below; the page itself is served
from tia_s7_copilot/web exactly as the copilot service would serve it.
"""

import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "tia_s7_copilot" / "web"
OUT = ROOT / "docs" / "assets"
ORIGIN = "http://localhost:8000"

GREETING = (
    "Hi, I'm **TIA S7 Copilot**, the copilot for this three-arm PCB assembly cell. "
    "I can start, stop or reset production, change the robot speed, explain what each arm is doing "
    "and estimate energy and cost per part. What would you like to do?"
)

# Same text the backend builds in handle_command() for a 50% -> 80% change with arms 2+3 active.
SPEED_REPLY = "\n".join([
    "✅ **Robot speed successfully updated from 50% to 80%** (+30%)",
    "",
    "**🤖 Actual Robot Speeds:**",
    "- Move Speed: 6,420,000",
    "- Catch Speed: 660,000",
    "",
    "**📊 Production Impact:**",
    "- New cycle time: 69.2 seconds (was 80.0s)",
    "- Cycle time change: -10.8 seconds (-13.5%)",
    "- New throughput: 52.0 parts/hour",
    "- Throughput change: +7.0 parts/hour (+15.6%)",
    "- Daily production change: 56 parts/day (8h shift)",
    "",
    "**⚡ Power Impact Analysis:**",
    "- Power mode change: LOW SPEED → HIGH SPEED",
    "- Arm power: 150W → 250W per active arm",
    "- 🔴 **CRITICAL POWER TIER CHANGE!** Crossed 50% threshold",
    "- Power change per arm: +100W",
    "- **Immediate power change:** +200W (affecting 2 active arms)",
    "",
    "**💰 Energy Cost Impact:**",
    "- **POWER TIER CHANGED:** Cost impact when arms are active",
    "- Potential hourly cost change: €+0.045/hour",
    "- Potential daily cost change: €+1.08/day",
    "- **Immediate cost change:** €+0.0300/hour (2 active arms)",
    "",
    "*The speed change has been applied and will take effect on the next robot operation cycle.*",
])

ENERGY_REPLY = "\n".join([
    "**Current system power: 509 W** (robot speed 80 % · HIGH SPEED mode)",
    "",
    "| Component | State | Power |",
    "|---|---|---|",
    "| Arm 1 | idle | 5 W |",
    "| Arm 2 | assembling | 250 W |",
    "| Arm 3 | assembling | 250 W |",
    "| Conveyor 1 | idle | 2 W |",
    "| Conveyor 2 | idle | 2 W |",
    "",
    "At this draw the cell uses **0.509 kWh per hour ≈ €0.076 / h** (€0.15 / kWh).",
    "Per assembled board (69.2 s cycle, production utilisation model) that is "
    "**0.0033 kWh ≈ €0.0005 per part**.",
    "",
    "Would you like to compare this with running at 50 % speed?",
])

PLC = {
    "Start": True, "Stop": False, "Conveyor_1": False, "Conveyor_2": False, "Total_Prod": 42,
    "Arm_1": False, "Arm_2": True, "Arm_3": True,
    "Proximity_Sensor1": False, "Proximity_Sensor2": True, "Proximity_Sensor3": False, "Proximity_Sensor4": False,
}

MOCK = {
    "/api/plc_status": PLC,
    "/api/robot_state": {"current_speed": 80, "overall_mode": "HIGH_SPEED",
                         "arms_active": {"arm_1": False, "arm_2": True, "arm_3": True}},
    "/api/robot_running_status": {"robots": {
        "arm_1": {"is_moving": False, "current_step": "IDLE", "progress": 0, "status": "STOPPED"},
        "arm_2": {"is_moving": True, "current_step": "ASSEMBLING", "progress": 95, "status": "MOVING"},
        "arm_3": {"is_moving": True, "current_step": "ASSEMBLING", "progress": 85, "status": "MOVING"},
    }, "system_active": True},
    "/api/energy_status": {"total_power": 509, "robots": {
        "arm_1": {"running": False, "power": 5, "status": "IDLE"},
        "arm_2": {"running": True, "power": 250, "status": "HIGH_SPEED"},
        "arm_3": {"running": True, "power": 250, "status": "HIGH_SPEED"},
    }, "conveyors": {
        "conveyor_1": {"running": False, "power": 2, "status": "IDLE"},
        "conveyor_2": {"running": False, "power": 2, "status": "IDLE"},
    }},
    "/api/prox_sensors": {k: v for k, v in PLC.items() if k.startswith("Proximity")},
    "/api/step_status": {"arms": {
        "arm_1": {"name": "IDLE", "progress": 0},
        "arm_2": {"name": "ASSEMBLING", "progress": 95},
        "arm_3": {"name": "ASSEMBLING", "progress": 85},
    }},
    "/api/voice/status": {"active": True, "listening": False, "last_command": None, "last_response": None},
    "/api/initial_greeting": {"user_message": "Who are you?", "ai_response": GREETING, "plc_status": PLC},
}


def handle(route, request):
    path = urlparse(request.url).path
    if path == "/api/command":
        command = json.loads(request.post_data or "{}").get("command", "").lower()
        reply = SPEED_REPLY if "speed" in command else ENERGY_REPLY
        body = {"response": reply, "plc_status": PLC}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if path.startswith("/api/"):
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(MOCK.get(path, {})))
    file = WEB / ("index.html" if path in ("", "/") else path.lstrip("/"))
    if not file.is_file():
        return route.fulfill(status=404, body="not found")
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    return route.fulfill(status=200, content_type=mime, body=file.read_bytes())


def ask(page, text):
    before = page.locator(".message-bubble.ai").count()
    page.fill("#userInput", text)
    page.press("#userInput", "Enter")
    page.wait_for_function(f"document.querySelectorAll('.message-bubble.ai').length > {before}")
    page.wait_for_timeout(700)
    # show the question at the top of the conversation instead of the end of a long answer
    page.evaluate("""() => {
        const box = document.getElementById('chatMessages');
        const users = box.querySelectorAll('.message-bubble.user');
        box.scrollTop = users[users.length - 1].offsetTop - box.offsetTop - 16;
    }""")
    page.wait_for_timeout(300)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=None, help="browser channel, e.g. msedge or chrome")
    parser.add_argument("--headed", action="store_true", help="run headed (window is moved off-screen)")
    parser.add_argument("--scale", type=float, default=1.5, help="device scale factor")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=args.channel,
            headless=not args.headed,
            args=["--window-position=-3000,0"] if args.headed else [],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=args.scale)
        page = context.new_page()
        page.route(f"{ORIGIN}/**", handle)
        page.goto(f"{ORIGIN}/")
        page.wait_for_selector(".message-bubble.ai")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(3500)  # let the polling loops fill the telemetry column

        ask(page, "Set speed to 80%")
        page.screenshot(path=str(OUT / "dashboard-light.png"))

        page.click("#themeSwitcher")
        ask(page, "Show me energy consumption")
        page.screenshot(path=str(OUT / "dashboard-dark.png"))

        browser.close()
    print("screenshots written to", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
