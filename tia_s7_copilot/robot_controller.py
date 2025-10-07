"""TIA S7 Copilot - robot controller service (port 8001).

Polls the PLC data block every 100 ms, runs the uArm Swift Pro motion sequences in
worker threads (position-triggered hand-over between arms), enforces the emergency
stop and publishes robot status / cycle timing to the shared data directory.

Run with:  python -m tia_s7_copilot.robot_controller
"""

import time
import threading
import requests
import snap7
import snap7.util
import json
import os
import math
from uarm.wrapper import SwiftAPI
from . import settings
from threading import Lock
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Create FastAPI app
app = FastAPI(title="TIA S7 Copilot robot controller")

# Add CORS if needed
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ⭐ SPEED CONFIGURATION - PERCENTAGE TO ACTUAL SPEED MAPPING
MAX_MOVE_SPEED = 8000000      # 100% speed for move_arm
MAX_CATCH_SPEED = 800000      # 100% speed for move_arm_catch  
MIN_SPEED = 100000           # 0% speed (minimum safe speed)

# ⭐ SPEED CONTROL VIA SHARED FILE
SPEED_FILE = settings.data_path("robot_speed.json")
current_speed_percentage = 50
speed_lock = threading.Lock()

# ⭐ PLC CONNECTION (address from settings / .env)
plc = snap7.client.Client()
plc.connect(settings.PLC_IP, settings.PLC_RACK, settings.PLC_SLOT)  # IP address, rack, slot

# YOUR ORIGINAL PLC SETTINGS
DB_NUMBER = 1
START_BYTE = 10
SIZE = 1  # Read only one byte since arm_1 to arm_3 are in byte 10

# Initialize uArm robots (serial ports from settings / .env)
arm_1 = SwiftAPI(port=settings.ROBOT_ARM_PORTS["arm_1"])
arm_2 = SwiftAPI(port=settings.ROBOT_ARM_PORTS["arm_2"])
arm_3 = SwiftAPI(port=settings.ROBOT_ARM_PORTS["arm_3"])

# Wait for devices to be ready
for swift in [arm_1, arm_2, arm_3]:
    swift.waiting_ready()
    device_info = swift.get_device_info()
    print(swift.port, device_info)
    firmware_version = device_info['firmware_version']
    if firmware_version and not firmware_version.startswith(('0.', '1.', '2.', '3.')):
        swift.set_speed_factor(0.00001)
        
arm_list = [arm_1, arm_2, arm_3]

# ⭐ ROBOT STATUS TRACKING
robot_status = {
    'arm_1': {'is_moving': False, 'last_update': time.time(), 'current_step': 'IDLE'},
    'arm_2': {'is_moving': False, 'last_update': time.time(), 'current_step': 'IDLE'},
    'arm_3': {'is_moving': False, 'last_update': time.time(), 'current_step': 'IDLE'}
}
status_lock = threading.Lock()

# ⭐ CYCLE TRACKING
arm_3_sequence_counter = 0  # 0 = first time, 1 = second time
arm_1_sequence_counter = 0  # 0 = sequence 1, 1 = sequence 2 (added to fix alternation)

# ⭐ ADD CYCLE TIMING VARIABLES
cycle_start_time = None
cycle_times_history = []

# ⭐ ADD EMERGENCY STOP SYSTEM
emergency_stop_active = False
stop_lock = threading.Lock()

# ⭐ ADD SYSTEM ENABLE FLAG - ROBOTS WON'T MOVE UNTIL EXPLICITLY STARTED
system_enabled = False  # ⭐ NEW: Prevents auto-start on application launch
system_lock = threading.Lock()


def enable_system():
    """Enable the robot system - allows robots to respond to PLC triggers"""
    global system_enabled
    with system_lock:
        system_enabled = True
        print("✅ ROBOT SYSTEM ENABLED - Robots can now respond to commands")

def disable_system():
    """Disable the robot system - robots will ignore PLC triggers"""
    global system_enabled
    with system_lock:
        system_enabled = False
        print("🛑 ROBOT SYSTEM DISABLED - Robots will ignore commands")

def is_system_enabled():
    """Check if system is enabled"""
    with system_lock:
        return system_enabled



def activate_emergency_stop():
    """Activate emergency stop - freeze all robots immediately"""
    global emergency_stop_active
    with stop_lock:
        emergency_stop_active = True
        print("🛑 EMERGENCY STOP ACTIVATED - All robots frozen!")
        
        # Stop all robots immediately - HARD STOP
        try:
            for i, swift in enumerate([arm_1, arm_2, arm_3], 1):
                # Clear command queue AND wait for robot to stop moving (immediate stop)
                swift.flush_cmd(wait_stop=True, timeout=0.1)
                # Deactivate all tools immediately
                swift.set_pump(False)
                swift.set_gripper(False)
                set_robot_stopped(i, "EMERGENCY_STOP")
                update_step_api(i, "EMERGENCY_STOP", 0)
            print("🛑 All robots HARD STOPPED - movements halted, tools deactivated")
        except Exception as e:
            print(f"Error during emergency stop: {e}")

def deactivate_emergency_stop():
    """Deactivate emergency stop - allow robots to move again"""
    global emergency_stop_active
    with stop_lock:
        emergency_stop_active = False
        print("✅ Emergency stop deactivated - robots can move again")
        
        # Reset all robots to idle (but don't call reset_all_coordination_flags to avoid conflicts)
        for i in [1, 2, 3]:
            set_robot_stopped(i, "IDLE")
            update_step_api(i, "IDLE", 0)

def check_emergency_stop():
    """Check if emergency stop is active"""
    with stop_lock:
        return emergency_stop_active

def emergency_stop_wrapper(func):
    """Decorator to check emergency stop before robot movements"""
    def wrapper(*args, **kwargs):
        if check_emergency_stop():
            print("🛑 Movement blocked - Emergency stop active!")
            return False
        return func(*args, **kwargs)
    return wrapper

def start_cycle_timing():
    """Start timing a new cycle"""
    global cycle_start_time
    cycle_start_time = time.time()
    print(f"⏱️ Cycle timing started at {time.strftime('%H:%M:%S')}")

def end_cycle_timing():
    """End cycle timing and save result"""
    global cycle_start_time, cycle_times_history
    if cycle_start_time:
        cycle_duration = time.time() - cycle_start_time
        cycle_times_history.append({
            'duration': cycle_duration,
            'timestamp': time.time(),
            'speed_percentage': get_speed_percentage()
        })
        
        # Keep only last 20 measurements
        if len(cycle_times_history) > 20:
            cycle_times_history = cycle_times_history[-20:]
        
        print(f"⏱️ Cycle completed in {cycle_duration:.1f}s at {get_speed_percentage()}% speed")
        
        # Save to file for api.py
        save_cycle_timing_to_file(cycle_duration)
        cycle_start_time = None

def save_cycle_timing_to_file(duration):
    """Save cycle timing for api.py to use"""
    try:
        timing_data = {
            'last_cycle_duration': duration,
            'speed_percentage': get_speed_percentage(),
            'timestamp': time.time(),
            'cycle_history': cycle_times_history[-10:]  # Last 10 cycles
        }
        with open(settings.data_path("cycle_timing.json"), 'w') as f:
            json.dump(timing_data, f, indent=2)
    except Exception as e:
        print(f"Error saving cycle timing: {e}")

# ⭐ POSITION MONITORING AND PLC INTEGRATION
def parse_position(position_data):
    """Parse position data regardless of format"""
    if position_data is None:
        return None
    
    if isinstance(position_data, dict):
        return position_data
    elif isinstance(position_data, (list, tuple)) and len(position_data) >= 3:
        return {'x': position_data[0], 'y': position_data[1], 'z': position_data[2]}
    else:
        print(f"Unexpected position format: {position_data}")
        return None

def check_position_reached(current_pos, target_pos, tolerance=10.0):
    """Check if robot reached target within tolerance"""
    if current_pos is None:
        return False
    
    parsed_pos = parse_position(current_pos)
    if parsed_pos is None:
        return False
    
    x_diff = abs(parsed_pos['x'] - target_pos['x'])
    y_diff = abs(parsed_pos['y'] - target_pos['y'])
    z_diff = abs(parsed_pos['z'] - target_pos['z'])
    
    # 10% tolerance calculation
    x_tolerance = max(tolerance, abs(target_pos['x']) * 0.1)
    y_tolerance = max(tolerance, abs(target_pos['y']) * 0.1)
    z_tolerance = max(tolerance, abs(target_pos['z']) * 0.1)
    
    position_reached = (x_diff <= x_tolerance and 
                       y_diff <= y_tolerance and 
                       z_diff <= z_tolerance)
    
    if position_reached:
        print(f"✅ Position reached! Target: {target_pos}, Current: {parsed_pos}")
        print(f"   Tolerances - X: {x_diff:.1f}≤{x_tolerance:.1f}, Y: {y_diff:.1f}≤{y_tolerance:.1f}, Z: {z_diff:.1f}≤{z_tolerance:.1f}")
    
    return position_reached

# ⭐ UPDATED COORDINATION TRIGGER POSITIONS
COORDINATION_TRIGGERS = {
    'arm_2_position4_complete': {'x': 190, 'y': 210, 'z': 120},    # ⭐ NEW: Triggers Arm 3 (earlier)
    'arm_3_position8_2_complete': {'x': 165, 'y': -5, 'z': 180},  # Triggers Arm 2 Seq 2
}

# ⭐ UPDATED COORDINATION FLAGS
arm_2_reached_position4 = False  # NEW FLAG
arm_3_reached_position8_2 = False
arm_2_sequence_2_pending = False

# ⭐ YOUR ORIGINAL PLC READING FUNCTION - RESTORED
def read_plc_data():
    """YOUR ORIGINAL PLC READING FUNCTION"""
    try:
        DB_bytearray = plc.db_read(DB_NUMBER, START_BYTE, SIZE)
        
        # YOUR ORIGINAL BIT ASSIGNMENTS (all in byte 10)
        arm_1_PLC = snap7.util.get_bool(DB_bytearray, 0, 0)  # DB1.DBX10.0
        arm_2_PLC = snap7.util.get_bool(DB_bytearray, 0, 1)  # DB1.DBX10.1  
        arm_3_PLC = snap7.util.get_bool(DB_bytearray, 0, 2)  # DB1.DBX10.2
        
        return arm_1_PLC, arm_2_PLC, arm_3_PLC
        
    except Exception as e:
        print(f"❌ Error reading PLC: {e}")
        return False, False, False

# ⭐ NEW FUNCTION TO READ STOP BIT FROM PLC
def read_plc_stop_data():
    """Read stop bit from PLC"""
    try:
        stop_data = plc.db_read(DB_NUMBER, 0, 1)  # Read byte 0 for Stop bit
        stop_PLC = snap7.util.get_bool(stop_data, 0, 1)  # DB1.DBX0.1 (Stop bit)
        return stop_PLC
    except Exception as e:
        print(f"❌ Error reading PLC stop data: {e}")
        return False

# ⭐ SHARED FILE SPEED CONTROL FUNCTIONS
def load_speed_from_file():
    """Load speed from shared file"""
    global current_speed_percentage
    try:
        if os.path.exists(SPEED_FILE):
            with open(SPEED_FILE, 'r') as f:
                data = json.load(f)
                with speed_lock:
                    new_speed = max(0, min(100, data.get('speed_percentage', 50)))
                    if new_speed != current_speed_percentage:
                        print(f"📁 Speed updated from file: {current_speed_percentage}% → {new_speed}%")
                        current_speed_percentage = new_speed
    except Exception as e:
        print(f"❌ Error loading speed from file: {e}")

def save_speed_to_file():
    """Save current speed to shared file"""
    try:
        with speed_lock:
            data = {
                'speed_percentage': current_speed_percentage,
                'timestamp': time.time(),
                'move_speed': percentage_to_move_speed(current_speed_percentage),
                'catch_speed': percentage_to_catch_speed(current_speed_percentage),
                'updated_by': 'robot_arm_run.py'
            }
        with open(SPEED_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"❌ Error saving speed to file: {e}")

def get_speed_percentage():
    """Get current robot speed percentage (checks file for updates)"""
    load_speed_from_file()  # Check for updates from api.py
    with speed_lock:
        return current_speed_percentage

# Load initial speed from file
load_speed_from_file()
save_speed_to_file()  # Create file if it doesn't exist

# ⭐ STATUS FUNCTIONS
def set_robot_moving(arm_id, step_name="MOVING"):
    """Set robot as moving with current step"""
    with status_lock:
        robot_status[f'arm_{arm_id}']['is_moving'] = True
        robot_status[f'arm_{arm_id}']['last_update'] = time.time()
        robot_status[f'arm_{arm_id}']['current_step'] = step_name
    
    save_robot_status_to_file()

def set_robot_stopped(arm_id, step_name="IDLE"):
    """Set robot as stopped"""
    with status_lock:
        robot_status[f'arm_{arm_id}']['is_moving'] = False
        robot_status[f'arm_{arm_id}']['last_update'] = time.time()
        robot_status[f'arm_{arm_id}']['current_step'] = step_name
    
    save_robot_status_to_file()

def get_all_robots_status_summary():
    """Get summary status for all robots"""
    with status_lock:
        return {
            'arm_1_moving': robot_status['arm_1']['is_moving'],
            'arm_2_moving': robot_status['arm_2']['is_moving'],
            'arm_3_moving': robot_status['arm_3']['is_moving'],
            'arm_1_step': robot_status['arm_1']['current_step'],
            'arm_2_step': robot_status['arm_2']['current_step'],
            'arm_3_step': robot_status['arm_3']['current_step'],
            'any_moving': any([robot_status['arm_1']['is_moving'], 
                              robot_status['arm_2']['is_moving'], 
                              robot_status['arm_3']['is_moving']])
        }

def save_robot_status_to_file():
    """Save robot status to shared file for api.py"""
    try:
        status_data = {
            'robots': {
                f'arm_{i}': {
                    'is_moving': robot_status[f'arm_{i}']['is_moving'],
                    'current_step': robot_status[f'arm_{i}']['current_step'],
                    'progress': 0,
                    'status': 'MOVING' if robot_status[f'arm_{i}']['is_moving'] else 'STOPPED'
                } for i in [1, 2, 3]
            },
            'system_active': any(robot_status[f'arm_{i}']['is_moving'] for i in [1,2,3]),
            'timestamp': time.time(),
            'source': 'robot_arm_run.py'
        }
        
        with open(settings.data_path("robot_status.json"), 'w') as f:
            json.dump(status_data, f, indent=2)
            
    except Exception as e:
        print(f"Error saving robot status: {e}")

# ⭐ STATUS DISPLAY THREAD
def status_display():
    """Display continuous status updates"""
    last_display = {}
    
    while True:
        current_status = get_all_robots_status_summary()
        current_speed = get_speed_percentage()
        
        current_status['speed'] = current_speed
        current_status['emergency_stop'] = check_emergency_stop()  # ⭐ ADD EMERGENCY STOP STATUS
        
        if current_status != last_display:
            timestamp = time.strftime('%H:%M:%S')
            print(f"\n[{timestamp}] ROBOT STATUS:")
            print(f"  ARM 1: {'MOVING' if current_status['arm_1_moving'] else 'STOPPED'} - {current_status['arm_1_step']}")
            print(f"  ARM 2: {'MOVING' if current_status['arm_2_moving'] else 'STOPPED'} - {current_status['arm_2_step']}")
            print(f"  ARM 3: {'MOVING' if current_status['arm_3_moving'] else 'STOPPED'} - {current_status['arm_3_step']}")
            print(f"  SYSTEM: {'ACTIVE' if current_status['any_moving'] else 'IDLE'}")
            print(f"  SPEED: {current_speed}%")
            if current_status['emergency_stop']:  # ⭐ SHOW EMERGENCY STOP STATUS
                print(f"  🛑 EMERGENCY STOP: ACTIVE")
            last_display = current_status.copy()
        
        time.sleep(1.0)

# Start status display thread
threading.Thread(target=status_display, daemon=True).start()

# ⭐ PERCENTAGE TO SPEED CONVERSION FUNCTIONS
def percentage_to_move_speed(percentage):
    """Convert 0-100% to actual move speed (100000 to 8000000)"""
    if percentage <= 0:
        return MIN_SPEED
    if percentage >= 100:
        return MAX_MOVE_SPEED
    
    speed_range = MAX_MOVE_SPEED - MIN_SPEED
    actual_speed = MIN_SPEED + (speed_range * (percentage / 100.0))
    return int(actual_speed)

def percentage_to_catch_speed(percentage):
    """Convert 0-100% to actual catch speed (100000 to 800000)"""
    if percentage <= 0:
        return MIN_SPEED
    if percentage >= 100:
        return MAX_CATCH_SPEED
    
    speed_range = MAX_CATCH_SPEED - MIN_SPEED
    actual_speed = MIN_SPEED + (speed_range * (percentage / 100.0))
    return int(actual_speed)

def reset_all_coordination_flags():
    """Reset all coordination and state flags"""
    global arm_2_reached_position4, arm_3_reached_position8_2, arm_2_sequence_2_pending
    global arm_1_busy, arm_2_busy, arm_3_busy, arm_3_sequence_counter, arm_1_sequence_counter
    
    print("🔄 Resetting all coordination flags...")
    
    # Reset coordination flags
    arm_2_reached_position4 = False
    arm_3_reached_position8_2 = False
    arm_2_sequence_2_pending = False
    
    # Reset busy flags
    arm_1_busy = False
    arm_2_busy = False
    arm_3_busy = False
    
    # Reset sequence counters (fresh start after STOP)
    arm_3_sequence_counter = 0
    arm_1_sequence_counter = 0
    
    print("✅ All coordination flags reset (counters reset to 0)")

def force_stop_all_sequences():
    """Force stop all running sequences and clear command queues"""
    global arm_1_busy, arm_2_busy, arm_3_busy
    
    print("🛑 Force stopping all sequences...")
    
    try:
        # Clear all robot command queues
        for swift in [arm_1, arm_2, arm_3]:
            swift.flush_cmd()
        
        # ⭐ ENHANCED: Deactivate all pumps and grippers
        print("🛑 Deactivating all pumps and grippers during force stop...")
        arm_1.set_pump(on=False)
        arm_2.set_gripper(catch=False)
        arm_3.set_pump(on=False)
        
        # Force set all arms to not busy
        arm_1_busy = False
        arm_2_busy = False
        arm_3_busy = False
        
        # Update step tracking
        for i in [1, 2, 3]:
            set_robot_stopped(i, "FORCE_STOPPED")
            update_step_api(i, "IDLE", 0)
        
        print("✅ All sequences force stopped and pumps/grippers deactivated")
        
    except Exception as e:
        print(f"❌ Error during force stop: {e}")

def complete_system_reset():
    """Complete system reset - clears everything"""
    print("🔄 COMPLETE SYSTEM RESET INITIATED...")
    
    # 1. Force stop everything first
    force_stop_all_sequences()
    
    # 2. Clear emergency stop WITHOUT calling deactivate_emergency_stop() to avoid recursion
    global emergency_stop_active
    with stop_lock:
        emergency_stop_active = False
        print("✅ Emergency stop cleared during system reset")
    
    # 3. Reset coordination flags directly
    reset_all_coordination_flags()
    
    # 4. Reset cycle timing
    global cycle_start_time
    cycle_start_time = None
    
    # 5. Update all step tracking
    for i in [1, 2, 3]:
        set_robot_stopped(i, "IDLE")
        update_step_api(i, "IDLE", 0)
    
    print("✅ COMPLETE SYSTEM RESET FINISHED - Ready for new sequences!")

# ⭐ UPDATED COORDINATION LOGIC
def check_and_trigger_coordination(swift, arm_id, target_pos):
    """Enhanced coordination - Arm 3 starts at position4, Arm 2 Seq 2 starts at position8_2"""
    global arm_3_busy, arm_2_sequence_2_pending, arm_2_reached_position4, arm_3_reached_position8_2, arm_3_sequence_counter
    
    # ⭐ CHECK EMERGENCY STOP BEFORE COORDINATION
    if check_emergency_stop():
        return
    
    try:
        current_pos = swift.get_position()
        
        # ⭐ ARM 2 POSITION4 TRIGGER - Start Arm 3 (EARLIER TRIGGER!)
        if (arm_id == 2 and 
            check_position_reached(current_pos, COORDINATION_TRIGGERS['arm_2_position4_complete'], tolerance=10.0)):
            
            print(f"🚀 POSITION TRIGGER: Arm 2 reached position4 - starting Arm 3!")
            print(f"   Current Pos: {current_pos}")
            print(f"   Target Pos: {COORDINATION_TRIGGERS['arm_2_position4_complete']}")
            
            arm_2_reached_position4 = True
            
            if not arm_3_busy and not check_emergency_stop():  # ⭐ CHECK EMERGENCY STOP
                # ⭐ CYCLE-BASED ARM 3 SEQUENCE SELECTION
                global arm_3_sequence_counter
                
                if arm_3_sequence_counter == 0:
                    print(f"🎯 Starting Arm 3 Sequence 1 (First Cycle)...")
                    arm_3_sequence_counter = 1  # Next time will be sequence 2
                    threading.Thread(target=arm_3_sequence_1_wrapper).start()
                else:
                    print(f"🎯 Starting Arm 3 Sequence 2 (Second Cycle)...")
                    arm_3_sequence_counter = 0  # Reset for next round
                    threading.Thread(target=arm_3_sequence_2_wrapper).start()
                
            # Mark that Arm 2 Seq 2 is pending (but don't start yet!)
            arm_2_sequence_2_pending = True
            print(f"⏳ Arm 2 Sequence 2 marked as PENDING - waiting for Arm 3 position8_2...")
        
        # ⭐ ARM 3 POSITION8_2 TRIGGER - Enable Arm 2 Seq 2
        elif (arm_id == 3 and 
              check_position_reached(current_pos, COORDINATION_TRIGGERS['arm_3_position8_2_complete'], tolerance=10.0)):
            
            print(f"✅ POSITION TRIGGER: Arm 3 reached position8_2 - enabling Arm 2 Seq 2!")
            print(f"   Current Pos: {current_pos}")
            print(f"   Target Pos: {COORDINATION_TRIGGERS['arm_3_position8_2_complete']}")
            
            arm_3_reached_position8_2 = True
            
            # ⭐ START ARM 2 SEQ 2 IF IT'S PENDING AND ARM 2 IS NOT BUSY
            if arm_2_sequence_2_pending and not arm_2_busy and not check_emergency_stop():  # ⭐ CHECK EMERGENCY STOP
                print(f"🚀 Starting Arm 2 Sequence 2 now that Arm 3 reached position8_2!")
                arm_2_sequence_2_pending = False
                arm_3_reached_position8_2 = False
                arm_2_reached_position4 = False  # Reset flag
                threading.Thread(target=arm_2_sequence_2_wrapper).start()
            else:
                print(f"⚠️ Arm 2 Seq 2 not ready: Pending={arm_2_sequence_2_pending}, Arm2_Busy={arm_2_busy}")
                
    except Exception as e:
        print(f"⚠️ Position trigger check error: {e}")

# ⭐ ENHANCED MOVE FUNCTIONS WITH POSITION MONITORING AND EMERGENCY STOP
@emergency_stop_wrapper
def move_arm_dynamic_with_triggers(swift, positions, speed_percentage, arm_id, wait=True, timeout=None):
    """Move arm with position monitoring for coordination triggers"""
    actual_speed = percentage_to_move_speed(speed_percentage)
    print(f"🏃 Arm {arm_id} Move Speed: {speed_percentage}% = {actual_speed}")
    set_robot_moving(arm_id, "MOVING")
    
    for i, pos in enumerate(positions):
        if check_emergency_stop():  # ⭐ CHECK BEFORE EACH POSITION
            print(f"🛑 Arm {arm_id} movement stopped - Emergency stop!")
            return False
            
        swift.set_position(**pos, speed=actual_speed, wait=wait, timeout=timeout)
        
        # ⭐ CHECK FOR COORDINATION TRIGGERS AFTER EACH POSITION
        if arm_id == 2 or arm_id == 3:  # Monitor both Arm 2 and Arm 3 for triggers
            check_and_trigger_coordination(swift, arm_id, pos)
        
        if wait:
            time.sleep(0.5)  # Small delay for movement completion
    
    return True

@emergency_stop_wrapper
def move_arm_catch_dynamic_with_triggers(swift, positions, speed_percentage, arm_id, wait=True, timeout=None):
    """Catch move with position monitoring"""
    actual_speed = percentage_to_catch_speed(speed_percentage)
    print(f"🤏 Arm {arm_id} Catch Speed: {speed_percentage}% = {actual_speed}")
    set_robot_moving(arm_id, "CATCHING")
    
    for i, pos in enumerate(positions):
        if check_emergency_stop():  # ⭐ CHECK BEFORE EACH POSITION
            print(f"🛑 Arm {arm_id} catch movement stopped - Emergency stop!")
            return False
            
        swift.set_position(**pos, speed=actual_speed, wait=wait, timeout=timeout)
        
        # ⭐ CHECK FOR COORDINATION TRIGGERS
        if arm_id == 2 or arm_id == 3:  # Monitor both arms
            check_and_trigger_coordination(swift, arm_id, pos)
        
        if wait:
            time.sleep(0.3)  # Smaller delay for catch movements
    
    return True

@emergency_stop_wrapper
def move_arm_dynamic(swift, positions, speed_percentage, arm_id, wait=True, timeout=None):
    """Standard move arm with dynamic speed (no position triggers)"""
    actual_speed = percentage_to_move_speed(speed_percentage)
    print(f"🏃 Arm {arm_id} Move Speed: {speed_percentage}% = {actual_speed}")
    set_robot_moving(arm_id, "MOVING")
    for pos in positions:
        if check_emergency_stop():  # ⭐ CHECK BEFORE EACH POSITION
            print(f"🛑 Arm {arm_id} movement stopped - Emergency stop!")
            return False
        swift.set_position(**pos, speed=actual_speed, wait=wait, timeout=timeout)

@emergency_stop_wrapper
def move_arm_catch_dynamic(swift, positions, speed_percentage, arm_id, wait=True, timeout=None):
    """Standard catch move with dynamic speed (no position triggers)"""
    actual_speed = percentage_to_catch_speed(speed_percentage)
    print(f"🤏 Arm {arm_id} Catch Speed: {speed_percentage}% = {actual_speed}")
    set_robot_moving(arm_id, "CATCHING")
    for pos in positions:
        if check_emergency_stop():  # ⭐ CHECK BEFORE EACH POSITION
            print(f"🛑 Arm {arm_id} catch movement stopped - Emergency stop!")
            return False
        swift.set_position(**pos, speed=actual_speed, wait=wait, timeout=timeout)

# ⭐ API INTEGRATION
def update_step_api(arm_number, step_name, progress=50):
    """Send step update to api.py via API"""
    try:
        if progress > 0 and progress < 100:
            set_robot_moving(arm_number, step_name)
        else:
            set_robot_stopped(arm_number, step_name)
            
        response = requests.post(
            "http://localhost:8000/api/update_step",
            params={"arm": arm_number, "step_name": step_name, "progress": progress},
            timeout=1
        )
        if response.status_code == 200:
            print(f"✅ Step updated: Arm {arm_number} - {step_name} ({progress}%)")
        else:
            print(f"❌ Step update failed: {response.text}")
    except Exception as e:
        print(f"⚠️ Step update error (continuing): {e}")

# ⭐ POSITION DEFINITIONS
# ARM 1 POSITIONS
arm_1_origin = [{'x': 180, 'y': 0, 'z': 0}]
arm_1_position1 = [{'x': 180, 'y': -40, 'z': -80}]
arm_1_position2 = [{'x': 180, 'y': -40, 'z': -114}]
arm_1_position3 = [{'x': 180, 'y': -40, 'z': -50}]
arm_1_position3_2 = [{'x': 180, 'y': -40, 'z': 200}]
arm_1_position4 = [{'x': 22, 'y': -215, 'z': 185}]
arm_1_position5 = [{'x': 50, 'y': -204, 'z': 100}]
arm_1_position6 = [{'x': 50, 'y': -204, 'z': 70}]

# ARM 1 SEQUENCE 2 POSITIONS
arm_1_seq2_position1 = [{'x': 180, 'y': 60, 'z': -80}]
arm_1_seq2_position2 = [{'x': 180, 'y': 60, 'z': -114}]
arm_1_seq2_position3 = [{'x': 180, 'y': 60, 'z': -50}]
arm_1_seq2_position3_2 = [{'x': 180, 'y': 60, 'z': 200}]

# ARM 2 POSITIONS
arm_2_position1 = [{'x': 160, 'y': 0, 'z': 80}]
arm_2_position2 = [{'x': 80, 'y': 210, 'z': 60}]
arm_2_position2_2 = [{'x': 80, 'y': 210, 'z': 43}]
arm_2_position3 = [{'x': 5, 'y': 190, 'z': 40}]
arm_2_position3_2 = [{'x': 5, 'y': 200, 'z': 100}]
arm_2_position4 = [{'x': 190, 'y': 210, 'z': 120}]  # ⭐ NEW TRIGGER POSITION FOR ARM 3
arm_2_position5 = [{'x': 160, 'y': 65, 'z': 200}]
arm_2_position6 = [{'x': 210, 'y': 85, 'z': -20}]
arm_2_position7 = [{'x': 180, 'y': 72, 'z': -40}]

# ARM 2 SEQUENCE 2 POSITIONS
arm_2_seq2_position1 = [{'x': 160, 'y': 0, 'z': 100}]
arm_2_seq2_position2 = [{'x': 70, 'y': -200, 'z': 95}]
arm_2_seq2_position3 = [{'x': 20, 'y': -205, 'z': 50}]
arm_2_seq2_position3_2 = [{'x': 50, 'y': -205, 'z': 55}]

# ARM 3 POSITIONS
arm_3_position1 = [{'x': 130, 'y': 0, 'z': 120}]
arm_3_position2 = [{'x': 205, 'y': -30, 'z': 120}]
arm_3_position3 = [{'x': 216, 'y': -38, 'z': 50}]
arm_3_position4 = [{'x': 216, 'y': -38, 'z': 35}]
arm_3_position5 = [{'x': 273, 'y': -150, 'z': 180}]
arm_3_position6 = [{'x': 269, 'y': -160, 'z': 160}]
arm_3_position7 = [{'x': 215, 'y': 25, 'z': 50}]
arm_3_position8 = [{'x': 215, 'y': 25, 'z': 35}]
arm_3_position8_2 = [{'x': 165, 'y': -5, 'z': 180}]  # ⭐ TRIGGER POSITION FOR ARM 2 SEQ 2

# ⭐ ARM SEQUENCES WITH UPDATED POSITION-BASED COORDINATION AND EMERGENCY STOP CHECKS

def arm_1_sequence_1_dynamic():
    """Arm 1 Sequence 1 with dynamic speed and emergency stop checks"""
    try:
        if check_emergency_stop():
            print("🛑 Arm 1 Sequence 1 blocked - Emergency stop active!")
            return
            
         # ⭐ START CYCLE TIMING
        start_cycle_timing()

        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 1 Sequence 1 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(1, "MOVING_TO_ORIGIN", 10)
        arm_1.set_pump(on=False)
        arm_1.set_wrist(82)
        move_arm_dynamic(arm_1, arm_1_origin, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "PICKUP_POSITION", 20)
        move_arm_dynamic(arm_1, arm_1_position1, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "ACTIVATING_PUMP", 30)
        arm_1.set_pump(on=True)
        
        if check_emergency_stop(): return
        update_step_api(1, "PICKING_UP", 40)
        move_arm_catch_dynamic(arm_1, arm_1_position2, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "LIFTING", 50)
        move_arm_catch_dynamic(arm_1, arm_1_position3, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "TO_CONVEYOR", 70)
        move_arm_dynamic(arm_1, arm_1_position3_2, speed_percentage, 1)    
        time.sleep(0.5)
        move_arm_dynamic(arm_1, arm_1_position4, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "PLACING", 85)
        move_arm_dynamic(arm_1, arm_1_position5, speed_percentage, 1)
        arm_1.set_wrist(52)
        time.sleep(0.5)
        arm_1.set_wrist(52)
        move_arm_catch_dynamic(arm_1, arm_1_position6, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "RELEASING", 95)
        arm_1.set_pump(on=False)
        move_arm_dynamic(arm_1, arm_1_position5, speed_percentage, 1)
        time.sleep(0.5)   
        
        if check_emergency_stop(): return
        update_step_api(1, "RETURNING", 98)
        arm_1.set_wrist(82)
        move_arm_dynamic(arm_1, arm_1_position4, speed_percentage, 1)
        time.sleep(0.5)
        move_arm_dynamic(arm_1, arm_1_position3_2, speed_percentage, 1)
        
        update_step_api(1, "IDLE", 0)
        print(f"✅ Arm 1 Sequence 1 completed at {speed_percentage}%")
        
    except Exception as e:
        print(f"❌ Arm 1 Sequence 1 error: {e}")
        update_step_api(1, "ERROR", 0)

def arm_1_sequence_2_dynamic():
    """Arm 1 Sequence 2 with dynamic speed and emergency stop checks"""
    try:
        if check_emergency_stop():
            print("🛑 Arm 1 Sequence 2 blocked - Emergency stop active!")
            return
            
        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 1 Sequence 2 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(1, "MOVING_TO_ORIGIN", 10)
        arm_1.set_pump(on=False)
        arm_1.set_wrist(82)
        move_arm_dynamic(arm_1, arm_1_origin, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "PICKUP_POSITION", 20)
        move_arm_dynamic(arm_1, arm_1_seq2_position1, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "ACTIVATING_PUMP", 30)
        arm_1.set_pump(on=True)
        
        if check_emergency_stop(): return
        update_step_api(1, "PICKING_UP", 40)
        move_arm_catch_dynamic(arm_1, arm_1_seq2_position2, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "LIFTING", 50)
        move_arm_catch_dynamic(arm_1, arm_1_seq2_position3, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "TO_CONVEYOR", 70)
        move_arm_dynamic(arm_1, arm_1_seq2_position3_2, speed_percentage, 1)    
        time.sleep(0.5)
        move_arm_dynamic(arm_1, arm_1_position4, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "PLACING", 85)
        move_arm_dynamic(arm_1, arm_1_position5, speed_percentage, 1)
        arm_1.set_wrist(85)
        time.sleep(0.5)
        arm_1.set_wrist(85)
        move_arm_catch_dynamic(arm_1, arm_1_position6, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "RELEASING", 95)
        arm_1.set_pump(on=False)
        move_arm_dynamic(arm_1, arm_1_position5, speed_percentage, 1)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(1, "RETURNING", 98)
        arm_1.set_wrist(82)
        move_arm_dynamic(arm_1, arm_1_position4, speed_percentage, 1)
        time.sleep(0.5)
        move_arm_dynamic(arm_1, arm_1_seq2_position3_2, speed_percentage, 1)
        
        update_step_api(1, "IDLE", 0)
        print(f"✅ Arm 1 Sequence 2 completed at {speed_percentage}%")
        
    except Exception as e:
        print(f"❌ Arm 1 Sequence 2 error: {e}")
        update_step_api(1, "ERROR", 0)

def arm_2_sequence_1_dynamic_with_position_triggers():
    """Arm 2 Sequence 1 - triggers Arm 3 at position4, waits for Arm 3 position8_2"""
    global arm_2_sequence_2_pending, arm_3_reached_position8_2, arm_2_reached_position4
    
    try:
        if check_emergency_stop():
            print("🛑 Arm 2 Sequence 1 blocked - Emergency stop active!")
            return
            
        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 2 Sequence 1 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(2, "INITIAL_POS", 15)
        move_arm_dynamic(arm_2, arm_2_position1, speed_percentage, 2)
        arm_2.set_gripper(catch=False)
        arm_2.set_wrist(180)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(2, "PICKUP_POINT", 25)
        move_arm_dynamic(arm_2, arm_2_position2, speed_percentage, 2)
        time.sleep(0.5)
        
        # ⭐ ENHANCED GRIPPING SEQUENCE - SLOWER APPROACH
        if check_emergency_stop(): return
        update_step_api(2, "APPROACHING_OBJECT", 30)
        print("🎯 Arm 2: Approaching object slowly for precise gripping...")
        move_arm_catch_dynamic(arm_2, arm_2_position2_2, speed_percentage, 2)
        time.sleep(1.0)  # ⭐ INCREASED DELAY
        
        if check_emergency_stop(): return
        update_step_api(2, "POSITIONING_FOR_GRIP", 35)
        print("🎯 Arm 2: Final positioning for grip...")
        move_arm_catch_dynamic(arm_2, arm_2_position3, speed_percentage, 2)
        time.sleep(1.5)  # ⭐ LONGER DELAY FOR POSITIONING
        
        # ⭐ ENHANCED GRIPPING WITH VERIFICATION
        if check_emergency_stop(): return
        update_step_api(2, "GRIPPING_OBJECT", 40)
        print("🤏 Arm 2: Activating gripper...")
        arm_2.set_gripper(catch=True)
        time.sleep(2.0)  # ⭐ INCREASED GRIP TIME
        
        # ⭐ SECURE GRIP CONFIRMATION
        if check_emergency_stop(): return
        update_step_api(2, "SECURING_GRIP", 45)
        print("🔒 Arm 2: Securing grip...")
        arm_2.set_gripper(catch=True)  # ⭐ DOUBLE GRIP COMMAND
        time.sleep(1.5)  # ⭐ ADDITIONAL SECURE TIME
        
        if check_emergency_stop(): return
        update_step_api(2, "LIFTING", 55)
        print("⬆️ Arm 2: Lifting object...")
        move_arm_dynamic(arm_2, arm_2_position3_2, speed_percentage, 2)
        time.sleep(1.0)  # ⭐ PAUSE AFTER LIFTING
        
        if check_emergency_stop(): return
        update_step_api(2, "TO_ASSEMBLY", 70)
        # ⭐ THIS MOVE TO position4 WILL TRIGGER ARM 3! (EARLIER TRIGGER)
        print(f"🎯 Arm 2: Moving to position4 - this will trigger Arm 3!")
        move_arm_dynamic_with_triggers(arm_2, arm_2_position4, speed_percentage, 2)
        arm_2.set_wrist(150)
        time.sleep(0.5)
        arm_2.set_wrist(120)
        move_arm_dynamic(arm_2, arm_2_position5, speed_percentage, 2)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(2, "WAITING_ARM3", 85)
        arm_2.set_wrist(90)
        move_arm_dynamic(arm_2, arm_2_position6, speed_percentage, 2)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(2, "ASSEMBLING", 95)
        arm_2.set_wrist(50)
        move_arm_catch_dynamic(arm_2, arm_2_position7, speed_percentage, 2)
        
        # ⭐ WAITING LOGIC - WAIT FOR ARM 3 POSITION8_2
        update_step_api(2, "WAITING_ARM3_POSITION8_2", 98)
        print(f"⏳ Arm 2 Sequence 1 completed - waiting for Arm 3 to reach position8_2...")
        
        # ⭐ ACTIVE WAITING LOOP FOR POSITION8_2
        wait_timeout = 30  # 30 second timeout
        wait_start = time.time()
        
        while not arm_3_reached_position8_2 and (time.time() - wait_start) < wait_timeout and not check_emergency_stop():
            print(f"⏳ Arm 2: Waiting for Arm 3 position8_2... ({int(time.time() - wait_start)}s)")
            time.sleep(1.0)
            
            # Check if Arm 2 Seq 2 was triggered by Arm 3 position8_2
            if arm_2_sequence_2_pending and arm_3_reached_position8_2:
                break
        
        if arm_3_reached_position8_2 and arm_2_sequence_2_pending:
            print(f"✅ Arm 3 reached position8_2! Arm 2 Sequence 2 will start...")
        else:
            print(f"⚠️ Timeout or Arm 3 position8_2 not reached")
            
        update_step_api(2, "IDLE", 0)
        
    except Exception as e:
        print(f"❌ Arm 2 Sequence 1 error: {e}")
        update_step_api(2, "ERROR", 0)

def arm_2_sequence_2_dynamic():
    """Arm 2 Sequence 2 with dynamic speed and emergency stop checks"""
    try:
        if check_emergency_stop():
            print("🛑 Arm 2 Sequence 2 blocked - Emergency stop active!")
            return
            
        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 2 Sequence 2 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(2, "TO_CONVEYOR2", 75)
        move_arm_dynamic(arm_2, arm_2_seq2_position2, speed_percentage, 2)
        arm_2.set_wrist(0)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(2, "PLACING", 85)
        move_arm_catch_dynamic(arm_2, arm_2_seq2_position3, speed_percentage, 2)
        time.sleep(1.0)
        
        if check_emergency_stop(): return
        update_step_api(2, "RELEASING", 95)
        arm_2.set_gripper(catch=False)
        time.sleep(1.0)
        
        if check_emergency_stop(): return
        update_step_api(2, "RETURNING", 98)
        move_arm_catch_dynamic(arm_2, arm_2_seq2_position3_2, speed_percentage, 2)
        time.sleep(0.5)
        move_arm_catch_dynamic(arm_2, arm_2_seq2_position2, speed_percentage, 2)
        time.sleep(0.5)
        move_arm_dynamic(arm_2, arm_2_seq2_position1, speed_percentage, 2)
        arm_2.set_wrist(90)
        
        update_step_api(2, "IDLE", 0)
        print(f"✅ Arm 2 Sequence 2 completed at {speed_percentage}%")
        
    except Exception as e:
        print(f"❌ Arm 2 Sequence 2 error: {e}")
        update_step_api(2, "ERROR", 0)

def arm_3_sequence_1_dynamic():
    """Arm 3 Sequence 1 with position8_2 monitoring for Arm 2 coordination"""
    try:
        if check_emergency_stop():
            print("🛑 Arm 3 Sequence 1 blocked - Emergency stop active!")
            return
            
        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 3 Sequence 1 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(3, "PCB_PICKUP", 15)
        arm_3.set_pump(on=False)
        arm_3.set_wrist(90)
        move_arm_dynamic(arm_3, arm_3_position1, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "PICKING_PCB", 30)
        move_arm_dynamic(arm_3, arm_3_position2, speed_percentage, 3)
        time.sleep(0.5)
        move_arm_dynamic(arm_3, arm_3_position3, speed_percentage, 3)
        time.sleep(0.5)
        arm_3.set_pump(on=True)
        move_arm_catch_dynamic(arm_3, arm_3_position4, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "TO_ASSEMBLY", 50)
        move_arm_catch_dynamic(arm_3, arm_3_position3, speed_percentage, 3)
        time.sleep(0.5)
        move_arm_dynamic(arm_3, arm_3_position2, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "WAITING_ARM2", 70)
        move_arm_dynamic(arm_3, arm_3_position5, speed_percentage, 3)
        arm_3.set_wrist(180)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "ASSEMBLING", 85)
        move_arm_catch_dynamic(arm_3, arm_3_position6, speed_percentage, 3)
        arm_3.set_pump(on=False)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "TO_STORAGE", 95)
        move_arm_catch_dynamic(arm_3, arm_3_position5, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "STORING", 98)
        # ⭐ THIS MOVE TO position8_2 WILL TRIGGER ARM 2 SEQ 2!
        print(f"🎯 Arm 3: Moving to position8_2 - this will trigger Arm 2 Seq 2!")
        move_arm_dynamic_with_triggers(arm_3, arm_3_position8_2, speed_percentage, 3)
        arm_3.set_wrist(70)
        
        update_step_api(3, "IDLE", 0)
        print(f"✅ Arm 3 Sequence 1 completed at {speed_percentage}%")
        
    except Exception as e:
        print(f"❌ Arm 3 Sequence 1 error: {e}")
        update_step_api(3, "ERROR", 0)

def arm_3_sequence_2_dynamic():
    """Arm 3 Sequence 2 with position8_2 monitoring for Arm 2 coordination"""
    try:
        if check_emergency_stop():
            print("🛑 Arm 3 Sequence 2 blocked - Emergency stop active!")
            return
            
        speed_percentage = get_speed_percentage()
        print(f"🤖 Arm 3 Sequence 2 starting at {speed_percentage}%")
        
        if check_emergency_stop(): return
        update_step_api(3, "PCB_PICKUP", 15)
        move_arm_dynamic(arm_3, arm_3_position2, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "PICKING_PCB", 30)
        move_arm_dynamic(arm_3, arm_3_position7, speed_percentage, 3)
        time.sleep(0.5)
        arm_3.set_pump(on=True)
        move_arm_catch_dynamic(arm_3, arm_3_position8, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "TO_ASSEMBLY", 50)
        move_arm_dynamic(arm_3, arm_3_position7, speed_percentage, 3)
        time.sleep(0.5)
        move_arm_dynamic(arm_3, arm_3_position2, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "WAITING_ARM2", 70)
        move_arm_dynamic(arm_3, arm_3_position5, speed_percentage, 3)
        arm_3.set_wrist(180)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "ASSEMBLING", 85)
        move_arm_catch_dynamic(arm_3, arm_3_position6, speed_percentage, 3)
        arm_3.set_pump(on=False)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "TO_STORAGE", 95)
        move_arm_catch_dynamic(arm_3, arm_3_position5, speed_percentage, 3)
        time.sleep(0.5)
        
        if check_emergency_stop(): return
        update_step_api(3, "STORING", 98)
        # ⭐ THIS MOVE TO position8_2 WILL TRIGGER ARM 2 SEQ 2!
        print(f"🎯 Arm 3: Moving to position8_2 - this will trigger Arm 2 Seq 2!")
        move_arm_dynamic_with_triggers(arm_3, arm_3_position8_2, speed_percentage, 3)
        arm_3.set_wrist(70)
        
        update_step_api(3, "IDLE", 0)
        print(f"✅ Arm 3 Sequence 2 completed at {speed_percentage}%")
        
    except Exception as e:
        print(f"❌ Arm 3 Sequence 2 error: {e}")
        update_step_api(3, "ERROR", 0)

# ⭐ THREAD SAFETY
arm_1_busy = False
arm_2_busy = False
arm_3_busy = False
lock = Lock()

def safe_set_busy(arm_num, value):
    global arm_1_busy, arm_2_busy, arm_3_busy
    with lock:
        if arm_num == 1:
            arm_1_busy = value
        elif arm_num == 2:
            arm_2_busy = value
        elif arm_num == 3:
            arm_3_busy = value

# ⭐ WRAPPER FUNCTIONS WITH THREAD SAFETY
def arm_1_sequence_1_wrapper():
    safe_set_busy(1, True)
    try:
        arm_1_sequence_1_dynamic()
    finally:
        safe_set_busy(1, False)
        
def arm_1_sequence_2_wrapper():
    safe_set_busy(1, True)
    try:
        arm_1_sequence_2_dynamic()
    finally:
        safe_set_busy(1, False)
        
def arm_2_sequence_1_wrapper():
    safe_set_busy(2, True)
    try:
        arm_2_sequence_1_dynamic_with_position_triggers()
    finally:
        safe_set_busy(2, False)

def arm_2_sequence_2_wrapper():
    safe_set_busy(2, True)
    try:
        arm_2_sequence_2_dynamic()
    finally:
        safe_set_busy(2, False)

def arm_3_sequence_1_wrapper():
    safe_set_busy(3, True)
    try:
        arm_3_sequence_1_dynamic()
    finally:
        safe_set_busy(3, False)

def arm_3_sequence_2_wrapper():
    safe_set_busy(3, True)
    try:
        arm_3_sequence_2_dynamic()
    finally:
        safe_set_busy(3, False)

# ⭐ INITIALIZE STEP TRACKING
print("🤖 Initializing robot arm step tracking...")
update_step_api(1, "IDLE", 0)
update_step_api(2, "IDLE", 0)
update_step_api(3, "IDLE", 0)
print("✅ Step tracking initialized!")

# Add this model for reset requests
class ResetRequest(BaseModel):
    command: str = "RESET_TO_ORIGIN"
    reset_all_arms: bool = True
    clear_emergency_stop: bool = True
    source: str = "api_request"

# Add this endpoint to your existing FastAPI app
@app.post("/api/reset_robots")
async def reset_robots_endpoint(request: ResetRequest):
    """API endpoint to reset all robot arms to origin positions"""
    try:
        print(f"🔄 Reset API called from: {request.source}")
        
        if request.command != "RESET_TO_ORIGIN":
            raise HTTPException(status_code=400, detail="Invalid reset command")
        
        # Execute reset in background thread to avoid blocking API response
        reset_thread = threading.Thread(target=execute_robot_reset_api, args=(request,))
        reset_thread.daemon = True
        reset_thread.start()
        
        return {
            "status": "success",
            "message": "Robot reset initiated - arms moving to origin positions",
            "reset_all_arms": request.reset_all_arms,
            "clear_emergency_stop": request.clear_emergency_stop
        }
        
    except Exception as e:
        print(f"❌ Reset API error: {e}")
        raise HTTPException(status_code=500, detail=f"Reset failed: {str(e)}")


@app.post("/api/enable_system")
async def enable_system_endpoint():
    """Enable robot system - allows robots to respond to PLC triggers"""
    try:
        enable_system()
        return {
            "status": "success",
            "message": "Robot system enabled",
            "system_enabled": True
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Enable failed: {str(e)}")


@app.post("/api/disable_system")
async def disable_system_endpoint():
    """Disable robot system - robots will ignore PLC triggers"""
    try:
        disable_system()
        return {
            "status": "success",
            "message": "Robot system disabled",
            "system_enabled": False
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Disable failed: {str(e)}")


@app.get("/api/system_status")
async def get_system_status():
    """Get current system enable/disable status"""
    return {
        "system_enabled": is_system_enabled(),
        "emergency_stop_active": check_emergency_stop()
    }

def execute_robot_reset_api(request: ResetRequest):
    """Execute robot reset based on API request"""
    global arm_1_busy, arm_2_busy, arm_3_busy, emergency_stop_active
    
    print("🔄 Executing API-triggered robot reset...")
    
    try:
        # 1. Force stop all sequences and clear emergency stop
        print("🛑 Force stopping all sequences...")
        force_stop_all_sequences()
        
        # 2. Clear emergency stop directly
        with stop_lock:
            emergency_stop_active = False
            print("✅ Emergency stop cleared via API")
        
        # 3. Reset all coordination flags
        reset_all_coordination_flags()
        
        # 4. Stop any current operations and deactivate all pumps/grippers
        print("🔄 Deactivating all pumps and grippers...")
        for swift in [arm_1, arm_2, arm_3]:
            swift.flush_cmd()  # Clear command queue
        
        # ⭐ DEACTIVATE ALL PUMPS AND GRIPPERS IMMEDIATELY
        try:
            arm_1.set_pump(on=False)
            arm_2.set_gripper(catch=False)
            arm_3.set_pump(on=False)
            print("✅ All pumps and grippers deactivated")
            time.sleep(1.0)  # Wait for deactivation
        except Exception as e:
            print(f"⚠️ Error deactivating pumps/grippers: {e}")
        
        # 5. Set all arms as busy during reset
        if request.reset_all_arms:
            arm_1_busy = True
            arm_2_busy = True
            arm_3_busy = True
        
        # 6. Execute reset for all arms simultaneously
        reset_threads = []
        
        def reset_arm_1():
            try:
                print("🔄 Resetting Arm 1 to origin...")
                update_step_api(1, "RESETTING", 50)
                
                # ⭐ ENHANCED: Ensure pump is off and set proper wrist
                arm_1.set_pump(on=False)  # Release any objects
                time.sleep(0.5)
                arm_1.set_wrist(82)       # Default wrist position
                time.sleep(0.5)
                
                print("🎯 Moving Arm 1 to origin position: x=180, y=0, z=0")
                move_arm_dynamic(arm_1, [{'x': 180, 'y': 0, 'z': 0}], 30, 1)  # Slow, safe speed
                time.sleep(1.0)
                
                # ⭐ DOUBLE-CHECK: Ensure pump is still off
                arm_1.set_pump(on=False)
                
                update_step_api(1, "IDLE", 0)
                print("✅ Arm 1 reset to origin complete")
                
            except Exception as e:
                print(f"❌ Arm 1 reset error: {e}")
            finally:
                arm_1_busy = False
        
        def reset_arm_2():
            try:
                print("🔄 Resetting Arm 2 to origin...")
                update_step_api(2, "RESETTING", 50)
                
                # ⭐ ENHANCED: Ensure gripper is open and set proper wrist
                arm_2.set_gripper(catch=False)  # Release gripper
                time.sleep(0.5)
                arm_2.set_wrist(90)             # Default wrist position
                time.sleep(0.5)
                
                print("🎯 Moving Arm 2 to origin position: x=160, y=0, z=80")
                move_arm_dynamic(arm_2, [{'x': 160, 'y': 0, 'z': 80}], 30, 2)  # Slow, safe speed
                time.sleep(1.0)
                
                # ⭐ DOUBLE-CHECK: Ensure gripper is still open
                arm_2.set_gripper(catch=False)
                
                update_step_api(2, "IDLE", 0)
                print("✅ Arm 2 reset to origin complete")
                
            except Exception as e:
                print(f"❌ Arm 2 reset error: {e}")
            finally:
                arm_2_busy = False
        
        def reset_arm_3():
            try:
                print("🔄 Resetting Arm 3 to origin...")
                update_step_api(3, "RESETTING", 50)
                
                # ⭐ ENHANCED: Ensure pump is off and set proper wrist
                arm_3.set_pump(on=False)  # Release any objects
                time.sleep(0.5)
                arm_3.set_wrist(90)       # Default wrist position
                time.sleep(0.5)
                
                print("🎯 Moving Arm 3 to origin position: x=130, y=0, z=120")
                move_arm_dynamic(arm_3, [{'x': 130, 'y': 0, 'z': 120}], 30, 3)  # Slow, safe speed
                time.sleep(1.0)
                
                # ⭐ DOUBLE-CHECK: Ensure pump is still off
                arm_3.set_pump(on=False)
                
                update_step_api(3, "IDLE", 0)
                print("✅ Arm 3 reset to origin complete")
                
            except Exception as e:
                print(f"❌ Arm 3 reset error: {e}")
            finally:
                arm_3_busy = False
        
        # Start all reset operations simultaneously
        if request.reset_all_arms:
            reset_threads = [
                threading.Thread(target=reset_arm_1),
                threading.Thread(target=reset_arm_2), 
                threading.Thread(target=reset_arm_3)
            ]
            
            for thread in reset_threads:
                thread.start()
            
            # Wait for all to complete
            for thread in reset_threads:
                thread.join()
            
            # ⭐ FINAL CHECK: Ensure all pumps/grippers are deactivated
            print("🔄 Final safety check - ensuring all pumps/grippers are off...")
            try:
                arm_1.set_pump(on=False)
                arm_2.set_gripper(catch=False)
                arm_3.set_pump(on=False)
                print("✅ Final safety check complete")
            except Exception as e:
                print(f"⚠️ Final safety check error: {e}")
                
            print("🔄 All robot arms reset to origin positions via API!")
        
        # 7. Final state reset
        print("🔄 Final state cleanup...")
        reset_all_coordination_flags()  # One final cleanup
        
        # 8. Update all step tracking to IDLE
        for i in [1, 2, 3]:
            set_robot_stopped(i, "IDLE")
            update_step_api(i, "IDLE", 0)
        
        print("✅ RESET COMPLETE - System ready for new sequences!")
        
    except Exception as e:
        print(f"❌ Error during API robot reset: {e}")
        # Ensure busy flags are cleared even on error
        arm_1_busy = False
        arm_2_busy = False
        arm_3_busy = False
        
        # ⭐ EMERGENCY: Try to deactivate pumps/grippers even on error
        try:
            arm_1.set_pump(on=False)
            arm_2.set_gripper(catch=False)
            arm_3.set_pump(on=False)
            print("🛑 Emergency pump/gripper deactivation completed")
        except:
            print("❌ Emergency pump/gripper deactivation failed")

def robot_main_loop():
    # ⭐ DECLARE GLOBAL VARIABLES
    global arm_2_sequence_2_pending, arm_3_reached_position8_2, arm_2_reached_position4
    global arm_1_busy, arm_2_busy, arm_3_busy, arm_3_sequence_counter, arm_1_sequence_counter
    
    print("🚀 Optimized Position-Based Robot Coordination System Starting...")
    
    print("🚀 Optimized Position-Based Robot Coordination System Starting...")
    print("🛑 Emergency Stop System Enabled!")
    print("📍 Trigger Sources:")
    print("   🔘 Arm 1: DB1.DBX10.0 (manual) OR DB1.DBX10.2 (Prox 4) - ALTERNATES Seq 1/2")
    print("   🔘 Arm 2 Seq 1: DB1.DBX10.1 (arm_2_PLC)") 
    print("   🔘 Arm 3 Sequences: Position-based (Arm 2 at 190,210,120) ⭐ EARLIER!")
    print("   🔘 Arm 2 Seq 2: Position-based (Arm 3 at 165,-5,180)")
    print("   🛑 Emergency Stop: DB1.DBX0.1 (Stop bit)")
    print(f"📊 Initial Speed: {get_speed_percentage()}%")
    
    # Previous state tracking - YOUR ORIGINAL VARIABLES
    prev_arm_1_PLC = False
    prev_arm_2_PLC = False
    prev_arm_3_PLC = False
    prev_stop_PLC = False  # ⭐ ADD STOP TRACKING
    
    loop_counter = 0
    status_update_counter = 0
    
    while True:
        try:
            #  USE YOUR ORIGINAL PLC READING
            arm_1_PLC, arm_2_PLC, arm_3_PLC = read_plc_data()
            
            #  READ STOP BIT FROM PLC
            stop_PLC = read_plc_stop_data()
            
            #  HANDLE STOP COMMAND - ENHANCED
            if stop_PLC and not prev_stop_PLC:
                print(" STOP command received from PLC!")
                activate_emergency_stop()
                force_stop_all_sequences()  #  ADD THIS
            elif not stop_PLC and prev_stop_PLC:
                print(" STOP command released from PLC!")
                complete_system_reset()  #  CHANGE THIS - was just deactivate_emergency_stop()
            
            current_speed = get_speed_percentage()

            # ⭐ CHECK IF SYSTEM IS ENABLED BEFORE PROCESSING ANY COMMANDS
            if not is_system_enabled():
                # System is disabled - just update previous states and continue
                prev_arm_1_PLC = arm_1_PLC
                prev_arm_2_PLC = arm_2_PLC
                prev_arm_3_PLC = arm_3_PLC
                prev_stop_PLC = stop_PLC
                time.sleep(0.1)
                continue

            # ⭐ ONLY PROCESS ARM COMMANDS IF NOT IN EMERGENCY STOP
            if not check_emergency_stop():
                # ⭐ ARM 1 - ALTERNATES BETWEEN SEQUENCE 1 AND 2 (DB1.DBX10.0)
                if arm_1_PLC and not prev_arm_1_PLC and not arm_1_busy:
                    if arm_1_sequence_counter == 0:
                        print(f"🟦 Arm 1 Sequence 1 triggered by DB1.DBX10.0 at {current_speed}%")
                        start_cycle_timing()
                        arm_1.set_buzzer(frequency=1000, duration=1)
                        threading.Thread(target=arm_1_sequence_1_wrapper).start()
                        arm_1_sequence_counter = 1  # Next time will be sequence 2
                    else:
                        print(f"🟦 Arm 1 Sequence 2 triggered by DB1.DBX10.0 at {current_speed}%")
                        end_cycle_timing()
                        arm_1.set_buzzer(frequency=1000, duration=1)
                        threading.Thread(target=arm_1_sequence_2_wrapper).start()
                        arm_1_sequence_counter = 0  # Next time will be sequence 1

                # ⭐ ARM 2 SEQUENCE 1 - YOUR ORIGINAL TRIGGER (DB1.DBX10.1)
                if arm_2_PLC and not prev_arm_2_PLC and not arm_2_busy:
                    print(f"🟩 Arm 2 Sequence 1 triggered by DB1.DBX10.1 at {current_speed}%")
                    arm_2.set_buzzer(frequency=1000, duration=1)
                    # ⭐ ARM 3 WILL BE TRIGGERED BY POSITION4! (EARLIER)
                    # ⭐ ARM 2 SEQ 2 WILL BE TRIGGERED BY ARM 3 POSITION8_2!
                    threading.Thread(target=arm_2_sequence_1_wrapper).start()

                # ⭐ ARM 1 TRIGGER (DB1.DBX10.2) - For Prox Sensor 4 / Production Complete
                # ALTERNATES between Seq 1 and 2 just like DB1.DBX10.0
                if arm_3_PLC and not prev_arm_3_PLC and not arm_1_busy:
                    if arm_1_sequence_counter == 0:
                        print(f"🟦 Arm 1 Sequence 1 triggered by DB1.DBX10.2 (Prox Sensor 4) at {current_speed}%")
                        start_cycle_timing()
                        arm_1.set_buzzer(frequency=1000, duration=1)
                        threading.Thread(target=arm_1_sequence_1_wrapper).start()
                        arm_1_sequence_counter = 1  # Next time will be sequence 2
                    else:
                        print(f"🟦 Arm 1 Sequence 2 triggered by DB1.DBX10.2 (Prox Sensor 4) at {current_speed}%")
                        end_cycle_timing()
                        arm_1.set_buzzer(frequency=1000, duration=1)
                        threading.Thread(target=arm_1_sequence_2_wrapper).start()
                        arm_1_sequence_counter = 0  # Next time will be sequence 1

                # ⭐ FIX: Check for pending Arm 2 Seq 2 in main loop
                if (arm_2_sequence_2_pending and arm_3_reached_position8_2 and 
                    not arm_2_busy and not arm_3_busy):
                    print(f" MAIN LOOP: Starting pending Arm 2 Sequence 2!")
                    arm_2_sequence_2_pending = False
                    arm_3_reached_position8_2 = False
                    arm_2_reached_position4 = False
                    threading.Thread(target=arm_2_sequence_2_wrapper).start()

            # Update previous states - YOUR ORIGINAL LOGIC
            prev_arm_1_PLC = arm_1_PLC
            prev_arm_2_PLC = arm_2_PLC
            prev_arm_3_PLC = arm_3_PLC
            prev_stop_PLC = stop_PLC  #  ADD STOP STATE TRACKING

            # Status updates
            status_update_counter += 1
            if status_update_counter % 10 == 0:
                save_robot_status_to_file()
            
            loop_counter += 1
            if loop_counter % 50 == 0:
                move_speed = percentage_to_move_speed(current_speed)
                catch_speed = percentage_to_catch_speed(current_speed)

        except Exception as e:
            print(f" Error: {e}")
        time.sleep(0.1)


#  MAIN LOOP WITH UPDATED COORDINATION AND EMERGENCY STOP MONITORING
if __name__ == "__main__":
    print("=" * 80)
    print(" 🤖 ROBOT ARM CONTROLLER STARTING...")
    print("=" * 80)
    print(" 🔒 SYSTEM STATUS: DISABLED (Awaiting START command)")
    print(" 📝 To enable robots: Say 'start' in the chatbot or call /api/enable_system")
    print(" 🚦 Robots will NOT move until you explicitly start the system")
    print("=" * 80)
    print(" Starting robot_controller.py with FastAPI server on port 8001...")
    
    # Start the main robot loop in a background thread
    robot_thread = threading.Thread(target=robot_main_loop, daemon=True)
    robot_thread.start()
    
    # Start FastAPI server
    uvicorn.run(app, host="0.0.0.0", port=8001)
