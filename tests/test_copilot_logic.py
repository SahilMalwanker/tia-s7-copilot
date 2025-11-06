import json
import os
from pathlib import Path

import pytest

import tia_s7_copilot.api as api
from tia_s7_copilot import prompts, settings

OPTIONAL_MODULES = ["ENERGY_CONTEXT", "COST_CONTEXT", "SPEED_CONTEXT", "PRODUCTION_CONTEXT", "SCENARIO_CONTEXT", "STATUS_CONTEXT"]
CYCLE_POINTS = [(30, 100), (50, 80), (75, 70), (100, 66)]


def set_speed(percent):
    with open(api.SPEED_FILE, "w") as f:
        json.dump({"speed_percentage": percent}, f)
    api._speed_cache["timestamp"] = 0


def selected_modules(question):
    context = api.select_contexts_for_question(question)
    return context, {name for name in OPTIONAL_MODULES if getattr(prompts, name) in context}


# ---------- configuration ----------

def test_state_files_live_in_the_configured_data_dir():
    assert settings.DATA_DIR == Path(os.environ["COPILOT_DATA_DIR"]).resolve()
    assert Path(api.SPEED_FILE).parent == settings.DATA_DIR
    assert Path(api.step_tracker.data_file).parent == settings.DATA_DIR


def test_plc_address_comes_from_settings():
    assert settings.get_plc_config() == {"ip": "127.0.0.1", "rack": 0, "slot": 1, "timeout": 5}
    assert (api.PLC_IP, api.PLC_RACK, api.PLC_SLOT) == ("127.0.0.1", 0, 1)
    assert set(settings.get_robot_config()["ports"]) == {"arm_1", "arm_2", "arm_3"}


def test_no_credentials_are_baked_into_the_code():
    assert api.API_KEY == "" and api.ENDPOINT == ""


# ---------- smart context selection ----------

def test_core_and_question_type_modules_are_always_sent():
    context, modules = selected_modules("hello")
    assert context.startswith(prompts.CORE_CONTEXT)
    assert prompts.QUESTION_TYPE_CONTEXT in context
    assert modules == set()


def test_run_for_hours_question_adds_energy_production_and_cost():
    _, modules = selected_modules("If we run for 20 hours, how much will it cost?")
    assert {"ENERGY_CONTEXT", "PRODUCTION_CONTEXT", "COST_CONTEXT"} <= modules


def test_speed_command_adds_speed_module():
    _, modules = selected_modules("Set speed to 80%")
    assert "SPEED_CONTEXT" in modules


def test_filtered_prompt_is_far_smaller_than_sending_every_module():
    small, _ = selected_modules("hello")
    everything = "\n\n".join(getattr(prompts, name) for name in ["CORE_CONTEXT", *OPTIONAL_MODULES, "QUESTION_TYPE_CONTEXT"])
    assert len(small) < 0.5 * len(everything)


# ---------- command intent parsing ----------

@pytest.mark.parametrize(
    "command, expected",
    [
        ("set speed to 80%", {"type": "absolute", "amount": 80}),
        ("increase speed by 10%", {"type": "percentage", "direction": "increase", "amount": 10}),
        ("slow down", {"type": "percentage", "direction": "decrease", "amount": 20}),
        ("full speed", {"type": "absolute", "amount": 100}),
    ],
)
def test_speed_change_commands(command, expected):
    assert api.extract_speed_change_intent(command) == expected


def test_what_if_questions_never_change_the_speed():
    assert api.extract_speed_change_intent("what happens if i increase speed by 20%?") is None
    assert api.extract_speed_simulation_intent("what if i increase speed by 20%") == {
        "type": "simulation",
        "direction": "increase",
        "amount": 20,
    }


def test_pending_speed_confirmation_is_found_in_history():
    history = [
        {"role": "user", "content": "What if I go faster?"},
        {"role": "assistant", "content": "... Would you like me to increase the speed to 70%?"},
    ]
    assert api.extract_pending_speed_from_history(history) == 70
    assert api.extract_pending_speed_from_history([]) is None


def test_scenario_phrasing_is_parsed_into_component_states():
    arms, conveyors = api.extract_custom_power_intent("what if only arm 2 is active")
    assert arms == {1: False, 2: True, 3: False}
    assert conveyors == {1: False, 2: False}


# ---------- production, timer and power models ----------

@pytest.mark.parametrize(
    "speed, seconds",
    [(10, 100), (30, 100), (40, 90), (50, 80), (62.5, 75), (80, 69.2), (100, 66), (120, 66)],
)
def test_cycle_time_interpolation(speed, seconds):
    assert api.interpolate_cycle_time(speed, CYCLE_POINTS) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "speed, prox1_ms, prox3_ms",
    [(0, 8000, 9000), (50, 5500, 7000), (80, 4000, 5800), (100, 3000, 5000), (150, 3000, 5000)],
)
def test_conveyor_timers_scale_with_speed_and_stay_clamped(speed, prox1_ms, prox3_ms):
    timers = api.calculate_dynamic_timers(speed)
    assert (timers["prox1_timer_ms"], timers["prox3_timer_ms"]) == (prox1_ms, prox3_ms)


def test_idle_cell_draws_19_watts():
    set_speed(80)
    total, details = api.calculate_power_for_custom_state({1: False, 2: False, 3: False}, {1: False, 2: False})
    assert total == 19
    assert len(details) == 5


def test_arm_power_tier_switches_above_50_percent():
    everything_on = ({1: True, 2: True, 3: True}, {1: True, 2: True})
    set_speed(50)
    low, _ = api.calculate_power_for_custom_state(*everything_on)
    set_speed(51)
    high, _ = api.calculate_power_for_custom_state(*everything_on)
    assert (low, high) == (3 * 150 + 2 * 50, 3 * 250 + 2 * 50)


def test_assembly_stage_from_step_names():
    def status(a1, a2, a3):
        return {"arms": {f"arm_{i}": {"name": n, "progress": 0} for i, n in enumerate([a1, a2, a3], 1)}}

    assert api.determine_assembly_stage(status("IDLE", "IDLE", "IDLE")) == "SYSTEM_IDLE"
    assert api.determine_assembly_stage(status("PICKUP_POSITION", "IDLE", "IDLE")) == "HOLDER_PLACEMENT_IN_PROGRESS"
    assert api.determine_assembly_stage(status("IDLE", "ASSEMBLING", "ASSEMBLING")) == "PCB_ASSEMBLY_IN_PROGRESS"


def test_scenario_generator_enumerates_every_on_off_combination():
    scenarios = list(api.scenario_generator())
    assert len(scenarios) == 32
    assert len({tuple(s.values()) for s in scenarios}) == 32
