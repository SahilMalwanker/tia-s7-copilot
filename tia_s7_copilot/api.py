"""TIA S7 Copilot - copilot service (port 8000).

FastAPI app that serves the operator dashboard, routes natural-language commands
through the LLM, reads/writes the S7-1500 PLC via Snap7, tracks assembly steps and
derives production/energy analytics.

Run with:  python -m uvicorn tia_s7_copilot.api:app --host 0.0.0.0 --port 8000
"""

import snap7
import snap7.util
import requests
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from itertools import product
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
import threading
import time
import json
import os
import asyncio

from . import settings

# ============================================
# TIA OPENNESS INTEGRATION
# ============================================
try:
    from .settings import get_plc_config
    from .tia_intelligence import get_tia_intelligence, close_tia_intelligence
    TIA_INTEGRATION_AVAILABLE = True
    print("✅ TIA Openness integration modules loaded")
except ImportError as e:
    TIA_INTEGRATION_AVAILABLE = False
    print(f"⚠️ TIA Openness integration not available: {e}")
    print("   System will run in Snap7-only mode")

# ============================================
# CONVERSATION HISTORY MANAGEMENT
# ============================================
conversation_histories = {}  # Store conversations by session ID

class ConversationManager:
    def __init__(self, max_history=10):
        self.max_history = max_history
    
    def get_history(self, session_id):
        """Get conversation history for a session"""
        if session_id not in conversation_histories:
            conversation_histories[session_id] = []
        return conversation_histories[session_id]
    
    def add_message(self, session_id, role, content):
        """Add a message to conversation history"""
        history = self.get_history(session_id)
        history.append({"role": role, "content": content})
        
        # Keep only last N messages to avoid token limits
        if len(history) > self.max_history * 2:  # *2 because user+assistant pairs
            history = history[-self.max_history * 2:]
            conversation_histories[session_id] = history
    
    def clear_history(self, session_id):
        """Clear conversation history for a session"""
        if session_id in conversation_histories:
            del conversation_histories[session_id]

# Create global conversation manager
conversation_manager = ConversationManager(max_history=10)

SPEED_FILE = settings.data_path("robot_speed.json")
CYCLE_TIMING_FILE = settings.data_path("cycle_timing.json")

#  SIMPLIFIED STEP TRACKER - FIXED RECURSION ISSUES
class StepTracker:
    def __init__(self):
        self.current_steps = {
            1: {"name": "IDLE", "progress": 0, "start_time": None, "estimated_end": None},
            2: {"name": "IDLE", "progress": 0, "start_time": None, "estimated_end": None},
            3: {"name": "IDLE", "progress": 0, "start_time": None, "estimated_end": None}
        }
        self.step_history = []
        self.data_file = settings.data_path("step_tracking_data.json")
        self._loading = False  #  PREVENT RECURSION

        #  ADD LAST STATES FOR PLC MONITORING
        self.last_states = {
            "Start": False,
            "Stop": False,
            "Conveyor_1": False,
            "Conveyor_2": False,
            "Arm_1": False,
            "Arm_2": False,
            "Arm_3": False,
        }
        
        #  STEP DEFINITIONS - FOR REFERENCE ONLY
        self.step_definitions = {
            1: {  # Arm 1 sequences
                0: {"name": "IDLE", "estimated_duration": 0},
                1: {"name": "MOVING_TO_ORIGIN", "estimated_duration": 2},
                2: {"name": "PICKUP_POSITION", "estimated_duration": 2},
                3: {"name": "ACTIVATING_PUMP", "estimated_duration": 1},
                4: {"name": "PICKING_UP", "estimated_duration": 2},
                5: {"name": "LIFTING", "estimated_duration": 2},
                6: {"name": "TO_CONVEYOR", "estimated_duration": 3},
                7: {"name": "PLACING", "estimated_duration": 3},
                8: {"name": "RELEASING", "estimated_duration": 2},
                9: {"name": "RETURNING", "estimated_duration": 3}
            },
            2: {  # Arm 2 sequences
                0: {"name": "IDLE", "estimated_duration": 0},
                1: {"name": "INITIAL_POS", "estimated_duration": 2},
                2: {"name": "PICKUP_POINT", "estimated_duration": 3},
                3: {"name": "GRIPPING", "estimated_duration": 4},
                4: {"name": "LIFTING", "estimated_duration": 2},
                5: {"name": "TO_ASSEMBLY", "estimated_duration": 3},
                6: {"name": "WAITING_ARM3", "estimated_duration": 2},
                7: {"name": "ASSEMBLING", "estimated_duration": 5},
                8: {"name": "TO_CONVEYOR2", "estimated_duration": 3},
                9: {"name": "PLACING", "estimated_duration": 2},
                10: {"name": "RELEASING", "estimated_duration": 2},
                11: {"name": "RETURNING", "estimated_duration": 3}
            },
            3: {  # Arm 3 sequences
                0: {"name": "IDLE", "estimated_duration": 0},
                1: {"name": "PCB_PICKUP", "estimated_duration": 2},
                2: {"name": "PICKING_PCB", "estimated_duration": 3},
                3: {"name": "TO_ASSEMBLY", "estimated_duration": 3},
                4: {"name": "WAITING_ARM2", "estimated_duration": 2},
                5: {"name": "ASSEMBLING", "estimated_duration": 5},
                6: {"name": "TO_STORAGE", "estimated_duration": 2},
                7: {"name": "STORING", "estimated_duration": 2}
            }
        }
        #  ADD PROX SENSOR TRACKING
        self.prox_last_states = {
            "Proximity_Sensor1": False,
            "Proximity_Sensor2": False,
            "Proximity_Sensor3": False,
            "Proximity_Sensor4": False
        }
        
        #  ADD PROX-DRIVEN STEP MAPPINGS
        self.prox_step_mappings = {
            "Proximity_Sensor1": {
                "trigger_step": "HOLDER_ON_CONVEYOR",
                "affected_arms": [1],
                "action": "start_conveyor_1"
            },
            "Proximity_Sensor2": {
                "trigger_step": "HOLDER_AT_POINT_B", 
                "affected_arms": [1, 2],
                "action": "stop_conveyor_1_start_arm_2"
            },
            "Proximity_Sensor3": {
                "trigger_step": "ASSEMBLY_ON_CONVEYOR2",
                "affected_arms": [2, 3],
                "action": "start_conveyor_2"
            },
            "Proximity_Sensor4": {
                "trigger_step": "PRODUCTION_COMPLETE",
                "affected_arms": [2],
                "action": "stop_conveyor_2_increment_counter"
            }
        }
        
        # Load any existing data - ONLY ONCE
        self._load_from_file()
        print("🏭 Step Tracker initialized - DUAL MODE ENABLED")
        print("📝 Step tracking from robot_controller.py AND PLC monitoring")
        print("🌐 Webpage will show live updates")

        #  START AUTOMATIC MONITORING FOR WEBPAGE
        self.monitoring_thread = threading.Thread(target=self._monitor_plc_changes, daemon=True)
        self.monitoring_thread.start()

    def _monitor_plc_changes(self):
        """Monitor PLC state changes and update steps automatically for webpage"""
        while True:
            try:
                #  ENHANCED: Read prox sensors along with existing PLC states
                try:
                    current_prox_states = read_prox_sensors_from_plc()
                except NameError:
                    # Function not yet defined (initialization order issue)
                    current_prox_states = {
                        "Proximity_Sensor1": False,
                        "Proximity_Sensor2": False,
                        "Proximity_Sensor3": False,
                        "Proximity_Sensor4": False
                    }
                
                # Get current PLC states (your existing code)
                try:
                    current_states = {
                        "Start": plc_vars_state.get("Start", False),
                        "Stop": plc_vars_state.get("Stop", False),
                        "Conveyor_1": plc_vars_state.get("Conveyor_1", False),
                        "Conveyor_2": plc_vars_state.get("Conveyor_2", False),
                        "Arm_1": plc_vars_state.get("Arm_1", False),
                        "Arm_2": plc_vars_state.get("Arm_2", False),
                        "Arm_3": plc_vars_state.get("Arm_3", False),
                    }
                except NameError:
                    # plc_vars_state not yet defined (initialization order)
                    current_states = {
                        "Start": False,
                        "Stop": False,
                        "Conveyor_1": False,
                        "Conveyor_2": False,
                        "Arm_1": False,
                        "Arm_2": False,
                        "Arm_3": False,
                    }
                
                #  NEW: Process prox sensor updates
                if "error" not in current_prox_states:
                    self.process_prox_sensor_updates(current_prox_states)
                
                # Check for state changes and update steps (your existing code)
                self._update_assembly_steps_for_webpage(current_states)
                
                # Update last states
                self.last_states = current_states.copy()
                
                # Wait before next check
                time.sleep(0.1)  #  FASTER: 100ms for responsive prox detection
                
            except Exception as e:
                print(f"Step monitoring error: {e}")
                time.sleep(2)  # Wait longer on error

    def process_prox_sensor_updates(self, current_prox_states):
        """Process prox sensor changes and update step tracking"""
        
        for sensor_name, current_state in current_prox_states.items():
            if sensor_name.startswith("Proximity_Sensor") and sensor_name in self.prox_last_states:
                last_state = self.prox_last_states[sensor_name]
                
                # Detect rising edge (sensor just triggered)
                if current_state and not last_state:
                    print(f"📍 {sensor_name} TRIGGERED!")
                    self.handle_prox_trigger(sensor_name)
                
                #  NEW: Detect falling edge (sensor released)
                elif not current_state and last_state:
                    print(f"📍 {sensor_name} RELEASED!")
                    self.handle_prox_release(sensor_name)
    
            # Update last states
        for sensor_name in self.prox_last_states:
            if sensor_name in current_prox_states:
                self.prox_last_states[sensor_name] = current_prox_states[sensor_name]

    #  ADD THIS NEW METHOD TO YOUR EXISTING CLASS  
    def handle_prox_trigger(self, sensor_name):
        """Handle individual prox sensor triggers"""
        
        if sensor_name not in self.prox_step_mappings:
            return
            
        mapping = self.prox_step_mappings[sensor_name]
        trigger_step = mapping["trigger_step"]
        affected_arms = mapping["affected_arms"]
        action = mapping["action"]
        
        print(f"🎯 Processing {sensor_name}: {trigger_step}")
        
        # Update step tracking for affected arms
        for arm_num in affected_arms:
            self.set_step_by_name(arm_num, trigger_step, 100)
        
        # Execute associated action
        self.execute_prox_action(action)

    #  ADD THIS NEW METHOD TO YOUR EXISTING CLASS
    def execute_prox_action(self, action):
        """Execute actions triggered by prox sensors with step completion"""
        
        try:
            if action == "start_conveyor_1":
                result = _update_plc_bool_variable(4, 0, True, "Conveyor_1")
                print(f"🔄 Conveyor 1 started: {result}")
                
            elif action == "stop_conveyor_1_start_arm_2":
                # Stop conveyor 1
                result1 = _update_plc_bool_variable(4, 0, False, "Conveyor_1") 
                print(f"⏹️ Conveyor 1 stopped: {result1}")
                
                #  RESET ARM 1 TO IDLE AFTER CONVEYOR STOPS
                print(f"🏁 Holder delivered - resetting Arm 1 to IDLE")
                self.set_step_by_name(1, "IDLE", 0)
                
                # Trigger Arm 2
                result2 = _update_plc_bool_variable(10, 1, True, "Arm_2")
                print(f"🤖 Arm 2 triggered: {result2}")
                
            elif action == "start_conveyor_2":
                result = _update_plc_bool_variable(4, 1, True, "Conveyor_2")
                print(f"🔄 Conveyor 2 started: {result}")
                
            elif action == "stop_conveyor_2_increment_counter":
                # Stop conveyor 2
                result1 = _update_plc_bool_variable(4, 1, False, "Conveyor_2")
                print(f"⏹️ Conveyor 2 stopped: {result1}")
                
                #  RESET ARM 2 TO IDLE AFTER FINAL DELIVERY
                print(f"🏁 Production complete - resetting Arm 2 to IDLE")
                self.set_step_by_name(2, "IDLE", 0)
                
                # Increment production counter
                current_count = plc_vars_state.get("Total_Prod", 0)
                new_count = current_count + 1
                result2 = _update_plc_int_variable(2, new_count, "Total_Prod")
                print(f"📊 Production: {current_count} → {new_count}")
                
        except Exception as e:
            print(f" Error executing prox action {action}: {e}")
    
    def handle_prox_release(self, sensor_name):
        """Handle prox sensor release - reset relevant arms to IDLE"""
        
        if sensor_name == "Proximity_Sensor2":
            # When holder leaves Point B, reset Arm 1
            print(f" Holder left Point B - resetting Arm 1 to IDLE")
            self.set_step_by_name(1, "IDLE", 0)
        
        elif sensor_name == "Proximity_Sensor4":
            # When product leaves final position, reset Arm 2
            print(f" Product delivered - resetting Arm 2 to IDLE") 
            self.set_step_by_name(2, "IDLE", 0)

        #  ADD THIS NEW METHOD TO YOUR EXISTING CLASS
    def get_prox_synchronized_status(self):
        """Get current status including prox sensor states"""
        regular_status = self.get_current_status()
            
        # Add prox sensor information
        try:
            prox_states = read_prox_sensors_from_plc()
        except (NameError, Exception):
            prox_states = {
                "Proximity_Sensor1": False,
                "Proximity_Sensor2": False,
                "Proximity_Sensor3": False,
                "Proximity_Sensor4": False
            }
            
        return {
            **regular_status,
            "prox_sensors": prox_states,
            "prox_last_states": self.prox_last_states,
            "plant_workflow_active": any(prox_states.get(f"Proximity_Sensor{i}", False) for i in [1,2,3,4])
        }

    def _update_assembly_steps_for_webpage(self, current_states):
        """Update assembly steps based on PLC state changes - FOR WEBPAGE ONLY"""
        
        #  ARM 1: Simple step tracking for webpage
        if current_states["Arm_1"] and not self.last_states["Arm_1"]:
            print("🌐 Webpage: Arm 1 started")
            # Only update if robot_controller.py hasn't set a more specific step
            if self.current_steps.get(1, {}).get("name", "IDLE") == "IDLE":
                self.set_step(1, 6, 50)  # "TO_CONVEYOR"
        elif not current_states["Arm_1"] and self.last_states["Arm_1"]:
            print("🌐 Webpage: Arm 1 stopped")
            # Only reset to IDLE if no detailed step is active
            current_step = self.current_steps.get(1, {}).get("name", "IDLE")
            if current_step not in ["RETURNING", "RELEASING"]:  # Don't override detailed steps
                self.set_step(1, 0, 0)   # "IDLE"
        
        #  ARM 2: Simple step tracking for webpage  
        if current_states["Arm_2"] and not self.last_states["Arm_2"]:
            print("🌐 Webpage: Arm 2 started")
            if self.current_steps.get(2, {}).get("name", "IDLE") == "IDLE":
                self.set_step(2, 7, 50)  # "ASSEMBLING"
        elif not current_states["Arm_2"] and self.last_states["Arm_2"]:
            print("🌐 Webpage: Arm 2 stopped")
            current_step = self.current_steps.get(2, {}).get("name", "IDLE")
            if current_step not in ["RETURNING", "RELEASING"]:
                self.set_step(2, 0, 0)   # "IDLE"
        
        #  ARM 3: Simple step tracking for webpage
        if current_states["Arm_3"] and not self.last_states["Arm_3"]:
            print("🌐 Webpage: Arm 3 started")
            if self.current_steps.get(3, {}).get("name", "IDLE") == "IDLE":
                self.set_step(3, 5, 50)  # "ASSEMBLING"
        elif not current_states["Arm_3"] and self.last_states["Arm_3"]:
            print("🌐 Webpage: Arm 3 stopped")
            current_step = self.current_steps.get(3, {}).get("name", "IDLE")
            if current_step not in ["STORING", "TO_STORAGE"]:
                self.set_step(3, 0, 0)   # "IDLE"
        
        #  CONVEYOR TRACKING (for completeness)
        if current_states["Conveyor_1"] and not self.last_states["Conveyor_1"]:
            print("🌐 Webpage: Conveyor 1 started")
        
        if current_states["Conveyor_2"] and not self.last_states["Conveyor_2"]:
            print("🌐 Webpage: Conveyor 2 started")
    
    def _save_to_file(self):
        """Save current state to file"""
        try:
            data = {
                "current_steps": self.current_steps,
                "step_history": self.step_history[-50:],
                "last_updated": datetime.now().isoformat()
            }
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving step data: {e}")
    
    def _load_from_file(self):
        """Load current state from file - PREVENT RECURSION"""
        if self._loading:  # ⭐ PREVENT RECURSION
            return False
            
        self._loading = True
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    self.current_steps = data.get("current_steps", self.current_steps)
                    self.step_history = data.get("step_history", [])
                    return True
        except Exception as e:
            print(f"Error loading step data: {e}")
        finally:
            self._loading = False
        return False
    
    def set_step(self, arm_number, step_number, progress=None):
        """Update current step for an arm - CAN BE CALLED BY robot_controller.py"""
        if arm_number not in [1, 2, 3]:
            return
        
        step_info = self.step_definitions[arm_number].get(step_number, {"name": "UNKNOWN", "estimated_duration": 0})
        step_name = step_info["name"]
        estimated_duration = step_info["estimated_duration"]
        
        now = datetime.now()
        estimated_end = now.timestamp() + estimated_duration if estimated_duration > 0 else None
        
        # Calculate progress if not provided
        if progress is None:
            if step_number == 0:
                progress = 0  # IDLE = 0% (not active)
            else:
                progress = 50  # Default progress for active steps
        
        # Update current status
        self.current_steps[arm_number] = {
            "name": step_name,
            "step_number": step_number,
            "progress": progress,
            "start_time": now.isoformat(),
            "estimated_end": estimated_end
        }
        
        # Add to history
        self.step_history.append({
            "timestamp": now.isoformat(),
            "arm": arm_number,
            "step_number": step_number,
            "step_name": step_name,
            "progress": progress
        })
        
        # Keep only last 100 history entries
        if len(self.step_history) > 100:
            self.step_history = self.step_history[-100:]
        
        # Save to file
        self._save_to_file()
        
        # Print for debugging
        print(f"📊 Sequence {arm_number}: {step_name} ({progress}%)")
    
    def set_step_by_name(self, arm_number, step_name, progress=50):
        """Set step by name - FOR robot_controller.py"""
        # Find step number by name
        for step_num, step_info in self.step_definitions[arm_number].items():
            if step_info["name"] == step_name:
                self.set_step(arm_number, step_num, progress)
                return
        
        # If not found, create custom step
        print(f" Custom step: {step_name} for arm {arm_number}")
        now = datetime.now()
        self.current_steps[arm_number] = {
            "name": step_name,
            "step_number": 99,  # Custom step
            "progress": progress,
            "start_time": now.isoformat(),
            "estimated_end": None
        }
        
        self.step_history.append({
            "timestamp": now.isoformat(),
            "arm": arm_number,
            "step_number": 99,
            "step_name": step_name,
            "progress": progress
        })
        
        self._save_to_file()
        print(f"📊 Sequence {arm_number}: {step_name} ({progress}%)")
    
    def get_current_status(self):
        """Get current status for all sequences"""
        return {
            "arms": {
                f"arm_{i}": self.current_steps.get(i, {"name": "IDLE", "progress": 0, "start_time": None}) 
                for i in [1, 2, 3]
            },
            "last_updated": datetime.now().isoformat()
        }
    
    def get_step_history(self, limit=50):
        """Get recent step history - NO RECURSIVE LOADING"""
        # ⭐ DON'T RELOAD FROM FILE - USE CURRENT DATA
        return {
            "history": self.step_history[-limit:] if self.step_history else [],
            "total_steps_today": len([s for s in self.step_history 
                                    if s.get("timestamp", "").startswith(datetime.now().strftime("%Y-%m-%d"))]),
            "summary": self._get_summary()
        }
    
    def _get_summary(self):
        """Generate summary statistics"""
        if not self.step_history:
            return {"average_step_time": 0, "most_common_step": "None"}
        
        active_sequences = len([seq for seq in [1, 2, 3] if self.current_steps.get(seq, {}).get("name", "IDLE") != "IDLE"])
        
        return {
            "total_steps_executed": len(self.step_history),
            "active_sequences": active_sequences,
            "assembly_cycles_today": len([s for s in self.step_history if "COMPLETE" in s.get("step_name", "")]),
            "last_activity": self.step_history[-1]["timestamp"] if self.step_history else None
        }

# ⭐ ADD THIS ENTIRE CLASS AFTER StepTracker CLASS
class IntelligentEnergyCalculator:
    def __init__(self):
        # 🔥 YOUR CORRECT UTILIZATION PATTERNS
        self.baseline_patterns = {
            'assembly_line_standard': {
                'description': 'Your actual 3-arm assembly line with conveyors',
                'cycle_breakdown_percent': {
                    'arm_1': 18,  # 18% of cycle time active (component picking)
                    'arm_2': 32,  # 32% of cycle time active (main assembly) 
                    'arm_3': 10,  # 10% of cycle time active (PCB delivery)
                    'conveyor_1': 8,  # 8% of cycle time active (part transport)
                    'conveyor_2': 8   # 8% of cycle time active (final transport)
                },
                'speed_factors': {
                    30: 0.9,   # At 30% speed, components work 90% as long
                    40: 0.85,  # At 40% speed, components work 85% as long
                    50: 0.8,   # At 50% speed, components work 80% as long
                    60: 1.0,   # Baseline at 60%
                    70: 1.15,  # Higher speed = longer active times
                    80: 1.3,
                    90: 1.45,
                    100: 1.6
                },
                'source': 'your_actual_system_measurements'
            }
        }
        
        # Historical learning data
        self.learned_patterns = {}
        self.cycle_analyzer = CycleEnergyAnalyzer()
        self.confidence_level = 'baseline'
    
    def get_intelligent_cycle_breakdown(self):
        """Get cycle breakdown using best available data"""
        
        # 1. Try to use learned patterns (highest confidence)
        if self.has_sufficient_learned_data():
            return self.get_learned_pattern()
        
        # 2. Try to use partial real data + intelligent extrapolation
        elif self.has_partial_data():
            return self.get_hybrid_pattern()
        
        # 3. Fall back to intelligent baseline (still useful!)
        else:
            return self.get_intelligent_baseline()
    
    def get_intelligent_baseline(self):
        """Get realistic baseline based on your actual system"""
        robot_speed = get_robot_speed_from_file()
        
        # Calculate realistic cycle time based on speed
        production_metrics = calculate_production_metrics()
        actual_cycle_time = production_metrics['measured_cycle_time']
        
        # Get baseline pattern
        pattern = self.baseline_patterns['assembly_line_standard']
        speed_factor = pattern['speed_factors'].get(robot_speed, 1.0)
        
        breakdown = {}
        for component, percent in pattern['cycle_breakdown_percent'].items():
            # Calculate realistic active time
            base_active_time = (percent / 100) * actual_cycle_time
            adjusted_active_time = base_active_time * speed_factor
            
            # Ensure active time doesn't exceed cycle time
            adjusted_active_time = min(adjusted_active_time, actual_cycle_time * 0.9)
            
            breakdown[component] = {
                'active_time': adjusted_active_time,
                'idle_time': actual_cycle_time - adjusted_active_time,
                'utilization_percent': (adjusted_active_time / actual_cycle_time) * 100,
                'confidence': 'baseline_estimate'
            }
        
        return {
            'cycle_time_seconds': actual_cycle_time,
            'component_breakdown': breakdown,
            'data_source': 'intelligent_baseline_your_utilization',
            'confidence_level': 'baseline',
            'robot_speed': robot_speed,
            'notes': 'Based on your actual system utilization patterns'
        }
    
    def get_learned_pattern(self):
        """Use patterns learned from actual production"""
        avg_breakdown = self.cycle_analyzer.get_average_cycle_breakdown()
        if avg_breakdown:
            avg_breakdown['confidence_level'] = 'high'
            avg_breakdown['data_source'] = 'learned_from_production'
            return avg_breakdown
        else:
            return self.get_intelligent_baseline()
    
    def get_hybrid_pattern(self):
        """Combine partial real data with intelligent estimates"""
        partial_data = self.cycle_analyzer.get_average_cycle_breakdown()
        baseline = self.get_intelligent_baseline()
        
        if not partial_data:
            return baseline
        
        # Blend real data with baseline where data is insufficient
        hybrid_breakdown = {}
        
        for component in baseline['component_breakdown']:
            if (component in partial_data['component_breakdown'] and
                partial_data['component_breakdown'][component]['utilization_percent'] > 2):
                # Use real data if meaningful
                hybrid_breakdown[component] = partial_data['component_breakdown'][component]
                hybrid_breakdown[component]['confidence'] = 'real_data'
            else:
                # Use intelligent baseline
                hybrid_breakdown[component] = baseline['component_breakdown'][component]
                hybrid_breakdown[component]['confidence'] = 'baseline_estimate'
        
        return {
            'cycle_time_seconds': partial_data.get('cycle_time_seconds', baseline['cycle_time_seconds']),
            'component_breakdown': hybrid_breakdown,
            'data_source': 'hybrid_real_and_baseline',
            'confidence_level': 'medium',
            'notes': 'Combination of real measurements and your system patterns'
        }
    
    def has_sufficient_learned_data(self):
        """Check if we have enough real data to be confident"""
        return (len(self.cycle_analyzer.completed_cycles) >= 5 and
                all(cycle['duration_seconds'] > 10 for cycle in self.cycle_analyzer.completed_cycles))
    
    def has_partial_data(self):
        """Check if we have some real data to work with"""
        return len(self.cycle_analyzer.completed_cycles) >= 1
    
    def calculate_realistic_energy_per_cycle(self):
        """Calculate energy using best available intelligence"""
        
        cycle_data = self.get_intelligent_cycle_breakdown()
        robot_speed = get_robot_speed_from_file()
        
        # Power consumption (actual hardware specs)
        arm_power_active = 250 if robot_speed > 50 else 150
        arm_power_idle = 5
        conveyor_power_active = 50
        conveyor_power_idle = 2
        
        total_cycle_energy = 0
        detailed_breakdown = {}
        
        for component, timing in cycle_data['component_breakdown'].items():
            active_time = timing['active_time']
            idle_time = timing['idle_time']
            
            if 'arm' in component:
                power_active = arm_power_active
                power_idle = arm_power_idle
            else:
                power_active = conveyor_power_active
                power_idle = conveyor_power_idle
            
            active_energy = power_active * active_time
            idle_energy = power_idle * idle_time
            component_energy = active_energy + idle_energy
            
            detailed_breakdown[component] = {
                'active_time_s': active_time,
                'idle_time_s': idle_time,
                'utilization_percent': timing['utilization_percent'],
                'confidence': timing['confidence'],
                'active_energy_ws': active_energy,
                'idle_energy_ws': idle_energy,
                'total_energy_ws': component_energy,
                'energy_kwh': component_energy / (1000 * 3600)
            }
            
            total_cycle_energy += component_energy
        
        return {
            'cycle_time_seconds': cycle_data['cycle_time_seconds'],
            'total_energy_kwh': total_cycle_energy / (1000 * 3600),
            'breakdown': detailed_breakdown,
            'data_source': cycle_data['data_source'],
            'confidence_level': cycle_data['confidence_level'],
            'robot_speed': robot_speed,
            'notes': cycle_data.get('notes', '')
        }

# ⭐ ADD THIS CLASS TOO
class CycleEnergyAnalyzer:
    def __init__(self):
        self.cycle_start_time = None
        self.current_cycle_data = {}
        self.component_timings = {
            'arm_1': {'start_time': None, 'total_active_time': 0},
            'arm_2': {'start_time': None, 'total_active_time': 0},
            'arm_3': {'start_time': None, 'total_active_time': 0},
            'conveyor_1': {'start_time': None, 'total_active_time': 0},
            'conveyor_2': {'start_time': None, 'total_active_time': 0}
        }
        self.completed_cycles = []
        self.last_plc_state = {}
    
    def analyze_plc_cycle(self, plc_data):
        """Extract actual operating times from PLC data"""
        now = datetime.now()
        
        # Detect cycle start (when Total_Prod increases)
        current_prod = plc_data.get('Total_Prod', 0)
        last_prod = self.last_plc_state.get('Total_Prod', 0)
        
        if current_prod > last_prod:
            # New cycle started
            if self.cycle_start_time is not None:
                # Complete previous cycle
                self.complete_cycle(now)
            
            self.start_new_cycle(now)
        
        # Track component activity during cycle
        if self.cycle_start_time is not None:
            self.track_component_activity(plc_data, now)
        
        self.last_plc_state = plc_data.copy()
    
    def start_new_cycle(self, timestamp):
        """Start tracking a new production cycle"""
        self.cycle_start_time = timestamp
        self.current_cycle_data = {
            'start_time': timestamp,
            'component_states': {},
            'step_changes': []
        }
        
        # Reset component timings for new cycle
        for component in self.component_timings:
            self.component_timings[component] = {
                'start_time': None, 
                'total_active_time': 0,
                'state_changes': []
            }
    
    def track_component_activity(self, plc_data, timestamp):
        """Track when each component is active during the cycle"""
        
        # Extract current component states from PLC
        current_states = {
            'arm_1': plc_data.get('Arm_1', False),
            'arm_2': plc_data.get('Arm_2', False),
            'arm_3': plc_data.get('Arm_3', False),
            'conveyor_1': plc_data.get('Conveyor_1', False),
            'conveyor_2': plc_data.get('Conveyor_2', False)
        }
        
        # Track state changes and accumulate active time
        for component, is_active in current_states.items():
            self.update_component_timing(component, is_active, timestamp)
    
    def update_component_timing(self, component, is_active, timestamp):
        """Update timing for a specific component"""
        timing = self.component_timings[component]
        
        # Check for state change
        if component not in self.current_cycle_data['component_states']:
            # First time seeing this component in this cycle
            self.current_cycle_data['component_states'][component] = is_active
            if is_active:
                timing['start_time'] = timestamp
            return
        
        previous_state = self.current_cycle_data['component_states'][component]
        
        if previous_state != is_active:
            # State changed
            if previous_state and timing['start_time'] is not None:
                # Component was active, now inactive - add to total time
                active_duration = (timestamp - timing['start_time']).total_seconds()
                timing['total_active_time'] += active_duration
                
                timing['state_changes'].append({
                    'start': timing['start_time'],
                    'end': timestamp,
                    'duration': active_duration
                })
            
            # Update state
            self.current_cycle_data['component_states'][component] = is_active
            if is_active:
                timing['start_time'] = timestamp
            else:
                timing['start_time'] = None
    
    def complete_cycle(self, end_time):
        """Complete the current cycle and calculate final timings"""
        if self.cycle_start_time is None:
            return
        
        cycle_duration = (end_time - self.cycle_start_time).total_seconds()
        
        # Finalize any active components
        for component, timing in self.component_timings.items():
            if timing['start_time'] is not None:
                # Component was still active at cycle end
                final_duration = (end_time - timing['start_time']).total_seconds()
                timing['total_active_time'] += final_duration
        
        # Store completed cycle data
        cycle_summary = {
            'start_time': self.cycle_start_time,
            'end_time': end_time,
            'duration_seconds': cycle_duration,
            'component_timings': {
                comp: {
                    'active_time_seconds': timing['total_active_time'],
                    'idle_time_seconds': cycle_duration - timing['total_active_time'],
                    'utilization_percent': (timing['total_active_time'] / cycle_duration) * 100
                }
                for comp, timing in self.component_timings.items()
            }
        }
        
        self.completed_cycles.append(cycle_summary)
        
        # Keep only last 10 cycles for analysis
        if len(self.completed_cycles) > 10:
            self.completed_cycles.pop(0)
    
    def get_average_cycle_breakdown(self):
        """Get average component timings from recent cycles"""
        if not self.completed_cycles:
            return None
        
        # Calculate averages from last 5 cycles
        recent_cycles = self.completed_cycles[-5:]
        
        avg_breakdown = {}
        avg_cycle_time = sum(cycle['duration_seconds'] for cycle in recent_cycles) / len(recent_cycles)
        
        for component in ['arm_1', 'arm_2', 'arm_3', 'conveyor_1', 'conveyor_2']:
            active_times = [cycle['component_timings'][component]['active_time_seconds'] 
                          for cycle in recent_cycles]
            avg_active_time = sum(active_times) / len(active_times)
            
            avg_breakdown[component] = {
                'active_time': avg_active_time,
                'idle_time': avg_cycle_time - avg_active_time,
                'utilization_percent': (avg_active_time / avg_cycle_time) * 100
            }
        
        return {
            'cycle_time_seconds': avg_cycle_time,
            'component_breakdown': avg_breakdown,
            'based_on_cycles': len(recent_cycles),
            'data_source': 'real_plc_analysis'
        }

# ⭐ CREATE GLOBAL STEP TRACKER INSTANCE
step_tracker = StepTracker()
intelligent_calculator = IntelligentEnergyCalculator()  
cycle_analyzer = CycleEnergyAnalyzer()                  

# PLC configuration
plc_client = snap7.client.Client()
PLC_IP = settings.PLC_IP
PLC_RACK = settings.PLC_RACK
PLC_SLOT = settings.PLC_SLOT
PLC_DB_NUMBER = 1

# ============================================
# TIA INTELLIGENCE INITIALIZATION
# ============================================
tia = None
tia_data_available = False

if TIA_INTEGRATION_AVAILABLE:
    try:
        print("🔄 Initializing TIA Intelligence module...")
        tia = get_tia_intelligence()
        if tia and tia.connected:
            tia_data_available = True
            print("✅ TIA Intelligence connected - enhanced features available")
        else:
            print("⚠️ TIA Intelligence initialized but not connected to TIA Portal")
            print("   Running in Snap7-only mode (PLC control still available)")
    except Exception as e:
        print(f"⚠️ TIA Intelligence initialization failed: {e}")
        print("   Running in Snap7-only mode (PLC control still available)")
else:
    print("ℹ️ TIA Integration not available - running in Snap7-only mode")

# ============================================
# MODULAR AI CONTEXTS - Smart Context System (prompts.py)
# ============================================
from .prompts import (
    CORE_CONTEXT,
    COST_CONTEXT,
    ENERGY_CONTEXT,
    PRODUCTION_CONTEXT,
    QUESTION_TYPE_CONTEXT,
    SCENARIO_CONTEXT,
    SPEED_CONTEXT,
    STATUS_CONTEXT,
)

# ⭐ ONLY ACTUAL DB1 VARIABLES
plc_vars_state = {
    "Start": False,
    "Stop": False,
    "Conveyor_1": False,
    "Conveyor_2": False,
    "Total_Prod": 0,
    "Arm_1": False,
    "Arm_2": False,
    "Arm_3": False,
}

starting_keywords = ["start", "enable", "true", "on", "turn on", "activate"]
stopping_keywords = ["stop", "disable", "false", "off", "turn off", "deactivate"]

# Azure OpenAI chat-completions URL and key come from the environment / .env file
API_KEY = settings.AZURE_OPENAI_API_KEY
ENDPOINT = settings.AZURE_OPENAI_ENDPOINT

app = FastAPI(title="TIA S7 Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# SMART CONTEXT SELECTION SYSTEM
# ============================================

def select_contexts_for_question(user_question: str) -> str:
    """
    Intelligently select only relevant context modules based on question analysis.
    This dramatically reduces token usage while improving answer quality.
    
    Returns: Combined context string with only relevant modules
    """
    
    question_lower = user_question.lower()
    
    # Start with core context (always included)
    selected_contexts = [CORE_CONTEXT]
    context_names = ["CORE"]  # For debugging
    
    # ============================================
    # ENERGY/POWER QUESTIONS
    # ============================================
    energy_keywords = [
        "energy", "power", "consumption", "kwh", "watt", "w",
        "per part", "electricity", "how much energy"
    ]
    
    if any(keyword in question_lower for keyword in energy_keywords):
        selected_contexts.append(ENERGY_CONTEXT)
        context_names.append("ENERGY")
    
    # ============================================
    # COST QUESTIONS
    # ============================================
    cost_keywords = [
        "cost", "€", "euro", "price", "expensive", "cheap",
        "how much", "total cost", "daily cost", "hourly cost"
    ]
    
    if any(keyword in question_lower for keyword in cost_keywords):
        selected_contexts.append(COST_CONTEXT)
        context_names.append("COST")
    
    # ============================================
    # SPEED QUESTIONS
    # ============================================
    speed_keywords = [
        "speed", "faster", "slower", "cycle time", "timing",
        "how fast", "increase", "decrease", "boost", "reduce",
        "set speed", "change speed", "adjust speed"
    ]
    
    if any(keyword in question_lower for keyword in speed_keywords):
        selected_contexts.append(SPEED_CONTEXT)
        context_names.append("SPEED")
    
    # ============================================
    # PRODUCTION QUESTIONS
    # ============================================
    production_keywords = [
        "parts per", "throughput", "production", "output",
        "daily", "hourly", "shift", "capacity", "produce",
        "manufacturing", "parts", "units"
    ]
    
    if any(keyword in question_lower for keyword in production_keywords):
        selected_contexts.append(PRODUCTION_CONTEXT)
        context_names.append("PRODUCTION")
    
    # ============================================
    # SCENARIO/WHAT-IF QUESTIONS
    # ============================================
    scenario_keywords = [
        "what if", "if all", "if only", "suppose", "imagine",
        "scenario", "hypothetical", "assuming", "let's say",
        "would it", "could it"
    ]
    
    if any(keyword in question_lower for keyword in scenario_keywords):
        selected_contexts.append(SCENARIO_CONTEXT)
        context_names.append("SCENARIO")
    
    # ============================================
    # STATUS QUESTIONS
    # ============================================
    status_keywords = [
        "status", "current", "now", "what's running",
        "system state", "overview", "summary", "show me",
        "what is", "are running", "active"
    ]
    
    if any(keyword in question_lower for keyword in status_keywords):
        selected_contexts.append(STATUS_CONTEXT)
        context_names.append("STATUS")
    
    # ============================================
    # SPECIAL CASE: "RUN FOR X HOURS" QUESTIONS
    # ============================================
    run_for_keywords = ["run for", "operate for", "work for", "running for"]
    
    if any(phrase in question_lower for phrase in run_for_keywords):
        # These need both energy AND production context
        if ENERGY_CONTEXT not in selected_contexts:
            selected_contexts.append(ENERGY_CONTEXT)
            context_names.append("ENERGY")
        if PRODUCTION_CONTEXT not in selected_contexts:
            selected_contexts.append(PRODUCTION_CONTEXT)
            context_names.append("PRODUCTION")
        if COST_CONTEXT not in selected_contexts:
            selected_contexts.append(COST_CONTEXT)
            context_names.append("COST")
    
    # ============================================
    # ALWAYS ADD QUESTION TYPE DETECTION
    # ============================================
    selected_contexts.append(QUESTION_TYPE_CONTEXT)
    context_names.append("QUESTION_TYPE")
    
    # Combine all selected contexts
    combined_context = "\n\n".join(selected_contexts)
    
    # Debug output
    print(f"📊 Context Selection: {', '.join(context_names)}")
    print(f"📏 Context size: {len(combined_context)} chars (vs ~15000 in old static approach)")
    
    return combined_context


# ============================================
# TIA OPENNESS CONTEXT GENERATION
# ============================================

def generate_tia_static_context():
    """
    Generate comprehensive static PLC context from TIA Openness.
    Includes: Tags, Blocks, Hardware, IP, Network, Alarms, HMI, Structure, Health
    """
    
    if not tia_data_available or not tia or not tia.connected:
        return """
⚠️ TIA PORTAL DATA NOT AVAILABLE
- TIA Portal integration is not active
- System is running in Snap7-only mode (PLC control still works)
- Tag/Block information from TIA Portal is unavailable
"""
    
    try:
        print("🔄 Generating comprehensive TIA static context from TIA Portal...")
        
        # Get ALL data from TIA Portal
        project_info = tia.get_project_info()
        tags_info = tia.get_all_tags()
        blocks_info = tia.get_all_blocks()
        hardware_info = tia.get_hardware_configuration()
        ip_info = tia.get_plc_ip_address()
        network_info = tia.get_network_configuration()
        alarm_info = tia.get_alarm_configuration()
        hmi_info = tia.get_hmi_configuration()
        structure_info = tia.get_program_structure()
        health_info = tia.analyze_program_health()
        tech_objects = tia.get_technology_objects()
        watch_tables = tia.get_watch_tables()
        
        context_parts = []
        
        # HEADER
        context_parts.extend([
            "=" * 80,
            "📋 TIA PORTAL PROJECT DATA (Live from TIA Openness)",
            "=" * 80,
            "",
            "⚠️ CRITICAL AI INSTRUCTION:",
            "When user asks about 'tags', 'blocks', 'hardware', 'I/O modules', 'IP address', or 'project':",
            "- USE THE DATA BELOW FROM TIA PORTAL (not generic information)",
            "- List the ACTUAL tags/blocks/hardware shown below",
            "- Reference REAL addresses, IPs, and types from this data",
            "- DO NOT say 'I cannot access' - the data is RIGHT HERE",
            "- DO NOT invent any data not explicitly listed below",
            "",
            "=" * 80,
            ""
        ])
        
        # PROJECT INFO
        if "error" not in project_info:
            context_parts.extend([
                "🏭 PROJECT INFORMATION:",
                f"Project Name: {project_info.get('project_name', 'N/A')}",
                f"Project Path: {project_info.get('project_path', 'N/A')}",
                f"PLC Name: {project_info.get('plc_name', 'N/A')}",
                f"TIA Version: {project_info.get('tia_version', 'N/A')}",
                f"Connection Status: {project_info.get('connection_status', 'N/A')}",
                ""
            ])
        
        # IP ADDRESS (Prominent section)
        if "error" not in ip_info:
            context_parts.extend([
                "🌐 PLC IP ADDRESS:",
                "=" * 80
            ])
            
            if ip_info.get('primary_ip'):
                context_parts.append(f"✅ Primary PLC IP: {ip_info['primary_ip']}")
            else:
                context_parts.append("⚠️ Primary IP: Not configured")
            
            if ip_info.get('all_ips'):
                context_parts.append("")
                context_parts.append("All Network Interfaces:")
                for device_name, ip_address in ip_info['all_ips'].items():
                    context_parts.append(f"  - {device_name}: {ip_address}")
            
            context_parts.extend(["", "=" * 80, ""])
        
        # HARDWARE CONFIG (Complete detail)
        if "error" not in hardware_info and hardware_info.get('total_devices', 0) > 0:
            context_parts.extend([
                "🔌 HARDWARE CONFIGURATION:",
                "=" * 80,
                f"Total Devices: {hardware_info['total_devices']}",
                ""
            ])
            
            for device in hardware_info.get('devices', []):
                context_parts.append(f"**Device: {device['name']}** ({device.get('type', 'Unknown')})")
                context_parts.append(f"  Total Components: {len(device.get('items', []))}")
                context_parts.append("")
                
                if device.get('items'):
                    context_parts.append("  Hardware Components:")
                    
                    def format_item(item, indent="    "):
                        """Recursively format hardware items and sub-items"""
                        lines = []
                        lines.append(f"{indent}✓ {item['name']}")
                        lines.append(f"{indent}  Type: {item.get('type', 'Unknown')}")
                        lines.append(f"{indent}  Class: {item.get('classification', 'Unknown')}")
                        
                        if item.get('position') and item['position'] != 'Unknown':
                            lines.append(f"{indent}  Position: {item['position']}")
                        
                        if item.get('ip_addresses'):
                            for ip in item['ip_addresses']:
                                lines.append(f"{indent}  🌐 IP: {ip}")
                        
                        # Recursively format sub-items
                        if item.get('sub_items'):
                            lines.append(f"{indent}  ⮑ Sub-components ({len(item['sub_items'])}):")
                            for sub_item in item['sub_items']:
                                sub_lines = format_item(sub_item, indent + "    ")
                                lines.extend(sub_lines)
                        
                        return lines
                    
                    # Format all top-level items
                    for item in device['items']:
                        item_lines = format_item(item)
                        context_parts.extend(item_lines)
                        context_parts.append("")
                
            context_parts.extend(["=" * 80, ""])
        
        # TAGS (ALL tags with full details)
        if "error" not in tags_info and tags_info.get('count', 0) > 0:
            context_parts.extend([
                "🏷️ COMPLETE PLC TAG LIST:",
                "=" * 80,
                f"📦 Total Tags: {tags_info['count']}",
                "",
                "⚠️ WHEN USER ASKS FOR TAGS: Show tags from this list (not generic names)",
                ""
            ])
            
            for i, tag in enumerate(tags_info['tags'], 1):
                address = tag.get('address', 'N/A')
                data_type = tag.get('data_type', 'N/A')
                comment = tag.get('comment', '')
                table = tag.get('table', 'N/A')
                
                if comment:
                    context_parts.append(f"{i}. {tag['name']} ({data_type}) @ {address} - {comment}")
                else:
                    context_parts.append(f"{i}. {tag['name']} ({data_type}) @ {address}")
            
            context_parts.extend(["", "=" * 80, ""])
        
        # BLOCKS (ALL blocks with full details)
        if "error" not in blocks_info and blocks_info.get('count', 0) > 0:
            context_parts.extend([
                "🔧 COMPLETE PLC BLOCK LIST:",
                "=" * 80,
                f"📦 Total Blocks: {blocks_info['count']}",
                "",
                "⚠️ WHEN USER ASKS FOR BLOCKS: Show blocks from this list (not invented names)",
                ""
            ])
            
            for i, block in enumerate(blocks_info['blocks'], 1):
                block_type = block.get('type', 'Unknown')
                block_number = block.get('number', 'N/A')
                block_lang = block.get('language', 'N/A')
                block_comment = block.get('comment', '')
                
                if block_comment:
                    context_parts.append(f"{i}. {block['name']} (Type: {block_type}, #: {block_number}, Lang: {block_lang}) - {block_comment}")
                else:
                    context_parts.append(f"{i}. {block['name']} (Type: {block_type}, #: {block_number}, Lang: {block_lang})")
            
            context_parts.extend(["", "=" * 80, ""])
        
        # PROGRAM STRUCTURE
        if "error" not in structure_info:
            summary = structure_info.get('summary', {})
            context_parts.extend([
                "🏗️ PROGRAM STRUCTURE:",
                "=" * 80,
                f"Total Blocks: {summary.get('total_blocks', 0)}",
                f"Organization Blocks (OB): {summary.get('organization_blocks', 0)}",
                f"Function Blocks (FB): {summary.get('function_blocks', 0)}",
                f"Functions (FC): {summary.get('functions', 0)}",
                f"Data Blocks (DB): {summary.get('data_blocks', 0)}",
                "",
                "=" * 80,
                ""
            ])
        
        # PROGRAM HEALTH
        if "error" not in health_info:
            metrics = health_info.get('metrics', {})
            context_parts.extend([
                "💊 PROGRAM HEALTH & DOCUMENTATION:",
                "=" * 80,
                f"Overall Status: {health_info.get('overall_status', 'unknown')}",
                f"Total Tags: {metrics.get('total_tags', 0)}",
                f"Documented Tags: {metrics.get('documented_tags', 0)}",
                f"Undocumented Tags: {metrics.get('undocumented_tags', 0)}",
                f"Documented Blocks: {metrics.get('documented_blocks', 0)}",
                f"Undocumented Blocks: {metrics.get('undocumented_blocks', 0)}",
                ""
            ])
            
            if health_info.get('issues'):
                context_parts.append("Issues:")
                for issue in health_info['issues']:
                    context_parts.append(f"  • {issue}")
                context_parts.append("")
            
            context_parts.extend(["=" * 80, ""])
        
        # NETWORK CONFIGURATION
        if "error" not in network_info and network_info.get('interfaces'):
            context_parts.extend([
                "🌐 NETWORK CONFIGURATION:",
                "=" * 80
            ])
            for interface in network_info['interfaces']:
                context_parts.append(f"Interface: {interface.get('name', 'N/A')}")
                context_parts.append(f"  Type: {interface.get('type', 'N/A')}")
                context_parts.append(f"  IP: {interface.get('ip_address', 'N/A')}")
                context_parts.append(f"  Subnet: {interface.get('subnet_mask', 'N/A')}")
                context_parts.append("")
            context_parts.extend(["=" * 80, ""])
        
        # ALARMS
        if "error" not in alarm_info and alarm_info.get('total_alarms', 0) > 0:
            context_parts.extend([
                f"🚨 ALARM CONFIGURATION: {alarm_info['total_alarms']} alarms configured",
                "=" * 80,
                ""
            ])
        
        # HMI
        if "error" not in hmi_info and hmi_info.get('total_screens', 0) > 0:
            context_parts.extend([
                f"🖥️ HMI CONFIGURATION: {hmi_info['total_screens']} screens found",
                "=" * 80,
                ""
            ])
        
        # TECHNOLOGY OBJECTS
        if "error" not in tech_objects and tech_objects.get('total_objects', 0) > 0:
            context_parts.extend([
                f"⚙️ TECHNOLOGY OBJECTS: {tech_objects['total_objects']} objects",
                "=" * 80,
                ""
            ])
        
        # WATCH TABLES
        if "error" not in watch_tables and watch_tables.get('total_tables', 0) > 0:
            context_parts.extend([
                f"👁️ WATCH TABLES: {watch_tables['total_tables']} tables",
                "=" * 80,
                ""
            ])
        
        # FOOTER
        context_parts.extend([
            "",
            "=" * 80,
            "🚨 CRITICAL VALIDATION RULES:",
            "=" * 80,
            "",
            "1. This data is extracted DIRECTLY from TIA Portal via TIA Openness API",
            "2. Use ONLY the blocks, tags, hardware, and IPs listed above",
            "3. DO NOT invent blocks like 'ConveyorControl', 'ArmControl', etc.",
            "4. DO NOT assume anything exists that isn't explicitly listed",
            "5. If asked about something not listed above → say 'Not found in project'",
            "",
            "When user asks about tags/blocks/hardware/IP:",
            "  ✅ Show ONLY the data from this section",
            "  ✅ Use EXACT names, addresses, and IPs as shown",
            "  ❌ DO NOT add anything not listed above",
            "  ❌ DO NOT invent generic names",
            "",
            "=" * 80
        ])
        
        final_context = "\n".join(context_parts)
        
        print(f"✅ TIA context generated: {len(final_context)} chars")
        print(f"   📦 Blocks: {blocks_info.get('count', 0)}")
        print(f"   🏷️ Tags: {tags_info.get('count', 0)}")
        print(f"   🔌 Hardware Devices: {hardware_info.get('total_devices', 0)}")
        print(f"   🌐 Primary IP: {ip_info.get('primary_ip', 'N/A')}")
        
        return final_context
        
    except Exception as e:
        print(f"❌ Error generating TIA context: {e}")
        import traceback
        traceback.print_exc()
        
        return f"""
⚠️ TIA CONTEXT GENERATION ERROR
- Error: {str(e)}
- TIA Portal data is temporarily unavailable
- DO NOT invent any blocks, tags, or hardware
"""
        
        context_parts.extend([
            "=" * 80,
            ""
        ])
        
        final_context = "\n".join(context_parts)
        print(f"✅ Generated TIA context ({len(final_context)} chars)")
        return final_context
        
    except Exception as e:
        print(f"❌ Error generating TIA context: {e}")
        return f"⚠️ TIA Portal data extraction failed: {str(e)}\n"


def build_dynamic_context(user_question: str, plc_status_string: str) -> str:
    """
    Build complete AI context with:
    1. Smart context selection (only relevant modules)
    2. Real-time system data
    3. TIA Portal data (if available and relevant)
    
    This is the main function that replaces your old STATIC_CONTEXT approach.
    """
    
    # Get question-specific contexts (smart selection)
    static_context = select_contexts_for_question(user_question)
    
    # ⭐ CHECK IF QUESTION NEEDS TIA DATA
    tia_keywords = [
        'tag', 'tags', 'block', 'blocks', 'tia', 'portal', 'project',
        'hardware', 'i/o', 'module', 'modules', 'cpu', 'network',
        'ip address', 'configuration', 'program', 'plc program',
        'organization block', 'function block', 'data block',
        'ob', 'fb', 'fc', 'db', 'variable', 'variables', 'plc'
    ]
    
    question_lower = user_question.lower()
    needs_tia_data = any(keyword in question_lower for keyword in tia_keywords)
    
    print(f"🔍 Question: '{user_question}'")
    print(f"🔍 TIA Data Available: {tia_data_available}")
    print(f"🔍 Needs TIA Data: {needs_tia_data}")
    
    # ⭐ ADD TIA PORTAL CONTEXT IF AVAILABLE AND NEEDED
    tia_context = ""
    if tia_data_available and needs_tia_data:
        try:
            print(f"✅ Generating TIA context for AI...")
            tia_context = generate_tia_static_context()
            print(f"✅ TIA context generated: {len(tia_context)} chars")
        except Exception as e:
            print(f"⚠️ Error generating TIA context: {e}")
            tia_context = "⚠️ TIA Portal data temporarily unavailable\n"
    elif needs_tia_data and not tia_data_available:
        print(f"⚠️ TIA data needed but not available")
        tia_context = "\n⚠️ TIA Portal is not connected. Please open TIA Portal and restart the application for detailed project information.\n"
    else:
        print(f"ℹ️ TIA data not needed for this question")
    
    # Get real-time data
    try:
        production_metrics = calculate_production_metrics()
        energy_status = get_energy_status()
        robot_speed = get_robot_speed_from_file()
        step_status = step_tracker.get_current_status()
    except Exception as e:
        print(f"⚠️ Error getting real-time data: {e}")
        # Fallback to minimal data
        production_metrics = {"current_speed_percentage": 50, "measured_cycle_time": 80, "parts_per_hour": 22, "parts_per_8_hour_shift": 176}
        energy_status = {"total_power": 350}
        robot_speed = 50
        step_status = {"arms": {"arm_1": {"name": "UNKNOWN", "progress": 0}}}
    
    # Build dynamic real-time context
    dynamic_context = f"""
CURRENT SYSTEM STATE:
{plc_status_string}

REAL-TIME PRODUCTION METRICS (Speed: {production_metrics['current_speed_percentage']}%):
- Current Cycle Time: {production_metrics['measured_cycle_time']:.1f} seconds
- Parts per Hour: {production_metrics['parts_per_hour']:.0f} parts
- Daily Output (8hrs): {production_metrics['parts_per_8_hour_shift']:.0f} parts
- Robot Speed: {robot_speed}% ({'HIGH SPEED' if robot_speed > 50 else 'LOW SPEED'} mode)
- Data Source: {production_metrics.get('data_source', 'calculated')}

CURRENT POWER STATUS:
- Total System Power: {energy_status.get('total_power', 0)}W
"""
    
    # Add detailed arm status if available
    if 'robots' in energy_status:
        for i in [1, 2, 3]:
            arm_key = f'arm_{i}'
            arm_data = energy_status['robots'].get(arm_key, {})
            dynamic_context += f"- Arm {i}: {arm_data.get('power', 5)}W ({arm_data.get('status', 'UNKNOWN')})\n"
    
    # Add conveyor status if available
    if 'conveyors' in energy_status:
        for i in [1, 2]:
            conv_key = f'conveyor_{i}'
            conv_data = energy_status['conveyors'].get(conv_key, {})
            dynamic_context += f"- Conveyor {i}: {conv_data.get('power', 2)}W ({conv_data.get('status', 'UNKNOWN')})\n"
    
    # Add current step tracking
    dynamic_context += f"""
CURRENT ASSEMBLY STEPS:
- Arm 1: {step_status['arms']['arm_1']['name']} ({step_status['arms']['arm_1']['progress']}%)
- Arm 2: {step_status['arms']['arm_2']['name']} ({step_status['arms']['arm_2']['progress']}%)
- Arm 3: {step_status['arms']['arm_3']['name']} ({step_status['arms']['arm_3']['progress']}%)

IMPORTANT: Use the above REAL-TIME data for all calculations and responses.
"""
    
    # ⭐ DETECT SPEED SIMULATION QUESTIONS AND INJECT CALCULATIONS
    speed_simulation_keywords = [
        "what happens", "what would happen", "if i increase", "if i decrease",
        "if i change", "if i set", "if speed", "simulate", "impact of",
        "what if", "suppose", "assuming"
    ]
    
    has_speed_simulation = any(keyword in question_lower for keyword in speed_simulation_keywords)
    has_percentage = re.search(r'(\d+)\s*%', user_question)
    
    if has_speed_simulation and has_percentage:
        print(f"🎯 Detected speed simulation question - calculating detailed impact...")
        
        try:
            # Extract target speed or change amount
            current_speed = get_robot_speed_from_file()
            percentage_match = re.search(r'(\d+)\s*%', user_question)
            percentage = int(percentage_match.group(1))
            
            # Determine if it's relative or absolute
            if any(word in question_lower for word in ["increase by", "decrease by", "reduce by", "boost by", "up by", "down by"]):
                # Relative change
                if any(word in question_lower for word in ["decrease", "reduce", "lower", "down", "slow"]):
                    target_speed = max(1, current_speed - percentage)
                else:
                    target_speed = min(100, current_speed + percentage)
            elif any(word in question_lower for word in ["set to", "change to", "speed to", "make it"]):
                # Absolute speed
                target_speed = min(100, max(1, percentage))
            else:
                # Assume it's an increase by default
                target_speed = min(100, current_speed + percentage)
            
            # Calculate power for both speeds
            current_tier = 'high_speed' if current_speed > 50 else 'low_speed'
            new_tier = 'high_speed' if target_speed > 50 else 'low_speed'
            
            # Assume 3 arms active, 2 conveyors active (typical production scenario)
            current_arm_power = 250 if current_speed > 50 else 150
            new_arm_power = 250 if target_speed > 50 else 150
            
            current_total_power = (current_arm_power * 3) + (50 * 2) + (5 * 0) + (2 * 0)  # 3 arms + 2 conveyors
            new_total_power = (new_arm_power * 3) + (50 * 2) + (5 * 0) + (2 * 0)
            
            power_difference = new_total_power - current_total_power
            power_percentage_change = (power_difference / current_total_power) * 100 if current_total_power > 0 else 0
            
            # Calculate costs (€0.15 per kWh)
            electricity_rate = 0.15
            current_cost_per_hour = (current_total_power / 1000) * electricity_rate
            new_cost_per_hour = (new_total_power / 1000) * electricity_rate
            cost_difference = new_cost_per_hour - current_cost_per_hour
            daily_cost_difference = cost_difference * 8
            
            # Interpolate cycle times
            speed_data_points = [(30, 100), (50, 80), (75, 70), (100, 66)]
            current_cycle_time = interpolate_cycle_time(current_speed, speed_data_points)
            new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
            
            cycle_time_saved = current_cycle_time - new_cycle_time
            current_parts_per_hour = 3600 / current_cycle_time
            new_parts_per_hour = 3600 / new_cycle_time
            parts_increase = new_parts_per_hour - current_parts_per_hour
            
            # Calculate per-part costs
            current_energy_per_cycle_kwh = (current_total_power * current_cycle_time) / 3_600_000
            new_energy_per_cycle_kwh = (new_total_power * new_cycle_time) / 3_600_000
            
            current_cost_per_part = current_energy_per_cycle_kwh * electricity_rate
            new_cost_per_part = new_energy_per_cycle_kwh * electricity_rate
            cost_per_part_difference = new_cost_per_part - current_cost_per_part
            
            # Inject calculated data into context
            dynamic_context += f"""

⚡ SPEED CHANGE IMPACT ANALYSIS (CALCULATED):
================================================================================
USER QUESTION DETECTED: Speed simulation from {current_speed}% to {target_speed}%

📊 CALCULATED METRICS (USE THESE EXACT VALUES IN YOUR RESPONSE):

**Current State (at {current_speed}%):**
- Cycle Time: {current_cycle_time:.1f} seconds
- Production Rate: {current_parts_per_hour:.0f} parts/hour ({current_parts_per_hour * 8:.0f} parts/day)
- Total Power: {current_total_power}W ({current_tier.replace('_', ' ').title()} mode)
- Hourly Energy Cost: €{current_cost_per_hour:.4f}
- Cost per Part: €{current_cost_per_part:.6f}

**New State (at {target_speed}%):**
- Cycle Time: {new_cycle_time:.1f} seconds
- Production Rate: {new_parts_per_hour:.0f} parts/hour ({new_parts_per_hour * 8:.0f} parts/day)
- Total Power: {new_total_power}W ({new_tier.replace('_', ' ').title()} mode)
- Hourly Energy Cost: €{new_cost_per_hour:.4f}
- Cost per Part: €{new_cost_per_part:.6f}

**Impact Summary:**
- Cycle Time Change: {cycle_time_saved:+.1f} seconds ({(cycle_time_saved/current_cycle_time)*100:+.1f}%)
- Production Increase: {parts_increase:+.0f} parts/hour ({parts_increase*8:+.0f} parts/day)
- Power Change: {power_difference:+}W ({power_percentage_change:+.1f}%)
- Hourly Cost Change: €{cost_difference:+.4f} ({(cost_difference/current_cost_per_hour)*100:+.1f}%)
- Daily Cost Change (8hrs): €{daily_cost_difference:+.2f}
- Cost per Part Change: €{cost_per_part_difference:+.6f} ({(cost_per_part_difference/current_cost_per_part)*100:+.1f}%)
- Power Tier Change: {'YES - Arms switch from LOW to HIGH SPEED mode!' if current_tier != new_tier else 'NO - Remains in same power mode'}

⚠️ CRITICAL INSTRUCTION:
Use the EXACT calculated values above in your response. DO NOT estimate or approximate.
Format your response with these specific numbers to show:
1. Current vs New Energy Cost (per hour)
2. Cost Difference (+ or -)
3. Daily Cost Impact (8-hour shift)
4. Cost per Part comparison
5. Power consumption changes
6. Production rate improvements
================================================================================
"""
            
            print(f"✅ Speed simulation data injected: {current_speed}% → {target_speed}%")
            print(f"   Power: {current_total_power}W → {new_total_power}W")
            print(f"   Cost: €{current_cost_per_hour:.4f}/hr → €{new_cost_per_hour:.4f}/hr")
            
        except Exception as e:
            print(f"⚠️ Error calculating speed simulation: {e}")
            import traceback
            traceback.print_exc()
    
    # Combine static (selected) and dynamic contexts
    full_context = f"{static_context}\n\n{tia_context}\n\n{dynamic_context}"
    
    # ⭐ DEBUG: Show what was included
    print(f"📊 Context Assembly Complete:")
    print(f"   - Static context: {len(static_context)} chars")
    print(f"   - TIA context: {len(tia_context)} chars {'✅ INCLUDED' if len(tia_context) > 100 else '⚠️ EMPTY/SMALL'}")
    print(f"   - Dynamic context: {len(dynamic_context)} chars")
    print(f"   - Total context: {len(full_context)} chars")
    
    return full_context

# -------- PLC Connection Helpers --------

def connect_plc():
    """Attempt PLC connection - non-blocking startup"""
    if not plc_client.get_connected():
        try:
            print(f"🔌 Connecting to PLC at {PLC_IP} (Rack:{PLC_RACK}, Slot:{PLC_SLOT})...")
            plc_client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
            print("✅ Successfully connected to PLC")
            return True
        except Exception as e:
            print(f"⚠️ PLC Connection Failed: {e}")
            print("⚠️ Application will start in LIMITED MODE (no PLC control)")
            print("\n💡 PLCSIM Advanced Troubleshooting:")
            print("   1. Ensure PLCSIM Advanced is running")
            print("   2. Virtual PLC must be in RUN mode (green)")
            print("   3. Check IP address in PLCSIM: " + PLC_IP)
            print("   4. Try Slot 0 if Slot 1 fails (set PLC_SLOT in .env)")
            print("   5. Firewall rule added for port 102\n")
            return False

@app.on_event("startup")
async def startup_event():
    # Try PLC connection but don't crash if it fails
    plc_connected = connect_plc()
    
    if not plc_connected:
        print("⚠️ Running without PLC - voice control and AI features still available")
    
    # ⭐ AUTO-START VOICE ASSISTANT
    if VOICE_ASSISTANT_AVAILABLE:
        print("🎤 Starting voice assistant in background...")
        try:
            start_voice_assistant()
            print("✅ Voice assistant active! Say 'copilot' to give commands.")
        except Exception as e:
            print(f"⚠️ Could not start voice assistant: {e}")

@app.on_event("shutdown")
def shutdown_event():
    if plc_client.get_connected():
        plc_client.disconnect()
        print("Disconnected from PLC.")
    
    # ⭐ CLEANUP TIA INTELLIGENCE
    if TIA_INTEGRATION_AVAILABLE and tia:
        try:
            close_tia_intelligence()
            print("✅ TIA Intelligence disconnected")
        except Exception as e:
            print(f"⚠️ Error closing TIA Intelligence: {e}")

def _update_plc_int_variable(byte_offset, new_value, var_name):
    global plc_vars_state
    if not plc_client.get_connected():
        plc_vars_state[var_name] = new_value
        return f"PLC Offline. Simulated update: {var_name} to {new_value}."
    try:
        buffer = bytearray(2) # S7 INT
        snap7.util.set_int(buffer, 0, new_value)
        plc_client.db_write(PLC_DB_NUMBER, byte_offset, buffer)
        plc_vars_state[var_name] = new_value
        return f"{var_name} updated to {new_value}."
    except Exception as e:
        return f"Failed to update {var_name} on PLC: {e}"

# ⭐ ADD THIS MISSING FUNCTION:
def _update_plc_bool_variable(byte_offset, bit_offset, new_value, var_name):
    global plc_vars_state
    if not plc_client.get_connected():
        plc_vars_state[var_name] = new_value
        return f"PLC Offline. Simulated update: {var_name} to {new_value}."
    try:
        reading = plc_client.db_read(PLC_DB_NUMBER, byte_offset, 1)
        snap7.util.set_bool(reading, 0, bit_offset, new_value)
        plc_client.db_write(PLC_DB_NUMBER, byte_offset, reading)
        plc_vars_state[var_name] = new_value
        return f"{var_name} updated to {new_value}."
    except Exception as e:
        return f"Failed to update {var_name} on PLC: {e}"


def update_stop(val):
    """Update stop bit and automatically handle start bit + disable robot system"""
    global plc_vars_state
    
    if val:  # Activating stop
        print("🛑 Activating STOP - clearing START bit and disabling robot system...")
        
        # Disable robot system first
        disable_robot_system()
        
        # Set stop bit to True
        stop_result = _update_plc_bool_variable(0, 1, True, "Stop")
        
        # Clear start bit to False
        start_result = _update_plc_bool_variable(0, 0, False, "Start")
        
        return f"🛑 EMERGENCY STOP ACTIVATED!\n- {stop_result}\n- Start bit cleared: {start_result}\n- Robot system disabled"
        
    else:  # Deactivating stop
        print("✅ Deactivating STOP...")
        return _update_plc_bool_variable(0, 1, False, "Stop")

def update_start(val):
    """Update start bit and automatically handle stop bit + enable robot system"""
    global plc_vars_state
    
    if val:  # Activating start
        print("🚀 Activating START - clearing STOP bit and enabling robot system...")
        
        # Clear stop bit first
        stop_result = _update_plc_bool_variable(0, 1, False, "Stop")
        
        # Set start bit to True
        start_result = _update_plc_bool_variable(0, 0, True, "Start")
        
        # ⭐ Enable robot system via robot_controller.py API
        robot_enable_result = enable_robot_system()
        
        return f"🚀 START ACTIVATED!\n- Stop bit cleared: {stop_result}\n- {start_result}\n- {robot_enable_result}"
        
    else:  # Deactivating start
        print("⏹️ Deactivating START and disabling robot system...")
        
        # Disable robot system
        disable_robot_system()
        
        return _update_plc_bool_variable(0, 0, False, "Start")


def enable_robot_system():
    """Enable robot system via robot_controller.py API"""
    try:
        response = requests.post("http://localhost:8001/api/enable_system", timeout=2)
        if response.status_code == 200:
            print("✅ Robot system enabled via API")
            return "✅ Robot system enabled - ready to respond to commands"
        else:
            return f"⚠️ Robot system enable returned status {response.status_code}"
    except Exception as e:
        print(f"⚠️ Failed to enable robot system: {e}")
        return f"⚠️ Robot system enable failed: {str(e)}"


def disable_robot_system():
    """Disable robot system via robot_controller.py API"""
    try:
        response = requests.post("http://localhost:8001/api/disable_system", timeout=2)
        if response.status_code == 200:
            print("✅ Robot system disabled via API")
        else:
            print(f"⚠️ Robot system disable returned status {response.status_code}")
    except Exception as e:
        print(f"⚠️ Failed to disable robot system: {e}")

def update_reset():
    """Reset command - sets Stop bit ON, clears Start bit, AND moves robots to starting positions"""
    print("🔄 RESET command - setting Stop ON, clearing Start, and resetting robot positions...")
    
    # 1. Set Stop bit to TRUE (ON) and clear Start bit
    stop_result = _update_plc_bool_variable(0, 1, True, "Stop")  # Changed to True
    start_result = _update_plc_bool_variable(0, 0, False, "Start")
    
    # 2. Send reset command to robot system via API
    robot_reset_result = send_robot_reset_command()
    
    return f"🔄 SYSTEM RESET COMPLETE!\n- {stop_result} (Set to ON)\n- {start_result}\n- {robot_reset_result}"

def send_robot_reset_command():
    """Send reset command to robot_controller.py via API"""
    try:
        # Method 1: Via API call to robot system
        response = requests.post(
            "http://localhost:8001/api/reset_robots",  # Robot system API endpoint
            json={
                "command": "RESET_TO_ORIGIN",
                "reset_all_arms": True,
                "clear_emergency_stop": True,
                "source": "main.py_reset_command"
            },
            timeout=10  # 10 second timeout for robot reset
        )
        
        if response.status_code == 200:
            result = response.json()
            return f"✅ {result.get('message', 'Robot arms reset to starting positions')}"
        else:
            return f"⚠️ Robot reset failed: HTTP {response.status_code} - {response.text}"
            
    except requests.exceptions.ConnectionError:
        # Fallback: Create command file if API is not available
        return send_robot_reset_command_fallback()
    except requests.exceptions.Timeout:
        return "⚠️ Robot reset timeout - operation may still be in progress"
    except Exception as e:
        return f"❌ Robot reset failed: {e}"

def send_robot_reset_command_fallback():
    """Fallback method: Create reset command file"""
    try:
        reset_command = {
            'command': 'RESET_TO_ORIGIN',
            'reset_all_arms': True,
            'clear_emergency_stop': True,
            'timestamp': time.time(),
            'source': 'main.py_reset_fallback'
        }
        
        with open(settings.data_path("robot_reset_command.json"), 'w') as f:
            json.dump(reset_command, f, indent=2)
            
        return "✅ Robot reset command sent (fallback method)"
        
    except Exception as e:
        return f"❌ All robot reset methods failed: {e}"

def extract_number_internal(s):
    match = re.search(r"[-+]?[0-9]*\.?[0-9]+", s)
    return float(match.group()) if match else None

def get_plc_status_string():
    # ✅ GET SPEED FROM FILE, NOT PLC
    current_speed = get_robot_speed_from_file()
    
    return (f"Start: {plc_vars_state['Start']}, Stop: {plc_vars_state['Stop']}, "
            f"Conveyor_1: {plc_vars_state['Conveyor_1']}, Conveyor_2: {plc_vars_state['Conveyor_2']}, "
            f"Total_Prod: {plc_vars_state['Total_Prod']}, Robot_Speed: {current_speed}, "
            f"Arms: [{plc_vars_state['Arm_1']}, {plc_vars_state['Arm_2']}, {plc_vars_state['Arm_3']}]")

def refresh_arm_state_from_plc():
    """Refresh just arm states - calls the full refresh now"""
    refresh_all_states_from_plc()

def refresh_all_states_from_plc():
    """Updates plc_vars_state with ONLY actual DB1 values + robot status for arms."""
    if not plc_client.get_connected():
        plc_vars_state.update({
            "Start": False,
            "Stop": False,
            "Conveyor_1": False,
            "Conveyor_2": False,
            "Total_Prod": 0,
            "Arm_1": False,
            "Arm_2": False,
            "Arm_3": False,
            # ⭐ ADD PROX SENSORS TO OFFLINE STATE
            "Proximity_Sensor1": False,
            "Proximity_Sensor2": False,
            "Proximity_Sensor3": False,
            "Proximity_Sensor4": False,
        })
        return
    
    try:
        # Read PLC data for non-arm variables (your existing code)
        data = plc_client.db_read(PLC_DB_NUMBER, 0, 8)
        plc_vars_state["Start"]       = snap7.util.get_bool(data, 0, 0)
        plc_vars_state["Stop"]        = snap7.util.get_bool(data, 0, 1)
        plc_vars_state["Total_Prod"]  = snap7.util.get_int(data, 2)
        plc_vars_state["Conveyor_1"]  = snap7.util.get_bool(data, 4, 0)
        plc_vars_state["Conveyor_2"]  = snap7.util.get_bool(data, 4, 1)
        
        # ⭐ ADD PROX SENSOR READING
        try:
            prox_states = read_prox_sensors_from_plc()
        except (NameError, Exception) as e:
            prox_states = {
                "Proximity_Sensor1": False,
                "Proximity_Sensor2": False,
                "Proximity_Sensor3": False,
                "Proximity_Sensor4": False
            }
        if "error" not in prox_states:
            plc_vars_state.update(prox_states)
        
        # ⭐ USE THE SAME is_moving LOGIC AS ENERGY STATUS (your existing code)
        try:
            robot_status_data = get_robot_running_status()
            robot_arms = robot_status_data.get('robots', {})
            
            # Extract is_moving for each arm (SAME AS ENERGY STATUS)
            for arm_num in [1, 2, 3]:
                arm_key = f'arm_{arm_num}'
                arm_data = robot_arms.get(arm_key, {})
                is_moving = arm_data.get('is_moving', False)  # ← SAME VARIABLE
                
                plc_vars_state[f"Arm_{arm_num}"] = is_moving  # ← MAP TO PLC STATUS
            
        except Exception as e:
            print(f"❌ Robot status error, using PLC fallback: {e}")
            # Fallback to PLC arm data (your existing code)
            try:
                arm_data = plc_client.db_read(PLC_DB_NUMBER, 10, 2)
                plc_vars_state["Arm_1"] = snap7.util.get_bool(arm_data, 0, 0)
                plc_vars_state["Arm_2"] = snap7.util.get_bool(arm_data, 0, 1)
                plc_vars_state["Arm_3"] = snap7.util.get_bool(arm_data, 0, 2)
            except:
                plc_vars_state["Arm_1"] = False
                plc_vars_state["Arm_2"] = False
                plc_vars_state["Arm_3"] = False

    except Exception as e:
        print(f"PLC refresh error: {e}")
        plc_vars_state.update({
            "Start": False,
            "Stop": False,
            "Conveyor_1": False,
            "Conveyor_2": False,
            "Total_Prod": 0,
            "Arm_1": False,
            "Arm_2": False,
            "Arm_3": False,
        })

# ⭐ STATUS REPORT GENERATION
def generate_status_report(plc_vars):
    """Generate status report - can be enhanced with AI"""
    # Calculate current total power consumption
    POWER_CONSUMPTION = {
        'robot_idle': 5,
        'robot_low_speed': 150,    # ← NEW: Low speed
        'robot_high_speed': 250,   # ← NEW: High speed
        'conveyor_idle': 2,
        'conveyor_running': 50
    }
    robot_speed = get_robot_speed_from_file()
    total_power = 0
    for i in [1, 2, 3]:
        if plc_vars.get(f'Arm_{i}', False):
            if robot_speed <= 50:  # ← NEW: 50% threshold
                total_power += POWER_CONSUMPTION['robot_low_speed']
            else:
                total_power += POWER_CONSUMPTION['robot_high_speed']
        else:
            total_power += POWER_CONSUMPTION['robot_idle']
    for i in [1, 2]:
        if plc_vars.get(f'Conveyor_{i}', False):
            total_power += POWER_CONSUMPTION['conveyor_running']
        else:
            total_power += POWER_CONSUMPTION['conveyor_idle']

    # Build status report
    active_arms = [f"Arm {i}" for i in range(1, 4) if plc_vars.get(f'Arm_{i}', False)]
    inactive_arms = [f"Arm {i}" for i in range(1, 4) if not plc_vars.get(f'Arm_{i}', False)]
    active_conveyors = [f"Conveyor {i}" for i in [1,2] if plc_vars.get(f'Conveyor_{i}', False)]
    inactive_conveyors = [f"Conveyor {i}" for i in [1,2] if not plc_vars.get(f'Conveyor_{i}', False)]

    reply = [
        f"System Status:",
        f"- Current Power Consumption: {total_power}W",
        f"- Control: Start={'ON' if plc_vars['Start'] else 'OFF'}, Stop={'ON' if plc_vars['Stop'] else 'OFF'}",
        f"- Active Arms: {', '.join(active_arms) if active_arms else 'None'}",
        f"- Inactive Arms: {', '.join(inactive_arms) if inactive_arms else 'None'}",
        f"- Active Conveyors: {', '.join(active_conveyors) if active_conveyors else 'None'}",
        f"- Inactive Conveyors: {', '.join(inactive_conveyors) if inactive_conveyors else 'None'}",
        f"- Robot Speed: {get_robot_speed_from_file()}%",
        f"- Total Production Count: {plc_vars['Total_Prod']}"
    ]
    return "\n".join(reply)

async def generate_status_report_ai(plc_vars):
    """AI-powered status report generation"""
    
    system_data = {
        "plc_states": plc_vars,
        "energy_consumption": get_current_energy_consumption_sync(),
        "step_tracking": step_tracker.get_current_status(),
        "system_utilization_raw": {
            "active_arms": sum(1 for i in [1,2,3] if plc_vars.get(f'Arm_{i}', False)),
            "active_conveyors": sum(1 for i in [1,2] if plc_vars.get(f'Conveyor_{i}', False)),
            "robot_speed": get_robot_speed_from_file(),
        },
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Generate a comprehensive system status report for this manufacturing system.

CURRENT SYSTEM DATA:
{json.dumps(system_data, indent=2)}

REQUIREMENTS:
1. Calculate total power consumption based on current states
2. Analyze system utilization and efficiency
3. Provide detailed status of all components
4. Include operational insights and recommendations

RESPONSE FORMAT:
Provide a structured status report with:
- System Status overview
- Current Power Consumption (calculated)
- Control states (Start/Stop)
- Active/Inactive Arms and Conveyors
- Robot Speed and Production Count
- System efficiency analysis
- Operational recommendations

Calculate all values dynamically based on the provided data.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI status report: {e}"

# ⭐ POWER CALCULATION FUNCTIONS
def calculate_power_for_custom_state(arm_states, conveyor_states):
    """Calculate power for custom state - enhanced with AI logic"""
    POWER_CONSUMPTION = {
        'robot_idle': 5,
        'robot_low_speed': 150,    # ← NEW: Low speed (≤50%)
        'robot_high_speed': 250,   # ← NEW: High speed (>50%)
        'conveyor_idle': 2,
        'conveyor_running': 50
    }
    total = 0
    details = []
    
    robot_speed = get_robot_speed_from_file()
    
    for num in [1, 2, 3]:
        if arm_states.get(num):
            if robot_speed <= 50:  
                power = POWER_CONSUMPTION['robot_low_speed']
                details.append(f"Arm {num}: {power}W (low speed mode)")  # ← NEW TEXT
            else:
                power = POWER_CONSUMPTION['robot_high_speed']
                details.append(f"Arm {num}: {power}W (high speed mode)")  # ← NEW TEXT
            total += power
        else:
            total += POWER_CONSUMPTION['robot_idle']
            details.append(f"Arm {num}: 5W (idle)")
    
    for num in [1, 2]:
        if conveyor_states.get(num):
            total += POWER_CONSUMPTION['conveyor_running']
            details.append(f"Conveyor {num}: 50W (running)")
        else:
            total += POWER_CONSUMPTION['conveyor_idle']
            details.append(f"Conveyor {num}: 2W (idle)")
    
    return total, details

async def calculate_power_for_custom_state_ai(arm_states, conveyor_states):
    """AI-powered power calculation for scenarios"""
    
    scenario_data = {
        "arm_states": arm_states,
        "conveyor_states": conveyor_states,
        "current_robot_speed": get_robot_speed_from_file(),
        "power_specifications": {
            "robot_idle": "5W",
            "robot_low_speed": "150W (speed <= 50%)",      # ← UPDATED
            "robot_high_speed": "250W (speed > 50%)",      # ← UPDATED
            "conveyor_idle": "2W",
            "conveyor_running": "50W"
        },
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Calculate power consumption for this specific scenario configuration.

SCENARIO DATA:
{json.dumps(scenario_data, indent=2)}

REQUIREMENTS:
1. Calculate total power consumption based on arm and conveyor states
2. Consider robot speed for power calculations (50% threshold: ≤50% = low speed, >50% = high speed)  # ← UPDATED
3. Provide detailed breakdown for each component
4. Include efficiency analysis

RESPONSE FORMAT:
Return a JSON object with:
{{
    "total_power": <number>,
    "details": [
        "Arm 1: <power>W (<status>)",
        "Arm 2: <power>W (<status>)",
        "Arm 3: <power>W (<status>)",
        "Conveyor 1: <power>W (<status>)",
        "Conveyor 2: <power>W (<status>)"
    ],
    "analysis": "<efficiency and optimization insights>"
}}

Power calculation rules:  # ← ADDED CLARITY
- Robot arms: 5W (idle), 150W (low speed ≤50%), 250W (high speed >50%)
- Conveyors: 2W (idle), 50W (running)
- Current robot speed: {scenario_data['current_robot_speed']}%

Calculate all values based on the power specifications provided.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        # Parse AI response to extract power and details
        # For now, fallback to regular calculation
        return calculate_power_for_custom_state(arm_states, conveyor_states)
    except Exception as e:
        return calculate_power_for_custom_state(arm_states, conveyor_states)

# ⭐ SCENARIO GENERATION
def scenario_generator(constraints=None):
    """
    Generates all possible ON/OFF scenarios for Arm 1, Arm 2, Arm 3, Conveyor 1, Conveyor 2.
    Optionally, accepts a function `constraints(scenario)` to filter scenarios.
    Yields: Each scenario as a dict: {'Arm_1': True/False, ..., 'Conveyor_2': True/False}
    """
    keys = ['Arm_1', 'Arm_2', 'Arm_3', 'Conveyor_1', 'Conveyor_2']
    for state in product([False, True], repeat=len(keys)):
        scenario = dict(zip(keys, state))
        if constraints is None or constraints(scenario):
            yield scenario

def extract_custom_power_intent(command_lower):
    import re
    
    # 1. Recognize scenario/what-if intent
    trigger = (
        any(k in command_lower for k in [
            "what if", "if only", "if ", "scenario", "simulate", "suppose", "imagine",
            "estimate", "calculate", "assume", "just", "only", "keep", "set", "active",
            "if all", "when all", "all arms", "all robots", "then what"
        ])
        or command_lower.strip().startswith("if")
        or ("only" in command_lower and ("energy" in command_lower or "power" in command_lower))
    )
    if not trigger:
        return None, None

    # 2. Extract intended states - Default: everything off
    arm_states = {1: False, 2: False, 3: False}
    conveyor_states = {1: False, 2: False}
    found_custom = False

    # 3. IMPROVED ARM PARSING
    
    # Handle "all arms" patterns first
    if re.search(r"all\s+arms?\s+(are\s+)?(active|on|running|enabled)", command_lower):
        arm_states = {1: True, 2: True, 3: True}
        found_custom = True
        print("DEBUG: All arms ON")
    elif re.search(r"all\s+arms?\s+(are\s+)?(inactive|off|stopped|disabled)", command_lower):
        arm_states = {1: False, 2: False, 3: False}
        found_custom = True
        print("DEBUG: All arms OFF")
    
    # Individual arm parsing - MUCH MORE ROBUST
    for arm_num in [1, 2, 3]:
        # Pattern for "arm X is active/on"
        active_patterns = [
            rf"arm\s+{arm_num}\s+is\s+(active|on|running|enabled)",
            rf"arm\s+{arm_num}\s+(active|on|running|enabled)",
            rf"robot\s+{arm_num}\s+is\s+(active|on|running|enabled)",
            rf"robot\s+{arm_num}\s+(active|on|running|enabled)"
        ]
        
        # Pattern for "arm X is inactive/off"  
        inactive_patterns = [
            rf"arm\s+{arm_num}\s+is\s+(inactive|off|stopped|disabled)",
            rf"arm\s+{arm_num}\s+(inactive|off|stopped|disabled)",
            rf"robot\s+{arm_num}\s+is\s+(inactive|off|stopped|disabled)",
            rf"robot\s+{arm_num}\s+(inactive|off|stopped|disabled)"
        ]
        
        # Check if this arm is mentioned as active
        for pattern in active_patterns:
            if re.search(pattern, command_lower):
                arm_states[arm_num] = True
                found_custom = True
                print(f"DEBUG: Arm {arm_num} set to ACTIVE")
                break
        
        # Check if this arm is mentioned as inactive
        for pattern in inactive_patterns:
            if re.search(pattern, command_lower):
                arm_states[arm_num] = False
                found_custom = True
                print(f"DEBUG: Arm {arm_num} set to INACTIVE")
                break

    # Handle "only arm X" patterns
    only_match = re.search(r"only\s+arm\s+([1-3])", command_lower)
    if only_match:
        active_arm = int(only_match.group(1))
        arm_states = {1: False, 2: False, 3: False}
        arm_states[active_arm] = True
        found_custom = True
        print(f"DEBUG: Only arm {active_arm} active")

    # 4. IMPROVED CONVEYOR PARSING
    
    # Handle "both conveyors" patterns
    if re.search(r"(both\s+)?conveyors?\s+(are\s+)?(both\s+)?(active|on|running|enabled)", command_lower):
        conveyor_states = {1: True, 2: True}
        found_custom = True
        print("DEBUG: Both conveyors ON")
    elif re.search(r"(both\s+)?conveyors?\s+(are\s+)?(both\s+)?(inactive|off|stopped|disabled)", command_lower):
        conveyor_states = {1: False, 2: False}
        found_custom = True
        print("DEBUG: Both conveyors OFF")
    
    # Individual conveyor parsing
    for conv_num in [1, 2]:
        active_patterns = [
            rf"conveyor\s+{conv_num}\s+is\s+(active|on|running|enabled)",
            rf"conveyor\s+{conv_num}\s+(active|on|running|enabled)"
        ]
        
        inactive_patterns = [
            rf"conveyor\s+{conv_num}\s+is\s+(inactive|off|stopped|disabled)",
            rf"conveyor\s+{conv_num}\s+(inactive|off|stopped|disabled)"
        ]
        
        for pattern in active_patterns:
            if re.search(pattern, command_lower):
                conveyor_states[conv_num] = True
                found_custom = True
                print(f"DEBUG: Conveyor {conv_num} set to ACTIVE")
                break
                
        for pattern in inactive_patterns:
            if re.search(pattern, command_lower):
                conveyor_states[conv_num] = False
                found_custom = True
                print(f"DEBUG: Conveyor {conv_num} set to INACTIVE")
                break

    # Handle "only conveyor X" patterns
    if "only conveyor 1" in command_lower:
        conveyor_states = {1: True, 2: False}
        found_custom = True
        print("DEBUG: Only conveyor 1 active")
    elif "only conveyor 2" in command_lower:
        conveyor_states = {1: False, 2: True}
        found_custom = True
        print("DEBUG: Only conveyor 2 active")

    print(f"DEBUG: Final result - Arms: {arm_states}, Conveyors: {conveyor_states}, Found: {found_custom}")
    return (arm_states, conveyor_states) if found_custom else (None, None)

# ⭐ AI COMMUNICATION
def send_gpt_request_internal(text, dynamic_context, session_id=None):
    """
    Send request to GPT with smart context selection + TIA data + conversation history.
    Uses build_dynamic_context() to ensure TIA Portal data is included when relevant.
    
    Args:
        text: User's question/command
        dynamic_context: Real-time system context
        session_id: Optional session ID for conversation history
    """
    headers = {"Content-Type": "application/json", "api-key": API_KEY}
    
    # ⭐ USE build_dynamic_context() which handles TIA injection!
    full_context = build_dynamic_context(text, dynamic_context)
    
    # Check for pending speed change in conversation history
    pending_speed_change = None
    conversation_history = []
    if session_id:
        conversation_history = conversation_manager.get_history(session_id)
        pending_speed_change = extract_pending_speed_from_history(conversation_history)
    
    # Add pending speed context if exists
    if pending_speed_change:
        full_context += f"""

⚠️ IMPORTANT CONVERSATION CONTEXT:
The AI previously suggested changing speed to {pending_speed_change}% and asked for confirmation.

If user says "yes", "ok", "sure", "do it", "go ahead", or similar confirmation:
→ INTENT: SPEED_CHANGE
→ TARGET_SPEED: {pending_speed_change}
→ ACTION: Change robot speed to {pending_speed_change}%
→ REASONING: User confirmed pending speed change from conversation

If user asks a different question, respond to that new question instead.
"""
        print(f"💡 Detected pending speed change: {pending_speed_change}% from conversation history")
    
    # Build messages with conversation history
    messages = [
        {"role": "system", "content": full_context}
    ]
    
    # Add conversation history if session exists
    if session_id and conversation_history:
        messages.extend(conversation_history)
        print(f"💬 Including {len(conversation_history)} previous messages from session: {session_id}")
    
    # Add current user message
    messages.append({"role": "user", "content": text})
    
    payload = {
        "messages": messages,
        "temperature": 0.25,
        "max_tokens": 1000
    }
    
    try:
        print(f"🔄 Sending request to AI API...")
        print(f"📡 Endpoint: {ENDPOINT}")
        print(f"🔑 API key configured: {bool(API_KEY)}")
        print(f"📡 Total messages in payload: {len(messages)}")
        
        response = requests.post(ENDPOINT, headers=headers, json=payload, timeout=30)
        
        print(f"📊 Response Status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()['choices'][0]['message']['content']
            print(f"✅ AI Response received successfully")
            
            # Save to conversation history
            if session_id:
                conversation_manager.add_message(session_id, "user", text)
                conversation_manager.add_message(session_id, "assistant", result)
                print(f"💾 Saved conversation to session: {session_id}")
            
            return result
        else:
            print(f"❌ AI API Error: {response.status_code}")
            print(f"📄 Error Details: {response.text}")
            return f"AI Service Error: {response.status_code} - {response.text}"
            
    except requests.exceptions.Timeout:
        print("⏰ AI API Timeout")
        return "AI Service timeout. Please try again."
    except requests.exceptions.ConnectionError:
        print("🔌 AI API Connection Error")
        return "Cannot connect to AI service. Please check your internet connection."
    except Exception as e:
        print(f"💥 AI API Exception: {e}")
        return f"AI Service Error: {e}"

def format_step_history_for_ai(history):
    """Format step history for AI consumption"""
    if not history:
        return "No recent activity"
    
    formatted = []
    for step in history:
        timestamp = step['timestamp'].split('T')[1][:8]  # Just time part
        formatted.append(f"[{timestamp}] Arm {step['arm']}: {step['step_name']} ({step['progress']}%)")
    
    return "\n".join(formatted)

def get_detailed_energy_context():
    """
    Get detailed energy context for AI.
    Now simplified since we use smart context selection.
    """
    try:
        energy_data = get_energy_status()
        robot_speed = get_robot_speed_from_file()
        
        # Build concise context
        context_lines = [
            f"CURRENT ENERGY STATUS:",
            f"Total System Power: {energy_data.get('total_power', 0)}W",
            f"Robot Speed: {robot_speed}% ({'HIGH SPEED' if robot_speed > 50 else 'LOW SPEED'} mode)",
            f"",
            f"COMPONENT BREAKDOWN:"
        ]
        
        # Add arm details
        for i in [1, 2, 3]:
            arm_data = energy_data.get('robots', {}).get(f'arm_{i}', {})
            power = arm_data.get('power', 5)
            status = arm_data.get('status', 'IDLE')
            running = arm_data.get('running', False)
            
            context_lines.append(f"- Arm {i}: {power}W ({status}) {'[ACTIVE]' if running else '[IDLE]'}")
        
        # Add conveyor details
        for i in [1, 2]:
            conv_data = energy_data.get('conveyors', {}).get(f'conveyor_{i}', {})
            power = conv_data.get('power', 2)
            status = conv_data.get('status', 'IDLE')
            running = conv_data.get('running', False)
            
            context_lines.append(f"- Conveyor {i}: {power}W ({status}) {'[RUNNING]' if running else '[IDLE]'}")
        
        return "\n".join(context_lines)
        
    except Exception as e:
        return f"Energy context error: {e}"

def calculate_dynamic_timers(robot_speed_percentage):
    """Calculate timer values in milliseconds"""
    try:
        speed = max(0, min(100, robot_speed_percentage))
        
        # Calculate in milliseconds (DINT values)
        prox1_timer_ms = int(8000 - (speed * 50))  # 9s → 4s
        prox1_timer_ms = max(3000, min(8000, prox1_timer_ms))
        
        prox3_timer_ms = int(9000 - (speed * 40))  # 9s → 5s  
        prox3_timer_ms = max(5000, min(9000, prox3_timer_ms))
        
        return {
            "prox1_timer_ms": prox1_timer_ms,
            "prox1_timer_s": prox1_timer_ms / 1000.0,  # ← ADD SECONDS
            "prox3_timer_ms": prox3_timer_ms,
            "prox3_timer_s": prox3_timer_ms / 1000.0,  # ← ADD SECONDS
            "robot_speed": speed
        }
        
    except Exception as e:
        print(f"❌ Timer calculation error: {e}")
        return None

def write_timers_to_plc(timer_values):
    """Write millisecond values to PLC DINT variables"""
    try:
        if not plc_client.get_connected():
            print("⚠️ PLC not connected - timers not written")
            return False
        
        # Write Prox1 timer to DB1.DBD12
        prox1_buffer = bytearray(4)
        snap7.util.set_dint(prox1_buffer, 0, timer_values["prox1_timer_ms"])
        plc_client.db_write(PLC_DB_NUMBER, 12, prox1_buffer)
        
        # Write Prox3 timer to DB1.DBD16
        prox3_buffer = bytearray(4)
        snap7.util.set_dint(prox3_buffer, 0, timer_values["prox3_timer_ms"])
        plc_client.db_write(PLC_DB_NUMBER, 16, prox3_buffer)
        
        # Brief wait for PLC to process
        time.sleep(0.1)
        
        # Verify write success
        verify_data = plc_client.db_read(PLC_DB_NUMBER, 12, 8)
        verify_prox1 = snap7.util.get_dint(verify_data, 0)
        verify_prox3 = snap7.util.get_dint(verify_data, 4)
        
        success = (verify_prox1 == timer_values['prox1_timer_ms'] and 
                  verify_prox3 == timer_values['prox3_timer_ms'])
        
        if success:
            print(f"✅ Dynamic timers updated: Prox1={timer_values['prox1_timer_s']:.1f}s, Prox3={timer_values['prox3_timer_s']:.1f}s")
        else:
            print(f"❌ Timer write verification failed")
        
        return success
        
    except Exception as e:
        print(f"❌ Error writing timers to PLC: {e}")
        return False

def read_current_timers_from_plc():
    """Read current timer values from PLC DB1 - SINGLE CORRECT VERSION"""
    try:
        if not plc_client.get_connected():
            return {"error": "PLC not connected"}
            
        # Read timer values from DB1 (DINT = 4 bytes each)
        # Offset 12 = Conveyor1_DynamicTimer, Offset 16 = Conveyor3_DynamicTimer
        timer_data = plc_client.db_read(PLC_DB_NUMBER, 12, 8)  # Read 8 bytes (2 DINTs)
        
        prox1_timer_ms = snap7.util.get_dint(timer_data, 0)  # Offset 12
        prox3_timer_ms = snap7.util.get_dint(timer_data, 4)  # Offset 16 (4 bytes after offset 12)
        
        # Also get current robot speed from file
        robot_speed = get_robot_speed_from_file()
        
        return {
            "robot_speed": robot_speed,
            "prox1_timer_ms": prox1_timer_ms,
            "prox1_timer_s": prox1_timer_ms / 1000.0,
            "prox3_timer_ms": prox3_timer_ms,
            "prox3_timer_s": prox3_timer_ms / 1000.0,
            "status": "connected"
        }
        
    except Exception as e:
        return {"error": str(e)}

def update_robot_speed_and_timers(new_percentage):
    """Update robot speed AND calculate/write new timer values"""
    try:
        # Calculate new timer values based on speed
        timer_values = calculate_dynamic_timers(new_percentage)
        if not timer_values:
            print("❌ Failed to calculate timer values")
            return False
            
        # Update robot speed
        speed_success = update_robot_speed_file(new_percentage)
        
        if speed_success:
            # Write new timer values to PLC
            timer_success = write_timers_to_plc(timer_values)
            
            if timer_success:
                print(f"🎯 Robot speed {new_percentage}% and dynamic timers updated successfully!")
                return True
            else:
                print(f"⚠️ Speed updated but timer write failed")
                return False
        else:
            print(f"❌ Speed update failed")
            return False
            
    except Exception as e:
        print(f"❌ Error in update_robot_speed_and_timers: {e}")
        return False

def calculate_intelligent_cost_per_part():
    """Calculate cost with clear confidence indicators"""
    
    energy_calc = intelligent_calculator.calculate_realistic_energy_per_cycle()
    electricity_rate = 0.15
    
    cost_per_part = energy_calc['total_energy_kwh'] * electricity_rate
    
    # Add confidence and improvement suggestions
    suggestions = []
    if energy_calc['confidence_level'] == 'baseline':
        suggestions.append("Run a few production cycles to get more accurate data")
        suggestions.append("Current estimates based on your actual system patterns")
    elif energy_calc['confidence_level'] == 'medium':
        suggestions.append("Collect more cycle data for higher accuracy")
    
    return {
        'energy_cost_per_part_eur': cost_per_part,
        'energy_per_part_kwh': energy_calc['total_energy_kwh'],
        'confidence_level': energy_calc['confidence_level'],
        'data_source': energy_calc['data_source'],
        'cycle_breakdown': energy_calc['breakdown'],
        'improvement_suggestions': suggestions,
        'calculation_notes': energy_calc.get('notes', ''),
        'robot_speed': energy_calc['robot_speed']
    }

def get_intelligent_energy_context():
    """Provide context with appropriate confidence levels"""
    
    cost_data = calculate_intelligent_cost_per_part()
    
    # ADD THIS WARNING FOR AI
    context_lines = [
        "⚠️ IMPORTANT: For 'energy per part' questions, use cycle analysis, NOT current idle state!",
        "",
        "INTELLIGENT ENERGY COST ANALYSIS:",
        "",
        "PRODUCTION CYCLE ESTIMATE (NOT CURRENT IDLE STATE):",
        f"- Energy Cost per Part: €{cost_data['energy_cost_per_part_eur']:.6f}",
        f"- Energy per Part: {cost_data['energy_per_part_kwh']:.6f} kWh",
        f"- Robot Speed: {cost_data['robot_speed']}%",
        "",
        f"CONFIDENCE LEVEL: {cost_data['confidence_level'].upper()}",
        "Based on your actual system utilization patterns during production",
        "",
        "COMPONENT BREAKDOWN DURING PRODUCTION:"
    ]
    
    # Add component breakdown with confidence indicators
    for component, data in cost_data['cycle_breakdown'].items():
        confidence_icon = "🎯" if data['confidence'] == 'real_data' else "📊"
        context_lines.append(f"- {component}: {data['active_time_s']:.1f}s active ({data['utilization_percent']:.1f}%) {confidence_icon}")
    
    context_lines.extend([
        "",
        f"DATA SOURCE: {cost_data['data_source']}",
        "",
        "IMPROVEMENT SUGGESTIONS:"
    ])
    
    for suggestion in cost_data['improvement_suggestions']:
        context_lines.append(f"• {suggestion}")
    
    context_lines.append("")
    context_lines.append("This analysis provides meaningful estimates even without production history,")
    context_lines.append("and will automatically improve as real data becomes available.")
    
    return "\n".join(context_lines)

def update_cycle_analysis():
    """Call this in your main update loop"""
    try:
        current_plc_data = plc_vars_state.copy()
        cycle_analyzer.analyze_plc_cycle(current_plc_data)
    except Exception as e:
        print(f"❌ Cycle analysis error: {e}")

def determine_assembly_stage(step_status):
    """Determine overall assembly stage based on current steps"""
    arm1_step = step_status['arms']['arm_1']['name']
    arm2_step = step_status['arms']['arm_2']['name']
    arm3_step = step_status['arms']['arm_3']['name']
    
    # Determine stage based on active steps
    if arm1_step != "IDLE" and "CONVEYOR" not in arm1_step:
        return "HOLDER_PLACEMENT_IN_PROGRESS"
    elif arm1_step in ["TO_CONVEYOR", "PLACING"]:
        return "HOLDER_BEING_PLACED_ON_CONVEYOR"
    elif arm2_step != "IDLE" and arm3_step != "IDLE":
        return "PCB_ASSEMBLY_IN_PROGRESS"
    elif arm2_step in ["TO_CONVEYOR2", "PLACING"]:
        return "FINAL_PLACEMENT_ON_CONVEYOR2"
    elif all(step['name'] == "IDLE" for step in step_status['arms'].values()):
        return "SYSTEM_IDLE"
    else:
        return "TRANSITION_BETWEEN_STAGES"

async def determine_assembly_stage_ai(step_status):
    """AI-powered assembly stage determination"""
    
    stage_data = {
        "current_steps": step_status,
        "plc_states": plc_vars_state,
        "step_history": step_tracker.get_step_history(limit=20),
        "assembly_workflow": {
            "stage_1": "Arm 1 places PCB holder onto Conveyor 1",
            "stage_2": "Conveyor 1 transports holder from Point A to Point B",
            "stage_3": "Arm 2 picks up holder, Arm 3 delivers PCB board",
            "stage_4": "Arms 2 and 3 work together to mount PCB onto holder",
            "stage_5": "Arm 2 transfers unit to Conveyor 2",
            "stage_6": "Conveyor 2 moves to final assembly stage"
        },
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Determine the current assembly stage based on step tracking and PLC states.

STAGE DATA:
{json.dumps(stage_data, indent=2)}

REQUIREMENTS:
1. Analyze current arm steps and progress
2. Consider PLC states (arm and conveyor activity)
3. Review recent step history for context
4. Determine exact assembly stage

RESPONSE FORMAT:
Return the current assembly stage as one of:
- SYSTEM_IDLE
- HOLDER_PLACEMENT_IN_PROGRESS
- HOLDER_BEING_PLACED_ON_CONVEYOR
- TRANSPORT_TO_POINT_B
- PCB_ASSEMBLY_IN_PROGRESS
- FINAL_PLACEMENT_ON_CONVEYOR2
- FINAL_TRANSPORT
- TRANSITION_BETWEEN_STAGES

Provide reasoning for the determination.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return "SYSTEM_IDLE"

def get_robot_speed_file():
    """Get current robot speed from shared file"""
    try:
        if os.path.exists(SPEED_FILE):
            with open(SPEED_FILE, 'r') as f:
                data = json.load(f)
                return data.get('speed_percentage', 50)
    except:
        pass
    return 50

# ⭐ INTELLIGENT STATUS FUNCTIONS
def get_intelligent_status_summary():
    """Generate intelligent status summary"""
    refresh_all_states_from_plc()
    step_status = step_tracker.get_current_status()
    
    # Calculate progress
    active_arms = [f"Arm {i}" for i in [1,2,3] if step_status['arms'][f'arm_{i}']['name'] != "IDLE"]
    current_stage = determine_assembly_stage(step_status)
    total_progress = sum(step_status['arms'][f'arm_{i}']['progress'] for i in [1,2,3]) / 3
    
    return {
        "current_stage": current_stage,
        "active_arms": active_arms,
        "total_progress": total_progress,
        "detailed_steps": {
            f"arm_{i}": {
                "step": step_status['arms'][f'arm_{i}']['name'],
                "progress": step_status['arms'][f'arm_{i}']['progress']
            } for i in [1,2,3]
        },
        "plc_states": plc_vars_state,
        "next_expected_action": predict_next_action(step_status, plc_vars_state)
    }

async def get_intelligent_status_summary_ai():
    """AI-powered intelligent status summary"""
    
    try:
        summary_data = {
            "plc_states": plc_vars_state,
            "step_tracking": step_tracker.get_current_status(),
            "step_history": step_tracker.get_step_history(limit=30),
            "energy_consumption": get_current_energy_consumption_sync(),
            "production_metrics": calculate_production_metrics(),
            "system_utilization_raw": calculate_system_utilization(),
            "timestamp": datetime.now().isoformat()
        }
        
        ai_prompt = f"""
Generate intelligent status summary with predictions and analysis.

COMPLETE SYSTEM DATA:
{json.dumps(summary_data, indent=2)}

REQUIREMENTS:
1. Determine current assembly stage
2. Calculate overall progress percentage
3. Identify active arms and their current operations
4. Predict next expected action
5. Provide operational insights

RESPONSE FORMAT:
Return a JSON object with:
{{
    "current_stage": "<assembly stage>",
    "active_arms": ["<list of active arms>"],
    "total_progress": <percentage>,
    "detailed_steps": {{
        "arm_1": {{"step": "<step name>", "progress": <percentage>}},
        "arm_2": {{"step": "<step name>", "progress": <percentage>}},
        "arm_3": {{"step": "<step name>", "progress": <percentage>}}
    }},
    "next_expected_action": "<prediction>",
    "operational_insights": "<analysis and recommendations>"
}}

Base all calculations on the provided real-time data.
"""
        
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
        
    except Exception as e:
        print(f"AI analysis error: {e}")
        # Return fallback data
        return {
            "current_stage": "SYSTEM_IDLE",
            "active_arms": [],
            "total_progress": 0,
            "detailed_steps": {
                "arm_1": {"step": "IDLE", "progress": 0},
                "arm_2": {"step": "IDLE", "progress": 0},
                "arm_3": {"step": "IDLE", "progress": 0}
            },
            "next_expected_action": "System ready for operation",
            "operational_insights": "All systems idle and ready"
        }

def predict_next_action(step_status, plc_states):
    """Predict what should happen next based on current assembly stage"""
    arm1_step = step_status['arms']['arm_1']['name']
    arm2_step = step_status['arms']['arm_2']['name']
    arm3_step = step_status['arms']['arm_3']['name']
    
    # Get current progress percentages
    arm1_progress = step_status['arms']['arm_1']['progress']
    arm2_progress = step_status['arms']['arm_2']['progress']
    arm3_progress = step_status['arms']['arm_3']['progress']
    
    # 🎯 SPECIFIC PREDICTIONS BASED ON CURRENT STEPS
    
    # Arm 1 is actively placing holder
    if arm1_step in ["PLACING", "TO_CONVEYOR"] and arm1_progress < 100:
        return "Completing holder placement on conveyor"
    
    # Arm 1 finished placing, conveyor should start
    if arm1_step == "PLACING" and arm1_progress >= 90 and not plc_states['Conveyor_1']:
        return "Conveyor 1 should start to transport holder"
    
    # Conveyor 1 is running, prepare for pickup
    if plc_states['Conveyor_1'] and arm2_step == "IDLE":
        return "Arm 2 should prepare for holder pickup at Point B"
    
    # Arm 2 is active but Arm 3 isn't - need PCB
    if arm2_step != "IDLE" and arm3_step == "IDLE" and arm2_progress < 80:
        return "Arm 3 should activate to deliver PCB board"
    
    # Both arms working on assembly
    if arm2_step in ["ASSEMBLING", "WAITING_ARM3", "TO_ASSEMBLY"] and arm3_step != "IDLE":
        return "PCB assembly in progress - coordinating arms"
    
    # Assembly complete, moving to final conveyor
    if arm2_step in ["TO_CONVEYOR2", "PLACING"] and not plc_states['Conveyor_2']:
        return "Conveyor 2 should start for final transport"
    
    # Conveyor 2 running - final stage
    if plc_states['Conveyor_2']:
        return "Final transport to completion station"
    
    # 🎯 STAGE-BASED PREDICTIONS
    current_stage = determine_assembly_stage(step_status)
    
    stage_actions = {
        "HOLDER_PLACEMENT_IN_PROGRESS": "Moving holder to conveyor position",
        "HOLDER_BEING_PLACED_ON_CONVEYOR": "Activating conveyor transport",
        "PCB_ASSEMBLY_IN_PROGRESS": "Coordinating dual-arm assembly",
        "FINAL_PLACEMENT_ON_CONVEYOR2": "Preparing final transport",
        "TRANSITION_BETWEEN_STAGES": "Synchronizing next assembly step",
        "SYSTEM_IDLE": "Waiting for start signal or next cycle"
    }
    
    return stage_actions.get(current_stage, "Monitoring system status")

def format_detailed_arm_status(detailed_steps):
    """Format detailed arm status for AI"""
    formatted = []
    for arm_key, data in detailed_steps.items():
        arm_num = arm_key.split('_')[1]
        step_name = data['step']
        progress = data['progress']
        
        if step_name == "IDLE":
            status = f"Arm {arm_num}: IDLE (waiting for trigger)"
        else:
            status = f"Arm {arm_num}: {step_name} - {progress}% complete"
        
        formatted.append(status)
    
    return "\n".join(formatted)

# ⭐ PRODUCTION METRICS
def calculate_production_metrics():
    """Calculate real-time production metrics - INTERPOLATED FROM REAL DATA + STEP ANALYSIS"""
    
    # 🎯 GET CURRENT ROBOT SPEED PERCENTAGE
    current_speed_percentage = get_robot_speed_from_file()
    
    # 🎯 INTERNAL DATA POINTS (HIDDEN FROM AI CONTEXT)
    speed_data_points = [
        (30, 100),   # 30% speed = 100 seconds
        (50, 80),    # 50% speed = 80 seconds  
        (75, 70),    # 75% speed = 70 seconds
        (100, 66)    # 100% speed = 66 seconds
    ]
    
    # 🎯 BASE TIMING DATA (KEEP FOR REFERENCE)
    BASE_SPEED_PERCENTAGE = 50
    BASE_CYCLE_TIME = 80  # seconds per cycle at 50% speed
    CYCLES_PER_OPERATION = 2  # 2 cycles per operation
    
    # 🎯 CALCULATE DYNAMIC CYCLE TIME USING INTERPOLATION (IMPROVED!)
    interpolated_cycle_time = interpolate_cycle_time(current_speed_percentage, speed_data_points)
    
    # 🎯 CALCULATE PRODUCTION RATES BASED ON INTERPOLATED TIME
    cycles_per_hour = 3600 / interpolated_cycle_time
    operations_per_hour = 3600 / (interpolated_cycle_time * CYCLES_PER_OPERATION)
    parts_per_hour = operations_per_hour * 2  # 2 parts per operation
    
    # 🎯 GET REAL MEASURED TIME FROM JSON (HIGHEST PRIORITY!)
    real_time = get_real_cycle_time()
    measured_cycle_time = real_time if real_time else interpolated_cycle_time
    
    # 🎯 STEP HISTORY ANALYSIS (KEEP YOUR EXCELLENT LOGIC!)
    step_history = step_tracker.get_step_history(limit=100)
    completed_operations = [h for h in step_history['history'] 
                        if 'COMPLETE' in h.get('step_name', '') or h.get('step_name') == 'IDLE']
    
    step_calculated_time = None
    if len(completed_operations) >= 4:  # Need at least 2 complete operations
        try:
            operation_times = []
            for i in range(2, len(completed_operations), 2):  # Every 2 completions = 1 operation
                time_diff = (datetime.fromisoformat(completed_operations[i]['timestamp']) - 
                            datetime.fromisoformat(completed_operations[i-2]['timestamp'])).total_seconds()
                if 60 < time_diff < 600:  # Filter reasonable operation times (1-10 minutes)
                    operation_times.append(time_diff / 2)  # Divide by 2 to get cycle time
            
            if operation_times:
                step_calculated_time = sum(operation_times) / len(operation_times)

        except Exception as e:
            print(f"Error calculating step timing: {e}")
    
    # 🎯 ENHANCED LOGGING WITH DATA SOURCE PRIORITY (CLEANED UP)
    if real_time:
        print(f"📊 Using REAL measured cycle time: {measured_cycle_time:.1f}s")
        data_source = "MEASURED_FROM_PRODUCTION"
        calculation_method = "measured"
    else:
        print(f"📊 Using calculated cycle time: {measured_cycle_time:.1f}s at {current_speed_percentage}% speed")
        data_source = "CALCULATED_FROM_PERFORMANCE_DATA"
        calculation_method = "interpolated"
    
    # 🎯 CALCULATE EFFICIENCY IMPROVEMENT VS BASELINE
    efficiency_improvement = ((BASE_CYCLE_TIME - measured_cycle_time) / BASE_CYCLE_TIME) * 100
    
    return {
        "current_speed_percentage": current_speed_percentage,
        "base_cycle_time": BASE_CYCLE_TIME,
        "calculated_cycle_time": interpolated_cycle_time,  
        "measured_cycle_time": measured_cycle_time,  
        "parts_per_cycle": 1,
        "step_calculated_time": step_calculated_time,  
        "cycles_per_operation": CYCLES_PER_OPERATION,
        "operation_time": measured_cycle_time * CYCLES_PER_OPERATION,
        "parts_per_operation": 2,
        "cycles_per_hour": 3600 / measured_cycle_time,  
        "operations_per_hour": 3600 / (measured_cycle_time * CYCLES_PER_OPERATION),
        "parts_per_hour": (3600 / (measured_cycle_time * CYCLES_PER_OPERATION)) * 2,
        "parts_per_8_hour_shift": ((3600 / (measured_cycle_time * CYCLES_PER_OPERATION)) * 2) * 8,
        "current_efficiency": efficiency_improvement,  
        "speed_impact": f"Speed {current_speed_percentage}% provides {efficiency_improvement:+.1f}% efficiency vs baseline",
        "data_source": data_source,  
        "calculation_method": calculation_method,  

    }

def interpolate_cycle_time(target_speed, data_points):
    """Interpolate cycle time based on real data points"""
    
    # Sort data points by speed
    data_points = sorted(data_points, key=lambda x: x[0])
    
    # Handle edge cases
    if target_speed <= data_points[0][0]:
        return data_points[0][1]  # Below minimum speed
    if target_speed >= data_points[-1][0]:
        return data_points[-1][1]  # Above maximum speed
    
    # Find the two points to interpolate between
    for i in range(len(data_points) - 1):
        speed1, time1 = data_points[i]
        speed2, time2 = data_points[i + 1]
        
        if speed1 <= target_speed <= speed2:
            # Linear interpolation between the two points
            ratio = (target_speed - speed1) / (speed2 - speed1)
            interpolated_time = time1 + ratio * (time2 - time1)
            
            return interpolated_time
    
    # Fallback (should never reach here)
    return 80.0

def calculate_system_utilization():
    """Calculate real system utilization"""
    active_arms = sum(1 for i in [1,2,3] if plc_vars_state.get(f'Arm_{i}', False))
    active_conveyors = sum(1 for i in [1,2] if plc_vars_state.get(f'Conveyor_{i}', False))
    
    return {
        "arms_utilization": (active_arms / 3) * 100,
        "conveyor_utilization": (active_conveyors / 2) * 100,
        "overall_utilization": ((active_arms + active_conveyors) / 5) * 100,
        "robot_speed": get_robot_speed_from_file()
    }

def get_real_cycle_time():
    """Get actual measured cycle time - ONLY if it's real measured data"""
    
    # Check if we have timing data
    if os.path.exists(CYCLE_TIMING_FILE):
        try:
            with open(CYCLE_TIMING_FILE, 'r') as f:
                data = json.load(f)
                
                # ✅ ONLY TRUST DATA FROM ACTUAL ROBOT RUNS
                updated_by = data.get('updated_by', '')
                calculation_method = data.get('calculation_method', '')
                
                # Reject data created by speed changes (calculated data)
                if 'main.py_speed_change' in updated_by or calculation_method == 'speed_based':
                    print(f" Ignoring calculated cycle time from speed change: {data.get('cycle_time', 0):.1f}s")
                    return None
                
                # Only accept data from actual robot measurements
                if 'robot_arm_run' in updated_by or calculation_method == 'measured':
                    cycle_time = data.get('last_cycle_duration', 0) or data.get('cycle_time', 0)
                    if cycle_time > 0:
                        print(f"✅ Using REAL measured cycle time: {cycle_time:.1f}s")
                        return cycle_time
                        
        except Exception as e:
            print(f"Error reading cycle timing: {e}")
    
    # If no real data, tell user to run a cycle
    print(" No real cycle timing available - using calculated estimate")
    return None

# ⭐ ENERGY FUNCTIONS
def get_current_energy_consumption_sync():
    try:
        energy_data = get_energy_status()
        production_metrics = calculate_production_metrics()  # ← MOVE OUTSIDE DICT
        cycle_time = production_metrics['measured_cycle_time']
        
        return {
            "total_power_w": energy_data.get("total_power", 0),
            "robots_power": sum(arm["power"] for arm in energy_data.get("robots", {}).values()),
            "conveyors_power": sum(conv["power"] for conv in energy_data.get("conveyors", {}).values()),
            "energy_per_cycle_kwh": (energy_data.get("total_power", 0) / 1000) * (cycle_time / 3600)
        }
    except:
        return {"total_power_w": 350, "energy_per_cycle_kwh": 0.0286}

async def get_current_energy_consumption():
    """Get real-time energy consumption data"""
    return get_current_energy_consumption_sync()

# ⭐ COST ANALYSIS
def calculate_energy_cost_per_part():
    """Calculate energy cost per part - ENERGY ONLY"""
    try:
        energy_data = get_energy_status()
        current_power_w = energy_data.get('total_power', 350)
        
        # Get production metrics for accurate cycle time
        production_metrics = calculate_production_metrics()
        cycle_time = production_metrics['measured_cycle_time']
        
        # Energy calculations based on cycle time
        energy_per_cycle_kwh = (current_power_w / 1000) * (cycle_time / 3600)
        
        # Electricity cost
        electricity_rate = 0.15  # €0.15 per kWh
        energy_cost_per_part = energy_per_cycle_kwh * electricity_rate
        
        return {
            "cycle_time": cycle_time,
            "energy_per_cycle_kwh": energy_per_cycle_kwh,
            "energy_cost_per_part": energy_cost_per_part,
            "daily_energy_cost": energy_cost_per_part * production_metrics['parts_per_8_hour_shift'],
            "current_power_w": current_power_w,
            "parts_per_hour": production_metrics['parts_per_hour']
        }
    except Exception as e:
        print(f"Error calculating costs: {e}")
        return {
            "error": str(e),
            "energy_cost_per_part": 0.002,
            "current_power_w": 350
        }

async def calculate_energy_cost_per_part_ai():
    """AI-powered cost analysis"""
    
    # Gather real system data
    current_data = {
        "energy_consumption": get_current_energy_consumption_sync(),
        "production_metrics": calculate_production_metrics(),
        "plc_states": plc_vars_state,
        "step_tracking": step_tracker.get_current_status(),
        "system_utilization": calculate_system_utilization(),
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Calculate detailed cost analysis for this manufacturing system.

CURRENT SYSTEM DATA:
{json.dumps(current_data, indent=2)}

COST ANALYSIS REQUIREMENTS:
1. Energy cost per part (based on real cycle times)
2. Total cost per part
3. Daily cost projections
4. Cost optimization opportunities

RESPONSE FORMAT:
Provide detailed cost breakdown in structured format with:
- Energy costs (Part 1 vs Part 2)
- Total costs
- Daily projections
- Cost optimization suggestions

Use current electricity rates and realistic cost factors.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI cost analysis: {e}"

# ⭐ TIMING ANALYSIS
def analyze_timing_breakdown():
    """Analyze timing breakdown of the assembly process"""
    # Based on your real timing data
    timing_breakdown = {
        "part_1": {
            "total_time": 152,  # 2:32
            "arm_1_seq_1": 50,  # ~50 seconds
            "coordination_wait": 25,  # wait for arm 3
            "arms_2_3_assembly": 77,  # remaining time
            "efficiency": 82.9  # (50+77)/152 * 100
        },
        "part_2": {
            "total_time": 142,  # 4:54 - 2:32
            "arm_1_seq_2": 50,  # ~50 seconds  
            "coordination_wait": 52,  # longer wait
            "arms_2_3_assembly": 40,  # remaining time
            "efficiency": 63.4  # (50+40)/142 * 100
        },
        "bottlenecks": [
            "Part 2 has 52-second coordination wait (36.6% of cycle)",
            "Arm coordination delays reduce overall efficiency",
            "Part 1 is more efficient (82.9%) than Part 2 (63.4%)"
        ],
        "optimization_opportunities": [
            "Reduce Part 2 coordination wait from 52s to 30s (-15% cycle time)",
            "Parallel processing during wait times",
            "Optimize arm sequencing for better coordination"
        ]
    }
    return timing_breakdown

async def get_production_time_breakdown_ai():
    """AI-powered production time analysis"""
    
    # Get real-time data
    step_history = step_tracker.get_step_history(limit=50)
    current_status = step_tracker.get_current_status()
    
    production_data = {
        "real_cycle_data": {
            "total_cycle_time": 294,
            "part_1_time": 152,
            "part_2_time": 142,
            "parts_per_cycle": 2
        },
        "current_step_status": current_status,
        "recent_step_history": step_history,
        "plc_states": plc_vars_state,
        "energy_data": get_current_energy_consumption_sync(),
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Analyze production timing and provide detailed breakdown.

PRODUCTION DATA:
{json.dumps(production_data, indent=2)}

ANALYSIS REQUIREMENTS:
1. Detailed timing breakdown for each part
2. Identify bottlenecks and inefficiencies
3. Calculate production rates and capacity
4. Analyze step-by-step timing
5. Suggest optimization opportunities
6. Compare actual vs theoretical performance

RESPONSE FORMAT:
Provide comprehensive analysis with:
- Current Cycle Performance
- Detailed Breakdown (Part 1 vs Part 2)
- Production Rate calculations
- Bottleneck identification
- Optimization recommendations

Base analysis on real timing data provided.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI production analysis: {e}"

# ⭐ BOTTLENECK ANALYSIS
def analyze_process_bottlenecks():
    """Analyze process bottlenecks"""
    timing = analyze_timing_breakdown()
    return {
        "bottlenecks": timing['bottlenecks'],
        "optimization_opportunities": timing['optimization_opportunities'],
        "part_1_efficiency": timing['part_1']['efficiency'],
        "part_2_efficiency": timing['part_2']['efficiency'],
        "overall_efficiency": (timing['part_1']['efficiency'] + timing['part_2']['efficiency']) / 2
    }

async def analyze_process_bottlenecks_ai():
    """AI-powered bottleneck analysis"""
    
    system_data = {
        "plc_states": plc_vars_state,
        "energy_consumption": get_current_energy_consumption_sync(),
        "production_metrics": calculate_production_metrics(),
        "step_tracking": step_tracker.get_current_status(),
        "step_history": step_tracker.get_step_history(limit=50),
        "system_utilization": calculate_system_utilization(),
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Analyze bottlenecks in this manufacturing system.

SYSTEM DATA:
{json.dumps(system_data, indent=2)}

BOTTLENECK ANALYSIS REQUIREMENTS:
1. Identify current bottlenecks in the assembly process
2. Analyze step timing inefficiencies
3. Find coordination delays between arms
4. Identify conveyor transport delays
5. Calculate impact of each bottleneck on overall throughput
6. Provide specific optimization recommendations

RESPONSE FORMAT:
Provide detailed bottleneck analysis with:
- Current bottlenecks identified
- Impact assessment for each bottleneck
- Root cause analysis
- Optimization recommendations
- Expected improvement estimates

Base analysis on real system data and step tracking information.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI bottleneck analysis: {e}"

def extract_speed_simulation_intent(command_lower):
    """Extract speed simulation questions like 'what if I increase by 20%'"""
    import re
    
    # Check for simulation indicators
    simulation_indicators = [
        "what happens", "what would happen", "if i", "if we", "simulate", 
        "what if", "suppose", "imagine", "estimate", "predict", "impact"
    ]
    
    if not any(indicator in command_lower for indicator in simulation_indicators):
        return None
    
    # Extract percentage from simulation question
    percentage_match = re.search(r'(\d+)\s*%', command_lower)
    if percentage_match:
        percentage = int(percentage_match.group(1))
        
        if any(word in command_lower for word in ["increase", "boost", "faster", "up"]):
            return {'type': 'simulation', 'direction': 'increase', 'amount': percentage}
        elif any(word in command_lower for word in ["decrease", "reduce", "slower", "down"]):
            return {'type': 'simulation', 'direction': 'decrease', 'amount': percentage}
    
    return None

def extract_speed_change_intent(command_lower):
    """Extract speed change commands - but NOT simulation questions"""
    import re
    
    print(f"🔍 DEBUG: Parsing command: '{command_lower}'")
    
    # ⭐ FIRST: Check if this is a simulation/what-if question (BLOCK ACTUAL CHANGES)
    simulation_indicators = [
        "what happens", "what would happen", "what will happen",
        "if i", "if we", "if the", "suppose", "imagine", "estimate", 
        "predict", "impact", "what if", "simulate", "simulation",
        "would it", "could it", "should i", "what about",
        "how about", "let's say", "assuming", "hypothetically"
    ]
    
    if any(indicator in command_lower for indicator in simulation_indicators):
        print(f"🔍 DEBUG: This is a SIMULATION question, not a speed change command")
        return None  # Don't treat simulations as actual speed changes
    
    # ⭐ SECOND: Check for ACTUAL speed change commands (only imperative/direct commands)
    actual_change_indicators = [
        "set speed to", "change speed to", "update speed to", "adjust speed to",
        "increase speed by", "decrease speed by", "reduce speed by", "boost speed by",
        "raise speed by", "lower speed by", "speed up by", "slow down by",
        "make it faster", "make it slower", "speed up", "slow down",
        "go faster", "go slower", "boost", "throttle", "accelerate", "decelerate"
    ]
    
    # Check if this is actually a command (not a question)
    has_actual_command = any(indicator in command_lower for indicator in actual_change_indicators)
    
    # If no clear command indicators, check for imperative tone
    if not has_actual_command:
        # Check for direct imperative patterns without question words
        question_words = ["what", "how", "why", "when", "where", "which", "who", "?"]
        has_question = any(word in command_lower for word in question_words)
        
        if has_question:
            print(f"🔍 DEBUG: Contains question words - likely not a command")
            return None
    
    print(f"🔍 DEBUG: Proceeding with speed change detection...")
    
    # Pattern 1: ABSOLUTE SETTING - "set/change/reduce speed TO X%" 
    absolute_patterns = [
        r'\b(set|change|update|adjust|make|put|reduce)\s+(?:the\s+)?(?:robot\s+)?speed\s+(?:to\s+|by\s+)?(\d+)\s*%',
        r'\bspeed\s+(?:to\s+)?(\d+)\s*%',
        r'\b(\d+)\s*%\s+speed'
    ]
    
    for pattern in absolute_patterns:
        match = re.search(pattern, command_lower)
        if match:
            # ✅ FIXED: Safely extract the number from any group
            groups = match.groups()
            if len(groups) >= 2 and groups[1] and groups[1].isdigit():
                amount = int(groups[1])
            elif len(groups) >= 1 and groups[0] and groups[0].isdigit():
                amount = int(groups[0])
            else:
                # Extract any number from the match
                number_match = re.search(r'(\d+)', match.group(0))
                if number_match:
                    amount = int(number_match.group(1))
                else:
                    continue
            
            print(f"🔍 DEBUG: Matched ABSOLUTE: set to {amount}%")
            return {'type': 'absolute', 'amount': amount}
    
    # Pattern 2: RELATIVE INCREASE - "increase/boost speed BY X%"
    increase_patterns = [
        r'\b(increase|boost|raise|up|accelerate)\s+(?:the\s+)?(?:robot\s+)?speed\s+by\s+(\d+)\s*%',
        r'\bspeed\s+up\s+by\s+(\d+)\s*%',
        r'\bgo\s+(\d+)\s*%\s+faster',
        r'\badd\s+(\d+)\s*%\s+(?:to\s+)?(?:the\s+)?speed'
    ]
    
    for pattern in increase_patterns:
        match = re.search(pattern, command_lower)
        if match:
            # ✅ FIXED: Safely extract the number
            groups = match.groups()
            amount = None
            for group in reversed(groups):  # Check from last to first
                if group and group.isdigit():
                    amount = int(group)
                    break
            
            if amount is None:
                continue
                
            print(f"🔍 DEBUG: Matched RELATIVE INCREASE: +{amount}%")
            return {'type': 'percentage', 'direction': 'increase', 'amount': amount}
    
    # Pattern 3: RELATIVE DECREASE - "decrease/reduce speed BY X%"
    decrease_patterns = [
        r'\b(decrease|reduce|lower|down|decelerate)\s+(?:the\s+)?(?:robot\s+)?speed\s+by\s+(\d+)\s*%',
        r'\bspeed\s+down\s+by\s+(\d+)\s*%',
        r'\bgo\s+(\d+)\s*%\s+slower',
        r'\bsubtract\s+(\d+)\s*%\s+(?:from\s+)?(?:the\s+)?speed'
    ]
    
    for pattern in decrease_patterns:
        match = re.search(pattern, command_lower)
        if match:
            # ✅ FIXED: Safely extract the number
            groups = match.groups()
            amount = None
            for group in reversed(groups):  # Check from last to first
                if group and group.isdigit():
                    amount = int(group)
                    break
            
            if amount is None:
                continue
                
            print(f"🔍 DEBUG: Matched RELATIVE DECREASE: -{amount}%")
            return {'type': 'percentage', 'direction': 'decrease', 'amount': amount}
    
    # Pattern 4: Simple directional commands (no specific percentage)
    simple_increase_triggers = [
        'faster', 'speed up', 'go faster', 'boost', 'accelerate', 
        'throttle up', 'increase speed', 'more speed', 'quicker'
    ]
    
    simple_decrease_triggers = [
        'slower', 'slow down', 'go slower', 'decelerate', 
        'throttle down', 'decrease speed', 'less speed', 'reduce speed'
    ]
    
    for trigger in simple_increase_triggers:
        if trigger in command_lower:
            print(f"🔍 DEBUG: Matched SIMPLE FASTER: +20%")
            return {'type': 'percentage', 'direction': 'increase', 'amount': 20}
    
    for trigger in simple_decrease_triggers:
        if trigger in command_lower:
            print(f"🔍 DEBUG: Matched SIMPLE SLOWER: -20%")
            return {'type': 'percentage', 'direction': 'decrease', 'amount': 20}
    
    # Pattern 5: Extreme commands
    extreme_patterns = {
        'max': {'type': 'absolute', 'amount': 100},
        'maximum': {'type': 'absolute', 'amount': 100},
        'full speed': {'type': 'absolute', 'amount': 100},
        'max speed': {'type': 'absolute', 'amount': 100},
        'maximum speed': {'type': 'absolute', 'amount': 100},
        'min': {'type': 'absolute', 'amount': 10},
        'minimum': {'type': 'absolute', 'amount': 10},
        'min speed': {'type': 'absolute', 'amount': 10},
        'minimum speed': {'type': 'absolute', 'amount': 10},
        'stop': {'type': 'absolute', 'amount': 0},
        'halt': {'type': 'absolute', 'amount': 0}
    }
    
    for trigger, result in extreme_patterns.items():
        if trigger in command_lower:
            print(f"🔍 DEBUG: Matched EXTREME: {trigger} -> {result}")
            return result
    
    # Pattern 6: Percentage-only patterns (like "80%" or "increase 25")
    percentage_only_patterns = [
        r'\b(\d+)\s*%(?:\s+(?:speed|please|now))?$',  # Just "80%" or "80% speed"
        r'(?:make\s+it\s+|set\s+to\s+)?(\d+)\s*percent',  # "25 percent" or "make it 80 percent"
        r'\b(?:increase|decrease|change|reduce)\s+(?:by\s+)?(\d+)(?:\s*%)?$'  # "reduce 25" or "change 50"
    ]
    
    for pattern in percentage_only_patterns:
        match = re.search(pattern, command_lower)
        if match:
            amount = int(match.group(1))
            # Determine if it's absolute or relative based on context
            if any(word in command_lower for word in ['increase', 'boost', 'up', 'more', 'add']):
                print(f"🔍 DEBUG: Matched PERCENTAGE INCREASE: +{amount}%")
                return {'type': 'percentage', 'direction': 'increase', 'amount': amount}
            elif any(word in command_lower for word in ['decrease', 'reduce', 'down', 'less', 'subtract']):
                print(f"🔍 DEBUG: Matched PERCENTAGE DECREASE: -{amount}%")
                return {'type': 'percentage', 'direction': 'decrease', 'amount': amount}
            else:
                # Default to absolute setting for standalone percentages
                print(f"🔍 DEBUG: Matched PERCENTAGE ABSOLUTE: set to {amount}%")
                return {'type': 'absolute', 'amount': amount}
    
    print(f"🔍 DEBUG: No speed pattern matched")
    return None

def read_prox_sensors_from_plc():
    """Read all 4 prox sensors from PLC using your exact addresses"""
    try:
        if not plc_client.get_connected():
            return {
                "Proximity_Sensor1": False,
                "Proximity_Sensor2": False,
                "Proximity_Sensor3": False,
                "Proximity_Sensor4": False,
                "error": "PLC not connected"
            }
        
        # Read prox sensor data from DB1 (your exact offsets)
        prox_data = plc_client.db_read(PLC_DB_NUMBER, 10, 1)  # Read 1 byte from offset 10
        
        prox_sensors = {
            "Proximity_Sensor1": snap7.util.get_bool(prox_data, 0, 3),  # DB1.DBX10.3
            "Proximity_Sensor2": snap7.util.get_bool(prox_data, 0, 4),  # DB1.DBX10.4
            "Proximity_Sensor3": snap7.util.get_bool(prox_data, 0, 5),  # DB1.DBX10.5
            "Proximity_Sensor4": snap7.util.get_bool(prox_data, 0, 6),  # DB1.DBX10.6
        }
        
        return prox_sensors
        
    except Exception as e:
        print(f"❌ Error reading prox sensors: {e}")
        return {
            "Proximity_Sensor1": False,
            "Proximity_Sensor2": False,
            "Proximity_Sensor3": False,
            "Proximity_Sensor4": False,
            "error": str(e)
        }

def get_robot_speed_file():
    """Get current robot speed from shared file - ALIAS"""
    return get_robot_speed_from_file()

_speed_cache = {"value": 50, "timestamp": 0}
CACHE_DURATION = 0.5  # 500ms cache

def get_robot_speed_from_file():
    now = time.time()
    if now - _speed_cache["timestamp"] < CACHE_DURATION:
        return _speed_cache["value"]  # Return cached value
    
    # Cache expired - read from file
    try:
        with open(SPEED_FILE, 'r') as f:
            data = json.load(f)
            speed = data.get('speed_percentage', 50)
            _speed_cache["value"] = speed
            _speed_cache["timestamp"] = now
            return speed
    except:
        return _speed_cache["value"]  # Return last known good value

def update_robot_speed_file(new_percentage):
    """Update robot speed via shared file with complete cycle time calculations - ENHANCED ERROR HANDLING"""
    try:
        # ✅ ENHANCED: Check directory permissions first
        file_dir = os.path.dirname(SPEED_FILE) or "."
        if not os.path.exists(file_dir):
            
            os.makedirs(file_dir, exist_ok=True)
        
        if not os.access(file_dir, os.W_OK):
            raise PermissionError(f"No write permission to directory: {file_dir}")
        
        # Check if file exists and permissions
        if os.path.exists(SPEED_FILE):
            print(f"🔧 DEBUG: Speed file exists, checking permissions...")
            if not os.access(SPEED_FILE, os.W_OK):
                raise PermissionError(f"No write permission to file: {SPEED_FILE}")
            try:
                with open(SPEED_FILE, 'r') as f:
                    current_data = json.load(f)
                    print(f"🔧 DEBUG: Current speed in file: {current_data.get('speed_percentage', 'unknown')}")
            except json.JSONDecodeError as json_error:
                print(f"🔧 DEBUG: Invalid JSON in existing file: {json_error}")
                # Continue anyway, we'll overwrite it
            except Exception as read_error:
                print(f"🔧 DEBUG: Error reading existing file: {read_error}")
        else:
            print(f"🔧 DEBUG: Speed file does not exist, will create new one")
        
        # Clamp to 1-100%
        clamped_percentage = max(1, min(100, int(new_percentage)))
        
        
        # ✅ UPDATED: Use interpolation instead of formula
        speed_data_points = [
            (30, 100), (50, 80), (75, 70), (100, 66)
        ]
        new_cycle_time = interpolate_cycle_time(clamped_percentage, speed_data_points)
        
        
        # Calculate robot speeds
        move_speed = 100000 + ((8000000 - 100000) * (clamped_percentage / 100.0))
        catch_speed = 100000 + ((800000 - 100000) * (clamped_percentage / 100.0))
        
        
        # Prepare speed data
        speed_data = {
            'speed_percentage': clamped_percentage,
            'timestamp': time.time(),
            'move_speed': int(move_speed),
            'catch_speed': int(catch_speed),
            'updated_by': 'main.py',
            'cycle_time_calculated': round(new_cycle_time, 1)
        }
        
        
        # ✅ ENHANCED: Try to write speed file with better error handling
        print(f"🔧 DEBUG: Attempting to write speed file...")
        try:
            with open(SPEED_FILE, 'w') as f:
                json.dump(speed_data, f, indent=2)
            
        except IOError as io_error:
            raise IOError(f"Failed to write speed file: {io_error}")
        except Exception as write_error:
            raise Exception(f"Unexpected error writing speed file: {write_error}")
        
        # ✅ ENHANCED: Verify the file was written correctly
        try:
            with open(SPEED_FILE, 'r') as f:
                verify_data = json.load(f)
                if verify_data.get('speed_percentage') != clamped_percentage:
                    raise ValueError(f"File verification failed: expected {clamped_percentage}%, got {verify_data.get('speed_percentage')}%")
            print(f"🔧 DEBUG: Speed file verification successful!")
        except Exception as verify_error:
            raise Exception(f"Speed file verification failed: {verify_error}")
        
        # ✅ ALSO UPDATE CYCLE TIMING FILE
        cycle_data = {
            "cycle_time": round(new_cycle_time, 1),
            "last_cycle_duration": round(new_cycle_time, 1),
            "speed_percentage": clamped_percentage,
            "timestamp": time.time(),
            "updated_by": "main.py_speed_change",
            "calculation_method": "interpolated_from_real_data"
        }
        
        
        # Try to write cycle timing file
        
        try:
            with open(CYCLE_TIMING_FILE, 'w') as f:
                json.dump(cycle_data, f, indent=2)
            print(f"🔧 DEBUG: Cycle timing file written successfully!")
        except Exception as cycle_error:
            print(f"🔧 WARNING: Failed to write cycle timing file: {cycle_error}")
            # Don't fail the whole operation for this
        
        print(f"✅ SUCCESS: Robot speed updated to {clamped_percentage}%")
        
        # ✅ WRITE SPEED TO PLC FOR PROCESS SIMULATE SYNC
        try:
            import struct
            # Map chatbot speed to Process Simulate (20% chatbot → 100% PS)
            ps_speed = float(clamped_percentage) * 5.0
            
            # Write to DB1.DBD22 (DB1, offset 22, REAL type)
            speed_bytes = bytearray(struct.pack('>f', ps_speed))
            plc_client.db_write(1, 22, speed_bytes)
            
            print(f"🎬 Process Simulate: {clamped_percentage}% → {ps_speed}% (written to DB1.DBD22)")
        except Exception as plc_error:
            print(f"⚠️ Process Simulate sync failed: {plc_error}")
            # Don't fail - file update already succeeded
        
        return True
        
    except PermissionError as perm_error:
        print(f"💥 PERMISSION ERROR: {perm_error}")
        return False
    except IOError as io_error:
        print(f"💥 FILE I/O ERROR: {io_error}")
        return False
    except json.JSONEncodeError as json_error:
        print(f"💥 JSON ENCODING ERROR: {json_error}")
        return False
    except Exception as e:
        print(f"💥 UNEXPECTED ERROR in update_robot_speed_file: {type(e).__name__}: {e}")
        import traceback
        print("💥 Full traceback:")
        traceback.print_exc()
        return False

def simulate_speed_impact(percentage_change):
    """Simulate the impact of speed change - FIXED VERSION"""
    
    # ✅ NEW WAY: This function is deprecated - use simulate_speed_impact_with_speeds
    print("⚠️ Warning: simulate_speed_impact() called with percentage_change - this may be incorrect after speed updates")
    current_speed = get_robot_speed_from_file()
    target_speed = current_speed + percentage_change
    
    return simulate_speed_impact_with_speeds(current_speed, target_speed)

def simulate_speed_impact_with_speeds(current_speed, target_speed):
    """Calculate speed impact using REAL INTERPOLATED data when available"""
    
    print(f"🔍 SPEED IMPACT CALCULATION:")
    print(f"   Current speed: {current_speed}%")
    print(f"   Target speed: {target_speed}%")
    
    # ✅ TRY TO GET REAL MEASURED CYCLE TIME FIRST
    real_current_time = get_real_cycle_time()
    
    if real_current_time:
        # Use REAL measured data as baseline
        print(f"   Using REAL MEASURED current cycle time: {real_current_time:.1f}s")
        current_cycle_time = real_current_time
        
        # Scale the measured time to target speed using interpolation
        speed_data_points = [
            (30, 100), (50, 80), (75, 70), (100, 66)
        ]
        new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
        print(f"   Target cycle time from interpolation: {new_cycle_time:.1f}s")
    else:
        # Use interpolation for both current and target
        speed_data_points = [
            (30, 100), (50, 80), (75, 70), (100, 66)
        ]
        current_cycle_time = interpolate_cycle_time(current_speed, speed_data_points)
        new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
        print(f"   Using INTERPOLATED times: {current_cycle_time:.1f}s → {new_cycle_time:.1f}s")
    
    # Calculate throughput
    current_throughput = 3600 / current_cycle_time
    new_throughput = 3600 / new_cycle_time
    time_saved_per_cycle = current_cycle_time - new_cycle_time
    throughput_increase = new_throughput - current_throughput
    
    return {
        'current_cycle_time': current_cycle_time,
        'new_cycle_time': new_cycle_time,
        'time_saved_per_cycle': time_saved_per_cycle,
        'new_throughput': new_throughput,
        'throughput_increase': throughput_increase
    }

def debug_cycle_time_sources():
    """Debug all cycle time data sources"""
    print("🔍 CYCLE TIME DEBUG - ALL SOURCES:")
    
    # Source 1: JSON file
    json_time = get_real_cycle_time()
    print(f"   JSON file (cycle_timing.json): {json_time}")
    
    # Source 2: Speed-based calculation
    current_speed = get_robot_speed_from_file()
    calculated_time = 80 * (50 / current_speed)
    print(f"   Speed calculation (80 * 50/{current_speed}): {calculated_time:.1f}s")
    
    # Source 3: Production metrics
    metrics = calculate_production_metrics()
    print(f"   Production metrics measured: {metrics['measured_cycle_time']:.1f}s")
    print(f"   Production metrics calculated: {metrics['calculated_cycle_time']:.1f}s")
    
    # Source 4: Check what simulate_speed_impact uses
    print(f"   Current robot speed from file: {current_speed}%")
    
    return {
        "json_time": json_time,
        "calculated_time": calculated_time,
        "metrics_measured": metrics['measured_cycle_time'],
        "metrics_calculated": metrics['calculated_cycle_time'],
        "current_speed": current_speed
    }

# ⭐ MAINTENANCE PREDICTION
def predict_maintenance_needs():
    """Predict maintenance needs"""
    total_prod = plc_vars_state.get('Total_Prod', 0)
    robot_speed = get_robot_speed_from_file()
    
    # Simple maintenance prediction based on usage
    cycles_until_maintenance = max(0, 10000 - total_prod)
    maintenance_urgency = "LOW" if cycles_until_maintenance > 5000 else "MEDIUM" if cycles_until_maintenance > 1000 else "HIGH"
    
    return {
        "cycles_until_maintenance": cycles_until_maintenance,
        "maintenance_urgency": maintenance_urgency,
        "total_production": total_prod,
        "robot_speed": robot_speed,
        "recommendations": [
            "Check arm lubrication" if cycles_until_maintenance < 2000 else "Normal operation",
            "Inspect conveyor belts" if cycles_until_maintenance < 3000 else "Belts OK",
            "Calibrate sensors" if cycles_until_maintenance < 1000 else "Sensors OK"
        ]
    }

async def predict_maintenance_needs_ai():
    """AI-powered predictive maintenance analysis"""
    
    system_data = {
        "plc_states": plc_vars_state,
        "energy_consumption": get_current_energy_consumption_sync(),
        "robot_speed": get_robot_speed_from_file(),
        "total_production": plc_vars_state.get('Total_Prod', 0),
        "step_history": step_tracker.get_step_history(limit=100),
        "system_utilization": calculate_system_utilization(),
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Predict maintenance needs for this manufacturing system.

SYSTEM DATA:
{json.dumps(system_data, indent=2)}

PREDICTIVE MAINTENANCE REQUIREMENTS:
1. Analyze equipment usage patterns
2. Assess wear and tear based on operation cycles
3. Predict maintenance intervals for each component
4. Identify early warning signs of potential failures
5. Calculate maintenance costs and scheduling
6. Provide preventive maintenance recommendations

RESPONSE FORMAT:
Provide comprehensive maintenance prediction with:
- Equipment health assessment
- Predicted maintenance schedules
- Risk analysis for each component
- Cost estimates for maintenance
- Preventive action recommendations
- Performance optimization suggestions

Base predictions on actual usage data and operational patterns.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI maintenance prediction: {e}"

# ⭐ SPEED SIMULATION

async def simulate_speed_impact_ai(speed_increase_percent):
    """AI-powered speed impact simulation - using REAL INTERPOLATION DATA"""
    
    # ✅ GET CURRENT SPEED AND CALCULATE TARGET
    current_speed = get_robot_speed_from_file()
    target_speed = current_speed + speed_increase_percent
    target_speed = max(1, min(100, target_speed))  # Clamp to valid range
    
    # ✅ USE YOUR INTERPOLATION SYSTEM
    speed_data_points = [
        (30, 100), (50, 80), (75, 70), (100, 66)
    ]
    
    # ✅ GET REAL MEASURED DATA IF AVAILABLE
    real_current_time = get_real_cycle_time()
    
    if real_current_time:
        # Use REAL measured data as baseline
        current_cycle_time = real_current_time
        new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
        data_source = "MEASURED_CURRENT_INTERPOLATED_TARGET"
    else:
        # Use interpolation for both
        current_cycle_time = interpolate_cycle_time(current_speed, speed_data_points)
        new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
        data_source = "INTERPOLATED_FROM_REAL_MEASUREMENTS"
    
    # ✅ CALCULATE REAL IMPACT
    time_saved_per_cycle = current_cycle_time - new_cycle_time
    current_throughput = 3600 / current_cycle_time  # cycles per hour
    new_throughput = 3600 / new_cycle_time
    throughput_increase = new_throughput - current_throughput
    
    # ✅ PRODUCTION METRICS (1 part per cycle)
    current_parts_per_hour = current_throughput
    new_parts_per_hour = new_throughput
    parts_increase = new_parts_per_hour - current_parts_per_hour
    
    # ✅ DAILY PRODUCTION (8-hour shift)
    current_daily_parts = current_parts_per_hour * 8
    new_daily_parts = new_parts_per_hour * 8
    daily_parts_increase = new_daily_parts - current_daily_parts
    
    current_data = {
        "speed_analysis": {
            "current_speed": current_speed,
            "target_speed": target_speed,
            "speed_increase": speed_increase_percent,
            "current_cycle_time": current_cycle_time,
            "new_cycle_time": new_cycle_time,
            "time_saved_per_cycle": time_saved_per_cycle,
            "current_throughput": current_throughput,
            "new_throughput": new_throughput,
            "throughput_increase": throughput_increase,
            "current_parts_per_hour": current_parts_per_hour,
            "new_parts_per_hour": new_parts_per_hour,
            "parts_increase_per_hour": parts_increase,
            "current_daily_parts": current_daily_parts,
            "new_daily_parts": new_daily_parts,
            "daily_parts_increase": daily_parts_increase,
            "percentage_improvement": (throughput_increase / current_throughput) * 100,
            "data_source": data_source
        },
        "interpolation_reference": speed_data_points,
        "current_energy_consumption": get_current_energy_consumption_sync(),
        "plc_states": plc_vars_state,
        "step_tracking": step_tracker.get_current_status(),
        "system_utilization": calculate_system_utilization(),
        "timestamp": datetime.now().isoformat()
    }
    
    ai_prompt = f"""
Analyze the impact of a {speed_increase_percent}% speed increase on this manufacturing system.

CALCULATED IMPACT DATA (using REAL INTERPOLATED MEASUREMENTS):
{json.dumps(current_data, indent=2)}

ANALYSIS REQUIREMENTS:
1. Interpret the calculated performance changes (based on real system measurements)
2. Estimate energy consumption changes (physics-based - power increases with speed)
3. Calculate cost implications (energy vs productivity gains)
4. Provide implementation recommendations
5. Assess risks and benefits
6. Consider power tier changes (50% threshold: ≤50% = 150W/arm, >50% = 250W/arm)

RESPONSE FORMAT:
Provide detailed analysis with:
- **Performance Impact Summary** (cycle time, throughput, daily production)
- **Energy & Cost Implications** (power consumption changes, cost per hour/day)
- **Implementation Recommendations** (optimal timing, considerations)
- **Risk Assessment** (equipment stress, power tier changes)
- **Expected Outcomes** (ROI, efficiency gains)

POWER CONSUMPTION RULES:
- Each arm: 5W (idle), 150W (≤50% speed), 250W (>50% speed)
- Each conveyor: 2W (idle), 50W (running)
- Current speed: {current_speed}% → Target speed: {target_speed}%
- Power tier change: {"YES" if (current_speed <= 50) != (target_speed <= 50) else "NO"}

Use the calculated data provided - all cycle times are from real measurements or interpolation.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI speed simulation: {e}"

async def analyze_speed_increase_ai(increase_percentage=20):
    """AI-powered speed analysis - using REAL INTERPOLATION DATA"""
    
    # ✅ GET CURRENT SPEED AND CALCULATE TARGET
    current_speed = get_robot_speed_from_file()
    target_speed = current_speed + increase_percentage
    target_speed = max(1, min(100, target_speed))  # Clamp to valid range
    
    # ✅ USE YOUR INTERPOLATION SYSTEM
    speed_data_points = [
        (30, 100), (50, 80), (75, 70), (100, 66)
    ]
    
    current_cycle_time = interpolate_cycle_time(current_speed, speed_data_points)
    new_cycle_time = interpolate_cycle_time(target_speed, speed_data_points)
    
    # ✅ CALCULATE REAL IMPACT
    time_saved_per_cycle = current_cycle_time - new_cycle_time
    current_throughput = 3600 / current_cycle_time  # cycles per hour
    new_throughput = 3600 / new_cycle_time
    throughput_increase = new_throughput - current_throughput
    
    # ✅ PRODUCTION METRICS (2 parts per operation, 2 cycles per operation)
    current_parts_per_hour = (current_throughput / 2) * 2  # 1 part per cycle
    new_parts_per_hour = (new_throughput / 2) * 2
    parts_increase = new_parts_per_hour - current_parts_per_hour
    
    # Collect system data with REAL calculated results
    current_data = {
        "speed_analysis": {
            "current_speed": current_speed,
            "target_speed": target_speed,
            "current_cycle_time": current_cycle_time,  # ✅ FROM INTERPOLATION
            "new_cycle_time": new_cycle_time,          # ✅ FROM INTERPOLATION
            "time_saved_per_cycle": time_saved_per_cycle,
            "current_throughput": current_throughput,
            "new_throughput": new_throughput,
            "throughput_increase": throughput_increase,
            "current_parts_per_hour": current_parts_per_hour,
            "new_parts_per_hour": new_parts_per_hour,
            "parts_increase": parts_increase,
            "percentage_improvement": (throughput_increase / current_throughput) * 100,
            "data_source": "interpolated_from_real_measurements"  # ✅ SHOW SOURCE
        },
        "speed_change_percentage": increase_percentage,
        "interpolation_data": speed_data_points,  # ✅ SHOW REFERENCE DATA
        "current_energy_per_cycle": get_current_energy_consumption_sync(),
        "active_arms": {
            "arm_1": plc_vars_state.get('Arm_1', False),
            "arm_2": plc_vars_state.get('Arm_2', False),
            "arm_3": plc_vars_state.get('Arm_3', False)
        },
        "active_conveyors": {
            "conveyor_1": plc_vars_state.get('Conveyor_1', False),
            "conveyor_2": plc_vars_state.get('Conveyor_2', False)
        },
        "timestamp": datetime.now().isoformat()
    }
    
    # Create AI prompt for comprehensive analysis
    ai_prompt = f"""
Analyze the impact of a {increase_percentage}% speed increase on this manufacturing system.

SPEED IMPACT ANALYSIS (using REAL INTERPOLATED DATA):
{json.dumps(current_data, indent=2)}

ANALYSIS REQUIREMENTS:
1. Interpret the calculated performance improvements (based on real measurements)
2. Estimate energy consumption changes (consider physics - energy increases exponentially with speed)
3. Calculate cost implications (energy vs productivity gains)
4. Assess implementation feasibility and risks
5. Provide specific recommendations

RESPONSE FORMAT:
Provide a detailed analysis in markdown format with:
- **Performance Changes** section
- **Energy & Cost Impact** section  
- **Daily Production Impact** section
- **Implementation Considerations** section
- **Bottom Line Recommendation**

Use realistic physics for energy calculations and consider equipment stress factors.
Base your analysis on the INTERPOLATED performance data from real measurements.
"""
    
    try:
        # Send to AI API for processing
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return f"Error getting AI analysis: {e}. Please try again."

# ⭐ MANUFACTURING INTELLIGENCE
def handle_manufacturing_intelligence(command_lower, plc_vars):
    
    return None  # Let regular AI handle other queries

async def handle_manufacturing_intelligence_ai(command_lower, plc_vars):
    """AI-powered manufacturing intelligence - no hardcoded values"""
    
    # Collect all relevant system data
    system_data = {
        "plc_states": plc_vars,
        "energy_consumption": get_current_energy_consumption_sync(),
        "production_metrics": calculate_production_metrics(),
        "step_tracking": step_tracker.get_current_status(),
        "step_history": step_tracker.get_step_history(limit=20),
        "system_utilization": calculate_system_utilization(),
        "user_query": command_lower,
        "timestamp": datetime.now().isoformat()
    }
    
    # Determine query type and create appropriate prompt
    if any(word in command_lower for word in ["parts per hour", "throughput", "production rate"]):
        analysis_type = "PRODUCTION_ANALYSIS"
    elif any(word in command_lower for word in ["cost per part", "energy cost", "total cost"]):
        analysis_type = "COST_ANALYSIS"
    elif any(word in command_lower for word in ["timing", "breakdown", "cycle analysis"]):
        analysis_type = "TIMING_ANALYSIS"
    elif "speed" in command_lower and any(word in command_lower for word in ["increase", "faster", "boost"]):
        analysis_type = "SPEED_IMPACT_ANALYSIS"
    else:
        analysis_type = "GENERAL_MANUFACTURING_ANALYSIS"
    
    ai_prompt = f"""
Perform {analysis_type} for this manufacturing system.

USER QUERY: {command_lower}

COMPLETE SYSTEM DATA:
{json.dumps(system_data, indent=2)}

ANALYSIS REQUIREMENTS:
Based on the user's query type ({analysis_type}), provide:
1. Relevant calculations using the real system data
2. Current performance metrics
3. Actionable insights
4. Specific recommendations
5. Data-driven conclusions

RESPONSE FORMAT:
Provide detailed analysis in markdown format appropriate for the query type.
Use the real system data provided - do not use hardcoded values.
Calculate everything dynamically based on current system state.
"""
    
    try:
        ai_response = send_gpt_request_internal(ai_prompt, get_plc_status_string())
        return ai_response
    except Exception as e:
        return None  # Fall back to regular AI

# ⭐ REQUEST MODELS
class CommandRequest(BaseModel):
    command: str
    session_id: Optional[str] = "default"  # Default session for conversation history

@app.get("/api/all_scenarios")
async def get_all_scenarios():
    """
    Returns an exhaustive list of all possible ON/OFF scenarios for arms and conveyors,
    along with their calculated power consumption.
    """
    result = []
    for sc in scenario_generator():
        # Transform keys from 'Arm_1' to 1 for calculate_power_for_custom_state
        arm_states_for_calc = {int(k.split('_')[1]): v for k, v in sc.items() if 'Arm' in k}
        conveyor_states_for_calc = {int(k.split('_')[1]): v for k, v in sc.items() if 'Conveyor' in k}
        
        power, details = calculate_power_for_custom_state(arm_states_for_calc, conveyor_states_for_calc)
        result.append({
            "scenario": sc,
            "power": power,
            "details": details
        })
    return result

@app.get("/api/custom_scenarios")
async def get_custom_scenarios(
    arms: Optional[str] = Query(None, description="Comma-separated list of 0s and 1s for Arm_1, Arm_2, Arm_3 states (e.g., '0,1,0')"),
    conveyors: Optional[str] = Query(None, description="Comma-separated list of 0s and 1s for Conveyor_1, Conveyor_2 states (e.g., '1,0')")
):
    """
    Returns scenarios matching specific arm and/or conveyor states, along with their power consumption.
    Example: /api/custom_scenarios?arms=0,1,0&conveyors=1,0
    """
    def constraint(s):
        match = True
        if arms:
            try:
                arm_states_list = [bool(int(x)) for x in arms.split(',')]
                if len(arm_states_list) != 3:
                    raise ValueError("Arms parameter must have 3 values.")
                for i in range(3):
                    if s[f"Arm_{i+1}"] != arm_states_list[i]:
                        match = False
                        break
            except (ValueError, IndexError) as e:
                raise HTTPException(status_code=400, detail=f"Invalid 'arms' parameter: {e}. Expected '0,1,0'.")

        if match and conveyors:
            try:
                conveyor_states_list = [bool(int(x)) for x in conveyors.split(',')]
                if len(conveyor_states_list) != 2:
                    raise ValueError("Conveyors parameter must have 2 values.")
                for i in range(2):
                    if s[f"Conveyor_{i+1}"] != conveyor_states_list[i]:
                        match = False
                        break
            except (ValueError, IndexError) as e:
                raise HTTPException(status_code=400, detail=f"Invalid 'conveyors' parameter: {e}. Expected '1,0'.")
        return match

    result = []
    for sc in scenario_generator(constraint):
        arm_states_for_calc = {int(k.split('_')[1]): v for k, v in sc.items() if 'Arm' in k}
        conveyor_states_for_calc = {int(k.split('_')[1]): v for k, v in sc.items() if 'Conveyor' in k}
        
        power, details = calculate_power_for_custom_state(arm_states_for_calc, conveyor_states_for_calc)
        result.append({
            "scenario": sc,
            "power": power,
            "details": details
        })
    return result

@app.post("/api/command")
async def handle_command(request: CommandRequest):
    """
    Main command handler with clean architecture:
    - Emergency commands processed first
    - AI classification for intelligent routing
    - Modular helper functions for each command type
    - Configuration-driven instead of hardcoded
    """
    global plc_vars_state
    refresh_arm_state_from_plc()
    user_command = request.command
    command_lower = user_command.lower().strip()
    dynamic_context_str = get_plc_status_string()

    # ============================================================================
    # 📋 CONFIGURATION - All hardcoded values in one place
    # ============================================================================
    
    CONFIG = {
        'emergency_patterns': ["emergency stop", "e-stop", "halt", "emergency", "abort"],
        'electricity_rate': 0.15,  # €/kWh
        'speed_limits': {'min': 1, 'max': 100},
        'power_threshold': 50,  # Speed threshold for power tier change
        'power_levels': {
            'high_speed': {'arm': 250, 'conveyor': 50, 'arm_idle': 5, 'conveyor_idle': 2},
            'low_speed': {'arm': 150, 'conveyor': 50, 'arm_idle': 5, 'conveyor_idle': 2}
        },
        'default_speed_change': 20,  # Default increment/decrement
        'hours_per_shift': 8,
        'hours_per_day': 24
    }

    # ============================================================================
    # 🔧 HELPER FUNCTIONS - Modular logic
    # ============================================================================
    
    def get_power_tier(speed):
        """Determine power tier based on speed"""
        return 'high_speed' if speed > CONFIG['power_threshold'] else 'low_speed'
    
    def get_arm_power(speed):
        """Get arm power consumption for given speed"""
        tier = get_power_tier(speed)
        return CONFIG['power_levels'][tier]['arm']
    
    def calculate_system_power(speed, active_arms, running_conveyors):
        """Calculate total system power consumption"""
        tier = get_power_tier(speed)
        power_config = CONFIG['power_levels'][tier]
        
        active_arm_power = active_arms * power_config['arm']
        idle_arm_power = (3 - active_arms) * power_config['arm_idle']
        active_conveyor_power = running_conveyors * power_config['conveyor']
        idle_conveyor_power = (2 - running_conveyors) * power_config['conveyor_idle']
        
        return {
            'total': active_arm_power + idle_arm_power + active_conveyor_power + idle_conveyor_power,
            'active_arms': active_arm_power,
            'idle_arms': idle_arm_power,
            'active_conveyors': active_conveyor_power,
            'idle_conveyors': idle_conveyor_power,
            'tier': tier,
            'arm_power': power_config['arm']
        }
    
    def calculate_cost_impact(power_change_watts):
        """Calculate hourly and daily cost impact from power change"""
        hourly_cost = (power_change_watts / 1000) * CONFIG['electricity_rate']
        daily_cost = hourly_cost * CONFIG['hours_per_day']
        return {'hourly': hourly_cost, 'daily': daily_cost}
    
    def clamp_speed(speed):
        """Clamp speed to valid range"""
        return max(CONFIG['speed_limits']['min'], min(CONFIG['speed_limits']['max'], speed))
    
    def calculate_robot_speeds(speed_percent):
        """Calculate actual robot move and catch speeds"""
        move_speed = 100000 + ((8000000 - 100000) * (speed_percent / 100.0))
        catch_speed = 100000 + ((800000 - 100000) * (speed_percent / 100.0))
        return {'move': int(move_speed), 'catch': int(catch_speed)}
    
    def extract_target_speed_from_analysis(ai_analysis, current_speed):
        """Extract target speed from AI analysis response"""
        import re
        
        # Check for absolute speed
        absolute_match = re.search(r"TARGET_SPEED:\s*(\d+)", ai_analysis)
        if absolute_match:
            return int(absolute_match.group(1))
        
        # Check for relative changes
        if "SPEED_DIRECTION: INCREASE" in ai_analysis:
            amount_match = re.search(r"SPEED_AMOUNT:\s*(\d+)", ai_analysis)
            amount = int(amount_match.group(1)) if amount_match else CONFIG['default_speed_change']
            return current_speed + amount
        
        if "SPEED_DIRECTION: DECREASE" in ai_analysis:
            amount_match = re.search(r"SPEED_AMOUNT:\s*(\d+)", ai_analysis)
            amount = int(amount_match.group(1)) if amount_match else CONFIG['default_speed_change']
            return current_speed - amount
        
        return None
    
    def format_power_breakdown(power_data, active_arms, running_conveyors):
        """Format power breakdown section"""
        return f"""**🔌 Current System Power Breakdown:**
- Active arms: {active_arms}/3 × {power_data['arm_power']}W = {power_data['active_arms']}W
- Idle arms: {3 - active_arms}/3 × 5W = {power_data['idle_arms']}W
- Running conveyors: {running_conveyors}/2 × 50W = {power_data['active_conveyors']}W
- Idle conveyors: {2 - running_conveyors}/2 × 2W = {power_data['idle_conveyors']}W
- **Total system power: {power_data['total']}W**"""
    
    def format_production_impact(simulation, current_speed, target_speed):
        """Format production impact section"""
        cycle_time_change = simulation['new_cycle_time'] - simulation['current_cycle_time']
        cycle_time_pct = (cycle_time_change / simulation['current_cycle_time']) * 100
        throughput_pct = (simulation['throughput_increase'] / (simulation['new_throughput'] - simulation['throughput_increase'])) * 100 if simulation['new_throughput'] != simulation['throughput_increase'] else 0
        
        return f"""**📊 Production Impact:**
- New cycle time: {simulation['new_cycle_time']:.1f} seconds (was {simulation['current_cycle_time']:.1f}s)
- Cycle time change: {cycle_time_change:+.1f} seconds ({cycle_time_pct:+.1f}%)
- New throughput: {simulation['new_throughput']:.1f} parts/hour
- Throughput change: {simulation['throughput_increase']:+.1f} parts/hour ({throughput_pct:+.1f}%)
- Daily production change: {simulation['throughput_increase'] * CONFIG['hours_per_shift']:.0f} parts/day ({CONFIG['hours_per_shift']}h shift)"""
    
    def format_power_impact(current_power, new_power, current_speed, target_speed, active_arms):
        """Format power impact section"""
        lines = []
        current_tier = get_power_tier(current_speed)
        new_tier = get_power_tier(target_speed)
        
        lines.append(f"**⚡ Power Impact Analysis:**")
        lines.append(f"- Power mode change: {current_tier.upper().replace('_', ' ')} → {new_tier.upper().replace('_', ' ')}")
        lines.append(f"- Arm power: {current_power['arm_power']}W → {new_power['arm_power']}W per active arm")
        
        if current_tier != new_tier:
            power_change_per_arm = new_power['arm_power'] - current_power['arm_power']
            lines.append(f"- 🔴 **CRITICAL POWER TIER CHANGE!** Crossed {CONFIG['power_threshold']}% threshold")
            lines.append(f"- Power change per arm: {power_change_per_arm:+.0f}W")
            
            if active_arms == 0:
                lines.append(f"- **Note:** No arms currently active, no immediate power change")
                lines.append(f"- **When arms activate:** Each will consume {new_power['arm_power']}W (was {current_power['arm_power']}W)")
                lines.append(f"- **Potential system impact:** {power_change_per_arm * 3:+.0f}W if all arms active")
            else:
                immediate_change = new_power['total'] - current_power['total']
                lines.append(f"- **Immediate power change:** {immediate_change:+.0f}W (affecting {active_arms} active arms)")
        else:
            lines.append(f"- Power tier unchanged: {new_tier.upper().replace('_', ' ')} mode")
            if active_arms == 0:
                lines.append(f"- **Note:** No arms currently active, no power change")
            else:
                lines.append(f"- No power consumption change (same tier, {active_arms} active arms)")
        
        return "\n".join(lines)
    
    def format_cost_impact(current_power, new_power, current_speed, target_speed, active_arms):
        """Format cost impact section"""
        lines = []
        current_tier = get_power_tier(current_speed)
        new_tier = get_power_tier(target_speed)
        
        lines.append(f"**💰 Energy Cost Impact:**")
        
        if current_tier != new_tier:
            # Power tier changed - show potential impact
            potential_power_change = (new_power['arm_power'] - current_power['arm_power']) * 3
            cost_impact = calculate_cost_impact(potential_power_change)
            
            lines.append(f"- **POWER TIER CHANGED:** Cost impact when arms are active")
            lines.append(f"- Potential hourly cost change: €{cost_impact['hourly']:+.3f}/hour")
            lines.append(f"- Potential daily cost change: €{cost_impact['daily']:+.2f}/day")
            
            if active_arms > 0:
                immediate_power_change = new_power['total'] - current_power['total']
                immediate_cost = calculate_cost_impact(immediate_power_change)
                lines.append(f"- **Immediate cost change:** €{immediate_cost['hourly']:+.4f}/hour ({active_arms} active arms)")
            else:
                lines.append(f"- **Current cost change:** €0.000/hour (no arms active)")
        else:
            # Same power tier
            if active_arms > 0:
                lines.append(f"- No cost change (same power tier, {active_arms} active arms)")
            else:
                lines.append(f"- No cost change (same power tier, no arms active)")
        
        return "\n".join(lines)
    
    def get_efficiency_note(current_speed, target_speed, new_throughput):
        """Get efficiency optimization note"""
        if target_speed > current_speed:
            note = "💡 **Higher speed = Higher power but better throughput per watt**"
        elif target_speed < current_speed:
            note = "💡 **Lower speed = Lower power consumption and better energy efficiency**"
        else:
            note = "💡 **No speed change = No power impact**"
        
        # Add optimization suggestions
        new_tier = get_power_tier(target_speed)
        if new_tier == 'high_speed' and new_throughput < 50:
            note += f"\n💡 **Optimization Tip:** Consider reducing to ≤{CONFIG['power_threshold']}% speed for lower power consumption if production target allows"
        elif new_tier == 'low_speed' and new_throughput > 60:
            note += "\n💡 **Optimization Tip:** Current speed provides good efficiency balance"
        
        return note
    
    def build_speed_change_response(current_speed, target_speed, original_target, simulation, current_power, new_power, active_arms, running_conveyors):
        """Build comprehensive speed change response"""
        parts = []
        
        # Header
        percentage_change = target_speed - current_speed
        parts.append(f"✅ **Robot speed successfully updated from {current_speed}% to {target_speed}%** ({percentage_change:+.0f}%)")
        
        if original_target != target_speed:
            parts.append(f"⚠️ *Target speed clamped from {original_target}% to valid range ({CONFIG['speed_limits']['min']}-{CONFIG['speed_limits']['max']}%)*")
        
        # Robot speeds
        robot_speeds = calculate_robot_speeds(target_speed)
        parts.append(f"\n**🤖 Actual Robot Speeds:**")
        parts.append(f"- Move Speed: {robot_speeds['move']:,}")
        parts.append(f"- Catch Speed: {robot_speeds['catch']:,}")
        
        # Production impact
        parts.append(f"\n{format_production_impact(simulation, current_speed, target_speed)}")
        
        # Power impact
        parts.append(f"\n{format_power_impact(current_power, new_power, current_speed, target_speed, active_arms)}")
        
        # Cost impact
        parts.append(f"\n{format_cost_impact(current_power, new_power, current_speed, target_speed, active_arms)}")
        
        # Power breakdown
        parts.append(f"\n{format_power_breakdown(new_power, active_arms, running_conveyors)} (was {current_power['total']}W)")
        
        # Efficiency note
        parts.append(f"\n{get_efficiency_note(current_speed, target_speed, simulation['new_throughput'])}")
        
        # Footer
        parts.append("\n*The speed change has been applied and will take effect on the next robot operation cycle.*")
        
        return "\n".join(parts)
    
    def get_ai_classification_prompt(user_command, current_speed):
        """Generate AI classification prompt (kept separate for clarity)"""
        return f"""
COMMAND ANALYSIS TASK:
You are a command classifier for a robotic system. Your job is to distinguish between:
1. QUESTIONS (user wants information) → DO NOT EXECUTE
2. COMMANDS (user wants action) → EXECUTE

COMMAND: "{user_command}"
CURRENT ROBOT SPEED: {current_speed}%

⚠️ CRITICAL CLASSIFICATION RULES:

A command is a QUESTION (SCENARIO_ANALYSIS) if it has ANY of these characteristics:
- Contains a question mark "?"
- Starts with question words: what, how, when, where, why, which, who
- Contains conditional phrases: "if I", "if we", "suppose", "assuming"
- Contains future speculation: "will be", "would be", "will happen", "what happens"
- Contains phrases: "what will", "what would", "what if", "how much will", "how long will"
- Asks about impact: "what's the impact of", "what will my cycle time be", "what will production be"
- Asks for prediction or simulation
- Tone is inquisitive rather than imperative

A command is a COMMAND (SPEED_CHANGE) if it has ALL of these characteristics:
- No question mark
- Imperative/declarative tone (giving an order)
- Direct action verb: "set", "change", "increase", "decrease", "make", "speed up"
- No conditional "if" statements
- No question words
- Direct command format without asking

ACTION TYPES:
1. SPEED_CHANGE - User wants to ACTUALLY change robot speed (imperative commands ONLY)
2. SCENARIO_ANALYSIS - User asks "what if" or hypothetical questions (questions ONLY)
3. POWER_QUESTION - User asks about power consumption
4. STATUS_REQUEST - User wants current system status
5. PRODUCTION_QUESTION - User asks about production metrics
6. GENERAL_QUESTION - Other questions

EXAMPLES - STUDY THESE CAREFULLY:

❌ QUESTION (SCENARIO_ANALYSIS - NO EXECUTION):
User: "What will be my cycle time if I set speed to 90%?"
→ Contains "what will", has "?", conditional "if I"
→ ACTION_TYPE: SCENARIO_ANALYSIS
→ REASONING: Question asking for prediction, not a command to execute

✅ COMMAND (SPEED_CHANGE - EXECUTE):
User: "Set speed to 90%"
→ Imperative verb, no "?", no "if", direct command
→ ACTION_TYPE: SPEED_CHANGE
→ TARGET_SPEED: 90
→ SPEED_DIRECTION: ABSOLUTE
→ REASONING: Direct imperative command without question

CRITICAL DECISION RULES (Apply in order):
1. If command contains "?" → SCENARIO_ANALYSIS (unless it's a simple status question)
2. If command contains "what will", "what would", "what if" → SCENARIO_ANALYSIS
3. If command contains "if I", "if we" → SCENARIO_ANALYSIS
4. If command contains "how much will", "how long will" → SCENARIO_ANALYSIS
5. If command starts with question words → SCENARIO_ANALYSIS
6. If command is imperative (no question words, no "?", direct verb) → SPEED_CHANGE
7. When in doubt, prefer SCENARIO_ANALYSIS over SPEED_CHANGE (safer)

RESPONSE FORMAT (STRICT - MUST FOLLOW EXACTLY):
ACTION_TYPE: <SPEED_CHANGE|SCENARIO_ANALYSIS|POWER_QUESTION|STATUS_REQUEST|PRODUCTION_QUESTION|GENERAL_QUESTION>
CONFIDENCE: <HIGH|MEDIUM|LOW>
TARGET_SPEED: <number or N/A>
SPEED_DIRECTION: <ABSOLUTE|INCREASE|DECREASE|N/A>
SPEED_AMOUNT: <number or N/A>
REASONING: <one sentence explaining your classification>

⚠️ SAFETY RULE:
If you're unsure whether it's a question or command, ALWAYS choose SCENARIO_ANALYSIS.
It's safer to ask for confirmation than to execute unwanted changes.

Now classify this command: "{user_command}"
"""

    # ============================================================================
    # 🚨 MAIN COMMAND PROCESSING LOGIC
    # ============================================================================
    
    try:
        # 🚨 EMERGENCY COMMANDS - HIGHEST PRIORITY
        if command_lower == "stop":
            return {"response": update_stop(True), "plc_status": plc_vars_state}
        
        if command_lower == "start":
            return {"response": update_start(True), "plc_status": plc_vars_state}
        
        if command_lower == "reset":
            return {"response": update_reset(), "plc_status": plc_vars_state}
        
        # Check additional emergency patterns
        for pattern in CONFIG['emergency_patterns']:
            if pattern in command_lower:
                return {"response": f"🚨 EMERGENCY STOP: {update_stop(True)}", "plc_status": plc_vars_state}
        
        # 🎯 TIMER STATUS COMMANDS
        if "timer" in command_lower and "status" in command_lower:
            timer_data = read_current_timers_from_plc()
            
            if "error" not in timer_data:
                response = f"""📊 **Dynamic Timer Status:**

**Current Robot Speed:** {timer_data['robot_speed']}%

**Active Timer Values:**
- Prox Sensor 1 → Conveyor 1: {timer_data['prox1_timer_s']:.1f}s ({timer_data['prox1_timer_ms']}ms)
- Prox Sensor 3 → Conveyor 2: {timer_data['prox3_timer_s']:.1f}s ({timer_data['prox3_timer_ms']}ms)

**Timer Ranges:**
- Prox Sensor 1: 4.0s - 9.0s (scales with robot speed)
- Prox Sensor 3: 5.0s - 9.0s (scales with robot speed)

**Formula:**
- Prox1: 9000ms - (speed% × 50ms)
- Prox3: 9000ms - (speed% × 40ms)"""
            else:
                response = f"❌ Timer status error: {timer_data['error']}"
            
            return {"response": response, "plc_status": plc_vars_state}
        
        # 🧠 AI COMMAND CLASSIFICATION
        print(f"🧠 Processing command with AI classification (session: {request.session_id})...")
        
        current_speed = get_robot_speed_from_file()
        analysis_prompt = get_ai_classification_prompt(user_command, current_speed)
        ai_analysis = send_gpt_request_internal(analysis_prompt, dynamic_context_str, session_id=request.session_id)
        
        print(f"🧠 AI Analysis: {ai_analysis}")
        
        # 🎯 SPEED CHANGE EXECUTION
        if "ACTION_TYPE: SPEED_CHANGE" in ai_analysis:
            try:
                # Extract target speed
                target_speed = extract_target_speed_from_analysis(ai_analysis, current_speed)
                
                if target_speed is None:
                    return {
                        "response": "❌ Could not determine target speed from your command. Please specify a speed percentage (e.g., 'set speed to 75%').",
                        "plc_status": plc_vars_state
                    }
                
                # Clamp to valid range
                original_target = target_speed
                target_speed = clamp_speed(target_speed)
                
                # Get current system state
                refresh_all_states_from_plc()
                active_arms = sum(1 for i in [1,2,3] if plc_vars_state.get(f'Arm_{i}', False))
                running_conveyors = sum(1 for i in [1,2] if plc_vars_state.get(f'Conveyor_{i}', False))
                
                # Calculate power states
                current_power = calculate_system_power(current_speed, active_arms, running_conveyors)
                new_power = calculate_system_power(target_speed, active_arms, running_conveyors)
                
                # Execute speed change
                print(f"🔧 Executing speed change: {current_speed}% → {target_speed}%")
                success = update_robot_speed_and_timers(target_speed)
                
                if success:
                    # Calculate production impact
                    simulation = simulate_speed_impact_with_speeds(current_speed, target_speed)
                    
                    # Build response
                    response_message = build_speed_change_response(
                        current_speed, target_speed, original_target, simulation,
                        current_power, new_power, active_arms, running_conveyors
                    )
                else:
                    # Error handling
                    import os
                    try:
                        if not os.path.exists(os.path.dirname(SPEED_FILE) or "."):
                            error_detail = f"Directory '{os.path.dirname(SPEED_FILE)}' does not exist"
                        elif not os.access(os.path.dirname(SPEED_FILE) or ".", os.W_OK):
                            error_detail = f"No write permission to directory"
                        else:
                            error_detail = "Unknown file system error"
                    except Exception as diag_error:
                        error_detail = f"Diagnostic error: {diag_error}"
                    
                    response_message = f"""❌ **Failed to update robot speed from {current_speed}% to {target_speed}%**

**Error Details:** {error_detail}

**Troubleshooting Steps:**
1. Check if the application has write permissions
2. Verify that the disk is not full
3. Ensure no other process is locking the speed file
4. Check the console output for detailed error messages

**Current working directory:** {os.getcwd()}
**Speed file path:** {SPEED_FILE}"""
                
                return {"response": response_message, "plc_status": plc_vars_state}
                
            except Exception as e:
                return {"response": f"❌ Error executing speed change: {e}", "plc_status": plc_vars_state}
        
        # 🧠 HANDLE OTHER COMMAND TYPES (AI-powered responses with conversation history)
        print(f"🎯 Processing non-speed command with AI (session: {request.session_id})...")
        response_message = send_gpt_request_internal(user_command, dynamic_context_str, session_id=request.session_id)
        
        return {"response": response_message, "plc_status": plc_vars_state}
        
    except Exception as e:
        print(f"💥 COMMAND ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"response": f"Error processing command: {str(e)}", "plc_status": plc_vars_state}

@app.get("/api/plc_status")
async def get_status():
    refresh_arm_state_from_plc()
    refresh_all_states_from_plc()
    return plc_vars_state

@app.get("/api/robot_speed_percentage")
def get_robot_speed_percentage():
    """Get current robot speed as percentage"""
    try:
        current_percentage = get_robot_speed_from_file()
        return {
            "speed_percentage": current_percentage,
            "display_text": f"{current_percentage}%"
        }
    except Exception as e:
        return {
            "speed_percentage": 50,
            "display_text": "50%"
        }

@app.get("/api/energy_status")
def get_energy_status():
    """Get energy status from robot_controller.py status"""
    refresh_arm_state_from_plc()
    
    # ⭐ GET ROBOT STATUS FROM robot_controller.py
    try:
        robot_status_data = get_robot_running_status()
        robot_arms = robot_status_data.get('robots', {})
    except Exception as e:
        print(f"Error getting robot status: {e}")
        robot_arms = {}
    
    POWER_CONSUMPTION = {
        'robot_idle': 5,
        'robot_low_speed': 150,    # ← NEW: Low speed
        'robot_high_speed': 250,   # ← NEW: High speed
        'conveyor_idle': 2,
        'conveyor_running': 50
    }
    
    energy_data = {
        'total_power': 0,
        'robots': {},
        'conveyors': {},
        'timestamp': datetime.now().isoformat()
    }
    
    robot_speed = get_robot_speed_from_file()
    
    # ⭐ USE ROBOT STATUS FROM robot_controller.py INSTEAD OF PLC
    for arm_num in [1, 2, 3]:
        arm_key = f'arm_{arm_num}'
        
        # Get status from robot_controller.py
        arm_data = robot_arms.get(arm_key, {})
        is_moving = arm_data.get('is_moving', False)
        
        if is_moving:
            if robot_speed <= 50:  # ← NEW: 50% threshold
                power = POWER_CONSUMPTION['robot_low_speed']
                status_str = 'LOW_SPEED'  # ← NEW STATUS
            else:
                power = POWER_CONSUMPTION['robot_high_speed']
                status_str = 'HIGH_SPEED'  # ← NEW STATUS
        else:
            power = POWER_CONSUMPTION['robot_idle']
            status_str = 'IDLE'
            
        energy_data['robots'][f'arm_{arm_num}'] = {
            'running': is_moving,  # ✅ FROM ROBOT STATUS
            'power': power,
            'status': status_str
        }
        energy_data['total_power'] += power
    
    for n in [1, 2]:
        conv_key = f'Conveyor_{n}'
        running = plc_vars_state.get(conv_key, False)
        conv_power = POWER_CONSUMPTION['conveyor_running'] if running else POWER_CONSUMPTION['conveyor_idle']
        conv_status = 'RUNNING' if running else 'IDLE'
        energy_data['conveyors'][f'conveyor_{n}'] = {
            'running': running,
            'power': conv_power,
            'status': conv_status
        }
        energy_data['total_power'] += conv_power
    
    return energy_data

@app.post("/api/test_raw_timer_write")
def test_raw_timer_write():
    """Test writing specific values to timer locations"""
    try:
        if not plc_client.get_connected():
            return {"error": "PLC not connected"}
        
        # Test values
        test_prox1 = 7500  # 7.5 seconds
        test_prox3 = 6000  # 6.0 seconds
        
        print(f"🔧 RAW TIMER TEST: Writing test values")
        print(f"   DB1.DBD12 (Prox1): {test_prox1}ms")
        print(f"   DB1.DBD16 (Prox3): {test_prox3}ms")
        
        # Write Prox1
        buffer1 = bytearray(4)
        snap7.util.set_dint(buffer1, 0, test_prox1)
        result1 = plc_client.db_write(PLC_DB_NUMBER, 12, buffer1)
        
        # Write Prox3
        buffer2 = bytearray(4)
        snap7.util.set_dint(buffer2, 0, test_prox3)
        result2 = plc_client.db_write(PLC_DB_NUMBER, 16, buffer2)
        
        # Read back
        time.sleep(0.1)
        verify_data = plc_client.db_read(PLC_DB_NUMBER, 12, 8)
        read_prox1 = snap7.util.get_dint(verify_data, 0)
        read_prox3 = snap7.util.get_dint(verify_data, 4)
        
        print(f"🔧 RAW TIMER TEST RESULTS:")
        print(f"   Prox1: wrote {test_prox1}, read {read_prox1}")
        print(f"   Prox3: wrote {test_prox3}, read {read_prox3}")
        
        return {
            "test_values": {"prox1": test_prox1, "prox3": test_prox3},
            "read_values": {"prox1": read_prox1, "prox3": read_prox3},
            "write_results": {"prox1": result1, "prox3": result2},
            "success": read_prox1 == test_prox1 and read_prox3 == test_prox3,
            "message": "Check console for detailed output"
        }
        
    except Exception as e:
        print(f"🔧 RAW TIMER TEST ERROR: {e}")
        return {"error": str(e)}

@app.get("/api/robot_state")
async def get_robot_state():
    refresh_arm_state_from_plc()
    robot_speed = get_robot_speed_from_file()
    any_arm_active = any([
        plc_vars_state.get('Arm_1', False),
        plc_vars_state.get('Arm_2', False),
        plc_vars_state.get('Arm_3', False)
    ])
    if robot_speed == 0 or not any_arm_active:
        overall_mode = "IDLE"
    elif robot_speed <= 50:  # ← NEW: 50% threshold
        overall_mode = "LOW_SPEED"   # ← NEW MODE
    else:
        overall_mode = "HIGH_SPEED"  # ← NEW MODE
    return {
        "current_speed": robot_speed,
        "overall_mode": overall_mode,
        "arms_active": {
            "arm_1": plc_vars_state.get('Arm_1', False),
            "arm_2": plc_vars_state.get('Arm_2', False),
            "arm_3": plc_vars_state.get('Arm_3', False)
        },
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/step_status")
async def get_step_status():
    """Get current step status for all arms"""
    try:
        return step_tracker.get_current_status()
    except Exception as e:
        print(f"Error getting step status: {e}")
        return {
            "arms": {
                "arm_1": {"name": "ERROR", "progress": 0, "start_time": None},
                "arm_2": {"name": "ERROR", "progress": 0, "start_time": None},
                "arm_3": {"name": "ERROR", "progress": 0, "start_time": None}
            },
            "last_updated": datetime.now().isoformat()
        }

@app.get("/api/step_history")
async def get_step_history():
    """Get step execution history"""
    return step_tracker.get_step_history()

@app.get("/api/step_summary")
async def get_step_summary():
    """Get summary of current operations"""
    try:
        status = step_tracker.get_current_status()
        active_arms = [f"arm_{i}" for i in [1, 2, 3] if status["arms"][f"arm_{i}"]["name"] != "IDLE"]
        total_progress = sum(status["arms"][f"arm_{i}"]["progress"] for i in [1, 2, 3]) / 3
        
        return {
            "active_arms": active_arms,
            "total_progress": total_progress,
            "current_operations": {
                f"arm_{i}": status["arms"][f"arm_{i}"]["name"] 
                for i in [1, 2, 3] if status["arms"][f"arm_{i}"]["name"] != "IDLE"
            }
        }
    except Exception as e:
        print(f"Error getting step summary: {e}")
        return {
            "active_arms": [],
            "total_progress": 0,
            "current_operations": {}
        }

@app.get("/api/intelligent_cost_analysis")
def get_intelligent_cost_analysis():
    """Get intelligent cost analysis with confidence levels"""
    try:
        return calculate_intelligent_cost_per_part()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/energy_breakdown_detailed")
def get_energy_breakdown_detailed():
    """Get detailed energy breakdown with confidence indicators"""
    try:
        energy_calc = intelligent_calculator.calculate_realistic_energy_per_cycle()
        return energy_calc
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/prox_sensors")
def get_prox_sensors():
    """Get current prox sensor states"""
    try:
        return read_prox_sensors_from_plc()
    except Exception as e:
        return {
            "Proximity_Sensor1": False,
            "Proximity_Sensor2": False,
            "Proximity_Sensor3": False,
            "Proximity_Sensor4": False,
            "error": str(e)
        }

@app.get("/api/step_status_with_prox")
async def get_step_status_with_prox():
    """Get step status including prox sensor information"""
    try:
        return step_tracker.get_prox_synchronized_status()
    except Exception as e:
        print(f"Error getting prox step status: {e}")
        return {"error": str(e)}

@app.get("/api/plant_workflow_status")
def get_plant_workflow_status():
    """Get real plant workflow status based on prox sensors"""
    try:
        prox_states = read_prox_sensors_from_plc()
        step_status = step_tracker.get_current_status()
        
        # Determine workflow stage based on prox sensors
        if prox_states.get("Proximity_Sensor4", False):
            stage = "PRODUCTION_COMPLETE"
        elif prox_states.get("Proximity_Sensor3", False):
            stage = "FINAL_TRANSPORT"
        elif prox_states.get("Proximity_Sensor2", False):
            stage = "ASSEMBLY_PHASE"
        elif prox_states.get("Proximity_Sensor1", False):
            stage = "TRANSPORT_TO_POINT_B"
        else:
            stage = "WAITING_FOR_HOLDER"
        
        return {
            "workflow_stage": stage,
            "prox_sensors": prox_states,
            "step_tracking": step_status,
            "conveyors": {
                "conveyor_1": plc_vars_state.get("Conveyor_1", False),
                "conveyor_2": plc_vars_state.get("Conveyor_2", False)
            },
            "production_count": plc_vars_state.get("Total_Prod", 0),
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/timer_status")
def get_timer_status():
    """Get current timer status"""
    timer_data = read_current_timers_from_plc()
    return timer_data

@app.post("/api/update_speed_and_timers")
def update_speed_and_timers_api(speed_data: dict):
    """Update robot speed and timers via API"""
    try:
        new_speed = speed_data.get("speed", 0)
        success = update_robot_speed_and_timers(new_speed)
        
        if success:
            timer_data = read_current_timers_from_plc()
            return {
                "message": f"Speed and timers updated to {new_speed}%",
                "timer_data": timer_data
            }
        else:
            return {"error": "Failed to update speed and timers"}
            
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/update_step")
async def update_step(arm: int, step_name: str, progress: int = 50):
    """Allow robot_controller.py to update step tracking"""
    try:
        step_tracker.set_step_by_name(arm, step_name, progress)
        return {"success": True, "message": f"Updated Arm {arm}: {step_name} ({progress}%)"}
    except Exception as e:
        print(f"Error updating step: {e}")
        return {"success": False, "error": str(e)}

@app.get("/api/assembly_status")
async def get_assembly_status():
    """Get detailed assembly line status based on actual PLC data"""
    try:
        refresh_all_states_from_plc()
        
        # Get current step status
        status = step_tracker.get_current_status()
        
        # Determine assembly stage based on actual PLC states
        arm1_active = plc_vars_state.get('Arm_1', False)
        conv1_active = plc_vars_state.get('Conveyor_1', False)
        arm2_active = plc_vars_state.get('Arm_2', False)
        arm3_active = plc_vars_state.get('Arm_3', False)
        conv2_active = plc_vars_state.get('Conveyor_2', False)
        
        if conv2_active:
            assembly_stage = "FINAL_TRANSPORT"
        elif arm2_active and arm3_active:
            assembly_stage = "PCB_ASSEMBLY"
        elif conv1_active:
            assembly_stage = "TRANSPORT_TO_POINT_B"
        elif arm1_active:
            assembly_stage = "HOLDER_PLACEMENT"
        else:
            assembly_stage = "IDLE"
        
        return {
            "assembly_stage": assembly_stage,
            "plc_states": {
                "arm_1": arm1_active,
                "arm_2": arm2_active,
                "arm_3": arm3_active,
                "conveyor_1": conv1_active,
                "conveyor_2": conv2_active
            },
            "step_tracking": status,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        print(f"Error getting assembly status: {e}")
        return {"error": str(e)}

# ============================================
# CONVERSATION HISTORY HELPER FUNCTIONS
# ============================================
def extract_pending_speed_from_history(conversation_history):
    """Extract pending speed change from conversation history"""
    if not conversation_history:
        return None
    
    # Look through recent messages (last 3 assistant messages)
    assistant_messages = [msg for msg in reversed(conversation_history) if msg.get('role') == 'assistant'][:3]
    
    for msg in assistant_messages:
        content = msg.get('content', '')
        
        # Pattern 1: "Would you like me to increase/decrease/change the speed to X%?"
        match = re.search(r'Would you like me to (?:increase|decrease|change) (?:the )?speed to (\d+)%', content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        
        # Pattern 2: "increase the speed to X%?"
        match = re.search(r'(?:increase|decrease|change) (?:the )?speed to (\d+)%\?', content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        
        # Pattern 3: "robot speed would change from X% to Y%"
        match = re.search(r'robot speed would change from \d+% to (\d+)%', content, re.IGNORECASE)
        if match:
            return int(match.group(1))
    
    return None

# ⭐ AI-POWERED ENDPOINTS WITH SYNC WRAPPERS
@app.get("/api/production_metrics")
def get_production_metrics():
    """AI-powered production metrics - synchronous"""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(get_production_time_breakdown_ai())
        loop.close()
        return {"ai_analysis": result}
    except Exception as e:
        # Fallback to regular calculation
        return calculate_production_metrics()

@app.get("/api/cost_analysis")
def get_cost_analysis():
    """AI-powered cost analysis - synchronous"""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(calculate_energy_cost_per_part_ai())
        loop.close()
        return {"ai_analysis": result}
    except Exception as e:
        # Fallback calculation
        return calculate_energy_cost_per_part()

@app.get("/api/bottleneck_analysis")
def get_bottleneck_analysis():
    """AI-powered bottleneck analysis - synchronous"""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(analyze_process_bottlenecks_ai())
        loop.close()
        return {"ai_analysis": result}
    except Exception as e:
        return analyze_process_bottlenecks()

@app.get("/api/maintenance_prediction")
def get_maintenance_prediction():
    """AI-powered maintenance prediction - synchronous"""
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(predict_maintenance_needs_ai())
        loop.close()
        return {"ai_analysis": result}
    except Exception as e:
        return predict_maintenance_needs()


@app.get("/api/intelligent_status")
def get_intelligent_status():
    """AI-enhanced status with predictions - synchronous"""
    # ⭐ SKIP AI - RETURN FALLBACK DIRECTLY
    return get_intelligent_status_summary()  # Remove the AI part

#@app.get("/api/intelligent_status")
#def get_intelligent_status():
    #"""AI-enhanced status with predictions - synchronous"""
    #try:
       # import asyncio
       # loop = asyncio.new_event_loop()
       # asyncio.set_event_loop(loop)
       # result = loop.run_until_complete(get_intelligent_status_summary_ai())
       # loop.close()
       # return result
    #except Exception as e:
        # Fallback to regular status
       # return get_intelligent_status_summary()

@app.post("/api/ai_speed_simulation")
async def simulate_ai_speed_change(speed_change: dict):
    """AI-powered speed simulation"""
    percentage = speed_change.get("percentage", 20)
    return await analyze_speed_increase_ai(percentage)



@app.get("/api/ai_production_metrics")
async def get_ai_production_metrics():
    """Get AI-powered production metrics"""
    return await get_production_time_breakdown_ai()

@app.get("/api/ai_cost_analysis")
async def get_ai_cost_analysis():
    """Get AI-powered cost analysis"""
    return await calculate_energy_cost_per_part_ai()

@app.get("/api/initial_greeting")
async def initial_greeting():
    """Initial greeting when page loads"""
    try:
        refresh_arm_state_from_plc()
        dynamic_context_str = get_plc_status_string()
        greeting = "Who are you?"
        
        ai_response = send_gpt_request_internal(greeting, dynamic_context_str)
        return {
            "user_message": greeting, 
            "ai_response": ai_response, 
            "plc_status": plc_vars_state
        }
    except Exception as e:
        print(f"Initial greeting error: {e}")
        # Return fallback response instead of failing
        return {
            "user_message": greeting,
            "ai_response": "Hello! I'm TIA S7 Copilot, ready to help with your manufacturing system. How can I assist you today?",
            "plc_status": plc_vars_state
        }

@app.get("/api/robot_running_status")
def get_robot_running_status():
    """Get robot running status from robot_controller.py"""
    try:
        # ⭐ OPTION A: Read from shared status file (RECOMMENDED)
        status_file = settings.data_path("robot_status.json")
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                robot_data = json.load(f)
                return robot_data
        
        # ⭐ FALLBACK: Use step tracking + PLC as proxy
        refresh_all_states_from_plc()
        step_status = step_tracker.get_current_status()
        
        robot_status = {}
        for i in [1, 2, 3]:
            arm_key = f'arm_{i}'
            
            # Enhanced detection logic
            plc_active = plc_vars_state.get(f'Arm_{i}', False)
            step_active = step_status['arms'][arm_key]['name'] != 'IDLE'
            has_progress = step_status['arms'][arm_key]['progress'] > 0
            
            # More accurate moving detection
            is_moving = step_active or has_progress or plc_active
            
            robot_status[arm_key] = {
                'is_moving': is_moving,
                'current_step': step_status['arms'][arm_key]['name'],
                'progress': step_status['arms'][arm_key]['progress'],
                'plc_trigger': plc_active,
                'status': 'MOVING' if is_moving else 'STOPPED'
            }
        
        return {
            'robots': robot_status,
            'system_active': any(robot_status[f'arm_{i}']['is_moving'] for i in [1,2,3]),
            'timestamp': datetime.now().isoformat(),
            'source': 'step_tracking_fallback'
        }
        
    except Exception as e:
        print(f"Error getting robot status: {e}")
        # Ultimate fallback
        return {
            'robots': {
                'arm_1': {'is_moving': False, 'current_step': 'IDLE', 'progress': 0, 'status': 'STOPPED'},
                'arm_2': {'is_moving': False, 'current_step': 'IDLE', 'progress': 0, 'status': 'STOPPED'},
                'arm_3': {'is_moving': False, 'current_step': 'IDLE', 'progress': 0, 'status': 'STOPPED'}
            },
            'system_active': False,
            'timestamp': datetime.now().isoformat(),
            'source': 'fallback'
        }

@app.get("/api/cycle_timing")
def get_cycle_timing():
    """Get current cycle timing data"""
    try:
        if os.path.exists(CYCLE_TIMING_FILE):
            with open(CYCLE_TIMING_FILE, 'r') as f:
                timing_data = json.load(f)
                return timing_data
        else:
            return {"message": "No cycle timing data available"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/dynamic_production_rate")
def get_dynamic_production_rate():
    """Get production rate based on current speed"""
    metrics = calculate_production_metrics()
    return {
        "current_speed": metrics['current_speed_percentage'],
        "cycle_time": metrics['measured_cycle_time'],
        "parts_per_hour": metrics['parts_per_hour'],
        "daily_output": metrics['parts_per_8_hour_shift'],
        "speed_impact": metrics['speed_impact']
    }

# ============================================
# TIA OPENNESS API ENDPOINTS
# ============================================

@app.get("/api/tia/status")
def get_tia_status():
    """Get TIA Intelligence module status"""
    if not TIA_INTEGRATION_AVAILABLE:
        return {
            "available": False,
            "connected": False,
            "message": "TIA Integration modules not loaded"
        }
    
    if not tia:
        return {
            "available": True,
            "connected": False,
            "message": "TIA Intelligence not initialized"
        }
    
    try:
        status = tia.get_status()
        return {
            "available": True,
            "connected": status['connected'],
            "details": status
        }
    except Exception as e:
        return {
            "available": True,
            "connected": False,
            "error": str(e)
        }

@app.get("/api/tia/tags")
def get_tia_tags():
    """Get all PLC tags from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        tags = tia.get_all_tags()
        if "error" in tags:
            raise HTTPException(status_code=500, detail=tags["error"])
        return tags
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/tags/search")
def search_tia_tags(q: str = Query(..., description="Search term")):
    """Search PLC tags"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        results = tia.search_tags(q)
        if "error" in results:
            raise HTTPException(status_code=500, detail=results["error"])
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/blocks")
def get_tia_blocks():
    """Get all PLC blocks from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        blocks = tia.get_all_blocks()
        if "error" in blocks:
            raise HTTPException(status_code=500, detail=blocks["error"])
        return blocks
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/hardware")
def get_tia_hardware():
    """Get hardware configuration from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        hardware = tia.get_hardware_configuration()
        if "error" in hardware:
            raise HTTPException(status_code=500, detail=hardware["error"])
        return hardware
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/ip_address")
def get_tia_ip_address():
    """Get PLC IP address from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        ip_data = tia.get_plc_ip_address()
        if "error" in ip_data:
            raise HTTPException(status_code=500, detail=ip_data["error"])
        return ip_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/program_structure")
def get_tia_program_structure():
    """Get program structure from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        structure = tia.get_program_structure()
        if "error" in structure:
            raise HTTPException(status_code=500, detail=structure["error"])
        return structure
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tia/refresh")
def refresh_tia_data():
    """Force refresh all TIA Portal data"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        result = tia.refresh_all_data()
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/project_info")
def get_tia_project_info():
    """Get TIA project information"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        info = tia.get_project_info()
        if "error" in info:
            raise HTTPException(status_code=500, detail=info["error"])
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tia/health")
def get_tia_program_health():
    """Get program health analysis from TIA Portal"""
    if not tia_data_available:
        raise HTTPException(status_code=503, detail="TIA Portal not connected")
    
    try:
        health = tia.analyze_program_health()
        if "error" in health:
            raise HTTPException(status_code=500, detail=health["error"])
        return health
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================
# VOICE ASSISTANT API - AUTO-START
# ============================================
try:
    from .voice_assistant import start_voice_assistant, get_voice_assistant
    VOICE_ASSISTANT_AVAILABLE = True
    print("✅ Voice assistant module loaded successfully")
except ImportError as e:
    VOICE_ASSISTANT_AVAILABLE = False
    print(f"⚠️ Voice assistant not available: {e}")
    print("   Install with: pip install -r requirements-voice.txt")
except Exception as e:
    VOICE_ASSISTANT_AVAILABLE = False
    print(f"❌ Error loading voice assistant: {e}")

@app.get("/api/voice/status")
async def get_voice_status():
    """Get current voice assistant status"""
    if not VOICE_ASSISTANT_AVAILABLE:
        return {"active": False, "error": "Voice assistant not installed"}
    
    try:
        # Read status from file
        voice_status_file = settings.data_path("voice_status.json")
        if os.path.exists(voice_status_file):
            with open(voice_status_file, 'r') as f:
                status = json.load(f)
                return status
        return {"active": False, "listening": False}
    except Exception as e:
        return {"active": False, "error": str(e)}

# ⭐ STATIC FILES
app.mount("/", StaticFiles(directory=settings.WEB_DIR, html=True), name="static")

if __name__ == "__main__":
    print("🚀 Starting FastAPI server...")
    
    # ⭐ AUTO-START VOICE ASSISTANT
    if VOICE_ASSISTANT_AVAILABLE:
        print("🎤 Starting voice assistant in background...")
        try:
            start_voice_assistant()
            print("✅ Voice assistant active! Say 'copilot' to give commands.")
        except Exception as e:
            print(f"⚠️ Could not start voice assistant: {e}")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
