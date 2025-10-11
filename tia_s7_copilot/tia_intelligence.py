"""TIA Openness intelligence module (Windows + TIA Portal only, optional).

Reads tags, blocks, hardware, network and project information from a TIA Portal
project and caches it as JSON. Connection mode is chosen automatically:
ATTACHED (TIA Portal open, read-only), EXCLUSIVE (headless instance) or
JSON_CACHE (last successful export). See docs/tia-dual-mode.md.
"""

import clr
import sys
import os
import json
from datetime import datetime
import threading
import time

# Import configuration
from .settings import get_tia_config, data_path

# Get TIA configuration
tia_config = get_tia_config()
TIA_VERSION = tia_config["tia_version"]
TIA_PATH = tia_config["tia_path"]
PROJECT_PATH = tia_config["project_path"]

# Add TIA Openness to path
sys.path.append(TIA_PATH)

# Load TIA Openness DLLs
try:
    clr.AddReference("Siemens.Engineering")
    from Siemens.Engineering import TiaPortal, TiaPortalMode
    from System.IO import FileInfo
    import System
    TIA_AVAILABLE = True
    print("✅ TIA Openness DLLs loaded successfully")
except Exception as e:
    TIA_AVAILABLE = False
    print(f"⚠️ TIA Openness not available: {e}")

class TIAIntelligence:
    """Complete TIA Openness integration for analysis and diagnostics (AUTO DUAL-MODE)"""
    
    def __init__(self):
        self.tia_portal = None
        self.project = None
        self.plc_software = None
        self.connected = False
        self.connection_error = None
        
        # ⭐ DUAL-MODE SYSTEM
        self.connection_mode = None  # "ATTACHED" (TIA open) or "EXCLUSIVE" (TIA closed)
        self.use_cached_json = False  # True when reading from JSON files
        
        # JSON cache file paths
        self.json_tags_file = data_path("tia_cache_tags.json")
        self.json_blocks_file = data_path("tia_cache_blocks.json")
        self.json_hardware_file = data_path("tia_cache_hardware.json")
        self.json_project_info_file = data_path("tia_cache_project_info.json")
        
        # Caches
        self.tag_cache = None
        self.block_cache = None
        self.alarm_cache = None
        self.structure_cache = None
        self.hardware_cache = None
        
        # Thread safety
        self.lock = threading.Lock()
        
        # Cache management
        self.last_tag_update = 0
        self.last_block_update = 0
        self.cache_duration = 5
        self.cache_timeout = 5
        
        # Connection retry settings
        self.max_retries = 3
        self.retry_delay = 2
        
    def connect(self):
        """Connect to TIA Portal with AUTOMATIC DUAL-MODE detection"""
        
        if os.environ.get('TIA_OPENNESS_DISABLED') == '1':
            self.connection_error = "TIA Openness disabled (TIA Portal running)"
            print("⚠️ TIA Openness disabled - running in Snap7-only mode")
            return False
        
        if not TIA_AVAILABLE:
            self.connection_error = "TIA Openness DLLs not available"
            print(f"❌ {self.connection_error}")
            return False
        
        if not os.path.exists(PROJECT_PATH):
            self.connection_error = f"Project file not found: {PROJECT_PATH}"
            print(f"❌ {self.connection_error}")
            return False
        
        # ⭐ AUTOMATIC MODE DETECTION
        tia_running = self._check_tia_running()
        
        if tia_running:
            print("\n🔍 TIA Portal is RUNNING - attempting ATTACH mode (read-only)...")
            return self._connect_attach_mode()
        else:
            print("\n🔍 TIA Portal is CLOSED - attempting EXCLUSIVE mode (full access)...")
            return self._connect_exclusive_mode()
    
    def _connect_attach_mode(self):
        """Connect to already-running TIA Portal instance (READ-ONLY)"""
        try:
            print("   Attaching to running TIA Portal process...")
            
            # Get running TIA Portal instances
            tia_processes = TiaPortal.GetProcesses()
            
            if tia_processes.Count == 0:
                print("❌ No TIA Portal processes found (it may have just closed)")
                return self._fallback_to_json_mode()
            
            print(f"   Found {tia_processes.Count} TIA Portal process(es)")
            
            # Attach to first running instance
            self.tia_portal = tia_processes[0].Attach()
            print("   ✅ Attached to TIA Portal process")
            
            # Get project from running instance
            if self.tia_portal.Projects.Count == 0:
                print("⚠️  No projects open in TIA Portal")
                return self._fallback_to_json_mode()
            
            self.project = self.tia_portal.Projects[0]
            print(f"   ✅ Connected to project: {self.project.Name}")
            
            # Find PLC software
            print("   Searching for PLC software...")
            self._find_plc_software()
            
            if self.plc_software:
                self.connected = True
                self.connection_mode = "ATTACHED"
                self.use_cached_json = False
                print("✅ TIA Intelligence ready in ATTACH mode (READ-ONLY)")
                print("   � Changes to project will be LIVE from TIA Portal")
                
                # Export fresh data to JSON files
                self._export_to_json_files()
                
                return True
            else:
                print("❌ PLC software not found")
                return self._fallback_to_json_mode()
                
        except Exception as e:
            print(f"❌ Attach mode failed: {e}")
            print(f"   Error type: {type(e).__name__}")
            return self._fallback_to_json_mode()
    
    def _connect_exclusive_mode(self):
        """Connect to TIA Portal in EXCLUSIVE mode (full access when TIA is closed)"""
        for attempt in range(self.max_retries):
            try:
                print(f"🔌 Opening TIA Portal (attempt {attempt + 1}/{self.max_retries})...")
                print(f"📂 Project: {PROJECT_PATH}")
                
                print("   Creating TiaPortal instance...")
                self.tia_portal = TiaPortal(TiaPortalMode.WithoutUserInterface)
                print("   ✅ TiaPortal instance created")
                
                print("   Opening project...")
                project_file = FileInfo(PROJECT_PATH)
                self.project = self.tia_portal.Projects.Open(project_file)
                
                print(f"✅ Connected to project: {self.project.Name}")
                
                print("   Searching for PLC software...")
                self._find_plc_software()
                
                if self.plc_software:
                    self.connected = True
                    self.connection_mode = "EXCLUSIVE"
                    self.use_cached_json = False
                    print("✅ TIA Intelligence ready in EXCLUSIVE mode (FULL ACCESS)")
                    
                    # Load and export data
                    self._initial_data_load()
                    self._export_to_json_files()
                    
                    return True
                else:
                    self.connection_error = "PLC software not found in project"
                    print(f"❌ {self.connection_error}")
                    self._print_available_devices()
                    return self._fallback_to_json_mode()
                    
            except Exception as e:
                self.connection_error = str(e)
                print(f"❌ Connection attempt {attempt + 1} failed: {e}")
                print(f"   Error type: {type(e).__name__}")
                
                if attempt < self.max_retries - 1:
                    print(f"⏳ Retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                else:
                    print("❌ All connection attempts failed")
                    self._print_troubleshooting_info()
                    return self._fallback_to_json_mode()
        
        return False
    
    def _fallback_to_json_mode(self):
        """Fallback to reading from cached JSON files"""
        print("\n⚠️  Falling back to JSON CACHE mode...")
        
        # Check if JSON files exist
        json_files_exist = (
            os.path.exists(self.json_tags_file) and
            os.path.exists(self.json_blocks_file) and
            os.path.exists(self.json_hardware_file)
        )
        
        if json_files_exist:
            print("✅ Found cached JSON files - using cached data")
            self.connected = True
            self.connection_mode = "JSON_CACHE"
            self.use_cached_json = True
            self._load_from_json_files()
            return True
        else:
            print("❌ No cached JSON files found")
            print("   💡 Open TIA Portal manually and try again, or close TIA Portal for exclusive access")
            self.connection_error = "No TIA connection and no cached data available"
            return False
    
    def _export_to_json_files(self):
        """Export current TIA data to JSON files for caching"""
        try:
            print("\n💾 Exporting TIA data to JSON cache files...")
            
            # Export tags
            tags_data = self.get_all_tags(force_refresh=True)
            if "error" not in tags_data:
                with open(self.json_tags_file, 'w', encoding='utf-8') as f:
                    json.dump(tags_data, f, indent=2)
                print(f"   ✅ Tags exported: {self.json_tags_file}")
            
            # Export blocks
            blocks_data = self.get_all_blocks(force_refresh=True)
            if "error" not in blocks_data:
                with open(self.json_blocks_file, 'w', encoding='utf-8') as f:
                    json.dump(blocks_data, f, indent=2)
                print(f"   ✅ Blocks exported: {self.json_blocks_file}")
            
            # Export hardware
            hardware_data = self.get_hardware_configuration()
            if "error" not in hardware_data:
                with open(self.json_hardware_file, 'w', encoding='utf-8') as f:
                    json.dump(hardware_data, f, indent=2)
                print(f"   ✅ Hardware exported: {self.json_hardware_file}")
            
            # Export project info
            project_info = self.get_project_info()
            if "error" not in project_info:
                with open(self.json_project_info_file, 'w', encoding='utf-8') as f:
                    json.dump(project_info, f, indent=2)
                print(f"   ✅ Project info exported: {self.json_project_info_file}")
            
            print("✅ JSON export complete\n")
            
        except Exception as e:
            print(f"⚠️  JSON export warning: {e}")
    
    def _load_from_json_files(self):
        """Load TIA data from cached JSON files"""
        try:
            print("📂 Loading TIA data from JSON cache...")
            
            # Load tags
            if os.path.exists(self.json_tags_file):
                with open(self.json_tags_file, 'r', encoding='utf-8') as f:
                    self.tag_cache = json.load(f)
                print(f"   ✅ Tags loaded: {self.tag_cache.get('count', 0)} tags")
            
            # Load blocks
            if os.path.exists(self.json_blocks_file):
                with open(self.json_blocks_file, 'r', encoding='utf-8') as f:
                    self.block_cache = json.load(f)
                print(f"   ✅ Blocks loaded: {self.block_cache.get('count', 0)} blocks")
            
            # Load hardware
            if os.path.exists(self.json_hardware_file):
                with open(self.json_hardware_file, 'r', encoding='utf-8') as f:
                    self.hardware_cache = json.load(f)
                print(f"   ✅ Hardware loaded: {self.hardware_cache.get('total_devices', 0)} devices")
            
            print("✅ JSON data loaded successfully\n")
            
        except Exception as e:
            print(f"❌ Error loading JSON files: {e}")
    
    def _check_tia_running(self):
        """Check if TIA Portal is running"""
        try:
            import subprocess
            result = subprocess.run(['tasklist'], capture_output=True, text=True, timeout=5)
            
            tia_processes = [
                'Siemens.Automation.Portal.exe',
                'TiaPortal.exe',
                'Siemens.Automation'
            ]
            
            tia_running = any(proc in result.stdout for proc in tia_processes)
            
            if tia_running:
                print("✅ TIA Portal process detected")
                return True
            else:
                print("✅ TIA Portal is NOT running")
                return False
            
        except Exception as check_error:
            print(f"⚠️ Could not check if TIA Portal is running: {check_error}")
            print("⚠️ Assuming TIA Portal is NOT running...")
            return False
            
        except Exception as check_error:
            print(f"⚠️ Could not check if TIA Portal is running: {check_error}")
            print("⚠️ Attempting to connect anyway...")
            return True
    
    def _print_troubleshooting_info(self):
        """Print troubleshooting information"""
        print("\n🔧 TROUBLESHOOTING TIPS:")
        print("1. Ensure TIA Portal V19 is open")
        print("2. Ensure your project is loaded in TIA Portal")
        print("3. Close TIA Portal UI and try again")
        print("4. Check if another application is using TIA Openness")
        print("5. Restart TIA Portal and try again")
        print()
    
    def _print_available_devices(self):
        """Print all available devices for debugging"""
        try:
            print("\n📋 Available devices in project:")
            device_count = 0
            for device in self.project.Devices:
                device_count += 1
                print(f"   Device {device_count}: {device.Name}")
                
                for item in device.DeviceItems:
                    item_name = str(item.Name)
                    print(f"      └─ Item: {item_name}")
            
            if device_count == 0:
                print("   ⚠️  No devices found in project")
            
            print("\n💡 TIP: Update the device name in _find_plc_software() method")
            print()
        except Exception as e:
            print(f"   Could not list devices: {e}")
    
    def _find_plc_software(self):
        """Find PLC software in project with enhanced search"""
        try:
            found_devices = []
            
            for device in self.project.Devices:
                device_name = str(device.Name)
                found_devices.append(device_name)
                
                for item in device.DeviceItems:
                    item_name = str(item.Name)
                    
                    search_patterns = [
                        "PLC_2", "PLC_1", "PLC", 
                        "CPU", "1515", "1516", "1517",
                        "S7-1500", "ET200"
                    ]
                    
                    for pattern in search_patterns:
                        if pattern.lower() in item_name.lower():
                            print(f"🎯 Found matching device item: {item_name}")
                            
                            if self._extract_plc_software(item):
                                return
            
            print(f"⚠️  Searched devices: {', '.join(found_devices)}")
            print("⚠️  No PLC software found")
                            
        except Exception as e:
            print(f"❌ Error finding PLC software: {e}")
    
    def _extract_plc_software(self, item):
        """Extract PLC software from device item"""
        try:
            software_container_type = System.Type.GetType(
                "Siemens.Engineering.HW.Features.SoftwareContainer, Siemens.Engineering"
            )
            
            get_service_method = item.GetType().GetMethod("GetService")
            generic_method = get_service_method.MakeGenericMethod(software_container_type)
            software_container = generic_method.Invoke(item, None)
            
            if software_container and hasattr(software_container, 'Software'):
                self.plc_software = software_container.Software
                print(f"✅ Found PLC Software: {self.plc_software.Name}")
                return True
                
        except Exception as e:
            print(f"   ⚠️  Could not get software: {e}")
        
        return False
    
    def _initial_data_load(self):
        """Load initial data on connection"""
        print("📊 Loading initial TIA data...")
        try:
            self.get_all_tags(force_refresh=True)
            self.get_all_blocks(force_refresh=True)
            print("✅ Initial data loaded")
        except Exception as e:
            print(f"⚠️  Initial data load warning: {e}")
    
    # ============================================
    # TAG INTELLIGENCE
    # ============================================
    
    def get_all_tags(self, force_refresh=False):
        """Get all tags with metadata from TIA Portal or JSON cache"""
        
        # ⭐ USE JSON CACHE MODE
        if self.use_cached_json:
            if self.tag_cache:
                print(f"📦 Using tags from JSON cache ({self.tag_cache.get('count', 0)} tags)")
                return self.tag_cache
            else:
                return {"error": "No cached tag data available", "count": 0, "tags": []}
        
        # ⭐ USE LIVE TIA CONNECTION
        if not self.connected:
            return {"error": "Not connected to TIA Portal", "details": self.connection_error}
        
        current_time = time.time()
        cache_age = current_time - self.last_tag_update
        
        if not force_refresh and self.tag_cache and cache_age < self.cache_duration:
            print(f"📦 Using cached tags ({cache_age:.1f}s old)")
            return self.tag_cache
        
        with self.lock:
            try:
                print(f"🔄 Fetching fresh tag data from TIA Portal...")
                tags = []
                tag_table_group = self.plc_software.TagTableGroup
                
                # ✅ FIX: Define tag_table_count before using it
                tag_table_count = tag_table_group.TagTables.Count
                print(f"  Found {tag_table_count} tag table(s)")

                for tag_table in tag_table_group.TagTables:
                    table_name = str(tag_table.Name)
                    tag_count = tag_table.Tags.Count
                    print(f"\n  Tag Table: '{table_name}' ({tag_count} tags)")
                    
                    for tag in tag_table.Tags:
                        tag_info = {
                            "name": str(tag.Name),
                            "data_type": str(tag.DataTypeName),
                            "address": "N/A",
                            "comment": "",
                            "table": table_name  # ✅ Add table name to tag info
                        }
                        
                        # Try to get address
                        try:
                            if hasattr(tag, 'LogicalAddress') and tag.LogicalAddress:
                                tag_info["address"] = str(tag.LogicalAddress)
                        except:
                            pass
                        
                        # Try to get comment
                        try:
                            if hasattr(tag, 'Comment') and tag.Comment:
                                comment_obj = tag.Comment
                                cultures = comment_obj.Cultures
                                if cultures.Count > 0:
                                    first_culture = cultures[0]
                                    comment_text = comment_obj.GetText(first_culture)
                                    if comment_text:
                                        tag_info["comment"] = str(comment_text)
                        except:
                            pass
                        
                        tags.append(tag_info)
                        
                        # Print tag details
                        print(f"    - {tag_info['name']}")
                        print(f"        Type: {tag_info['data_type']}")
                        if tag_info['address'] != "N/A":
                            print(f"        Address: {tag_info['address']}")
                        if tag_info['comment']:
                            print(f"        Comment: {tag_info['comment']}")

                print(f"\n  ✓ Total tags found: {len(tags)}")

                # Save tags to JSON file
                tags_file = data_path("plc_tags.json")
                with open(tags_file, 'w', encoding='utf-8') as f:
                    json.dump(tags, f, indent=2, ensure_ascii=False)
                print(f"  ✓ Tags saved to: {tags_file}")
                
                self.tag_cache = {
                    "tags": tags, 
                    "count": len(tags), 
                    "timestamp": datetime.now().isoformat(),
                    "cache_age": 0
                }
                self.last_tag_update = current_time
                
                print(f"✅ Loaded {len(tags)} tags from TIA Portal")
                return self.tag_cache
                
            except Exception as e:
                print(f"❌ Error fetching tags: {e}")
                import traceback
                traceback.print_exc()
                return {"error": str(e)}
    
    def get_tag_metadata(self, tag_name):
        """Get detailed metadata for a specific tag"""
        tags_data = self.get_all_tags()
        
        if "error" in tags_data:
            return tags_data
        
        for tag in tags_data["tags"]:
            if tag["name"].lower() == tag_name.lower():
                return {
                    "tag": tag,
                    "found": True,
                    "metadata": {
                        "name": tag["name"],
                        "type": tag["data_type"],
                        "address": tag["address"],
                        "comment": tag["comment"],
                        "table": tag["table"]
                    }
                }
        
        return {"found": False, "error": f"Tag '{tag_name}' not found"}
    
    def search_tags(self, search_term):
        """Search tags by name or comment"""
        tags_data = self.get_all_tags()
        
        if "error" in tags_data:
            return tags_data
        
        search_lower = search_term.lower()
        results = []
        
        for tag in tags_data["tags"]:
            if (search_lower in tag["name"].lower() or 
                search_lower in tag["comment"].lower()):
                results.append(tag)
        
        return {
            "search_term": search_term,
            "results": results,
            "count": len(results)
        }
    
    # ============================================
    # HARDWARE CONFIGURATION INTELLIGENCE
    # ============================================
    
    def get_hardware_configuration(self):
        """Get complete hardware configuration from TIA Portal or JSON cache"""
        
        # ⭐ USE JSON CACHE MODE
        if self.use_cached_json:
            if self.hardware_cache:
                print(f"📦 Using hardware from JSON cache ({self.hardware_cache.get('total_devices', 0)} devices)")
                return self.hardware_cache
            else:
                return {"error": "No cached hardware data available", "devices": [], "total_devices": 0}
        
        # ⭐ USE LIVE TIA CONNECTION
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            print("🔄 Fetching hardware configuration from TIA Portal...")
            hardware_data = {
                "devices": [],
                "total_devices": 0,
                "timestamp": datetime.now().isoformat()
            }
            
            device_count = self.project.Devices.Count
            print(f"  Found {device_count} device(s) in project")
            
            for device in self.project.Devices:
                device_name = str(device.Name)
                print(f"\n  📦 Device: {device_name}")
                
                device_info = {
                    "name": device_name,
                    "type": "Unknown",
                    "items": []
                }
                
                # Get device type
                try:
                    if hasattr(device, 'TypeIdentifier'):
                        device_info["type"] = str(device.TypeIdentifier)
                        print(f"     Type: {device_info['type']}")
                except:
                    pass
                
                # Get device items (CPU, I/O modules, etc.) - RECURSIVE
                item_count = device.DeviceItems.Count
                print(f"     Items: {item_count}")
                
                # Process all items recursively
                def process_device_item(item, indent_level=2):
                    """Recursively process device items and their sub-items"""
                    indent = "  " * indent_level
                    item_name = str(item.Name)
                    print(f"{indent}- {item_name}")
                    
                    item_info = {
                        "name": item_name,
                        "type": "Unknown",
                        "classification": "Unknown",
                        "position": "Unknown",
                        "ip_addresses": [],
                        "sub_items": []
                    }
                    
                    # Get item type
                    try:
                        if hasattr(item, 'TypeIdentifier'):
                            item_info["type"] = str(item.TypeIdentifier)
                            print(f"{indent}  Type: {item_info['type']}")
                    except:
                        pass
                    
                    # Get classification
                    try:
                        if hasattr(item, 'Classification'):
                            classification = str(item.Classification)
                            item_info["classification"] = classification
                            print(f"{indent}  Classification: {classification}")
                    except:
                        pass
                    
                    # Get position/slot
                    try:
                        if hasattr(item, 'PositionNumber'):
                            item_info["position"] = str(item.PositionNumber)
                            print(f"{indent}  Position: {item_info['position']}")
                    except:
                        pass
                    
                    # ✅ IP ADDRESS EXTRACTION FOR CPU ITEMS
                    if item_info["classification"] == "CPU":
                        try:
                            print(f"{indent}  🔍 Attempting IP extraction for CPU")
                            
                            # Get NetworkInterface service
                            network_interface_type = System.Type.GetType(
                                "Siemens.Engineering.HW.Features.NetworkInterface, Siemens.Engineering"
                            )
                            
                            if network_interface_type:
                                get_service = item.GetType().GetMethod("GetService")
                                
                                if get_service:
                                    generic_get_service = get_service.MakeGenericMethod(network_interface_type)
                                    network_interface = generic_get_service.Invoke(item, None)
                                    
                                    if network_interface:
                                        if hasattr(network_interface, 'Nodes'):
                                            node_count = network_interface.Nodes.Count
                                            print(f"{indent}  ✅ Found {node_count} network node(s)")
                                            
                                            for node in network_interface.Nodes:
                                                # Try to get IP address
                                                try:
                                                    if hasattr(node, 'GetAttribute'):
                                                        for attr_name in ['Address', 'IpAddress', 'IP']:
                                                            try:
                                                                value = node.GetAttribute(attr_name)
                                                                if value:
                                                                    value_str = str(value)
                                                                    if value_str not in item_info["ip_addresses"]:
                                                                        item_info["ip_addresses"].append(value_str)
                                                                        print(f"{indent}    ✅ IP: {value_str}")
                                                            except:
                                                                pass
                                                except Exception as node_error:
                                                    pass
                        
                        except Exception as ip_error:
                            print(f"{indent}  ⚠️ IP extraction error: {ip_error}")
                    
                    # ✅ RECURSIVELY PROCESS SUB-ITEMS
                    try:
                        if hasattr(item, 'DeviceItems') and item.DeviceItems.Count > 0:
                            sub_count = item.DeviceItems.Count
                            print(f"{indent}  ⮑ Sub-items: {sub_count}")
                            for sub_item in item.DeviceItems:
                                sub_item_info = process_device_item(sub_item, indent_level + 2)
                                item_info["sub_items"].append(sub_item_info)
                    except Exception as sub_error:
                        print(f"{indent}  ⚠️ Could not process sub-items: {sub_error}")
                    
                    return item_info
                
                # Process all top-level device items
                for item in device.DeviceItems:
                    item_info = process_device_item(item, indent_level=2)
                    device_info["items"].append(item_info)
                
                hardware_data["devices"].append(device_info)
            
            hardware_data["total_devices"] = len(hardware_data["devices"])
            
            # Save to JSON
            hardware_file = data_path("plc_hardware.json")
            with open(hardware_file, 'w', encoding='utf-8') as f:
                json.dump(hardware_data, f, indent=2, ensure_ascii=False)
            print(f"\n  ✓ Hardware config saved to: {hardware_file}")
            
            print(f"✅ Hardware configuration retrieved")
            return hardware_data
            
        except Exception as e:
            print(f"❌ Error fetching hardware: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}
    
    def _extract_device_info(self, device):
        """Extract detailed device information"""
        device_info = {
            "name": str(device.Name),
            "type": str(device.TypeIdentifier) if hasattr(device, 'TypeIdentifier') else "Unknown",
            "items": [],
            "network_interfaces": []
        }
        
        try:
            for item in device.DeviceItems:
                item_info = self._extract_device_item_info(item)
                device_info["items"].append(item_info)
        except Exception as e:
            print(f"⚠️ Error extracting device items: {e}")
        
        return device_info
    
    def _extract_device_item_info(self, item):
        """Extract detailed device item information"""
        item_info = {
            "name": str(item.Name),
            "type": str(item.TypeIdentifier) if hasattr(item, 'TypeIdentifier') else "Unknown",
            "classification": str(item.Classification) if hasattr(item, 'Classification') else "Unknown",
            "addresses": []
        }
        
        try:
            network_interface_type = System.Type.GetType(
                "Siemens.Engineering.HW.Features.NetworkInterface, Siemens.Engineering"
            )
            
            get_service_method = item.GetType().GetMethod("GetService")
            if get_service_method:
                generic_method = get_service_method.MakeGenericMethod(network_interface_type)
                network_interface = generic_method.Invoke(item, None)
                
                if network_interface:
                    if hasattr(network_interface, 'Nodes'):
                        for node in network_interface.Nodes:
                            node_info = self._extract_network_node_info(node)
                            item_info["addresses"].append(node_info)
        
        except Exception as e:
            print(f"⚠️ Could not get network interface: {e}")
        
        return item_info
    
    def _extract_network_node_info(self, node):
        """Extract network node information (IP addresses, etc.)"""
        node_info = {
            "interface": str(node.GetAttribute("Interface")) if hasattr(node, 'GetAttribute') else "Unknown",
            "addresses": {}
        }
        
        try:
            if hasattr(node, 'GetAttribute'):
                try:
                    ip_address = node.GetAttribute("Address")
                    if ip_address:
                        node_info["addresses"]["ip"] = str(ip_address)
                except:
                    pass
                
                try:
                    subnet = node.GetAttribute("SubnetMask")
                    if subnet:
                        node_info["addresses"]["subnet"] = str(subnet)
                except:
                    pass
                
                try:
                    router = node.GetAttribute("RouterAddress")
                    if router:
                        node_info["addresses"]["router"] = str(router)
                except:
                    pass
        
        except Exception as e:
            print(f"⚠️ Could not extract network node info: {e}")
        
        return node_info
    
    def get_plc_ip_address(self):
        """Get PLC IP address from hardware configuration with fallback methods"""
        
        # Method 1: Try to get from hardware configuration
        hardware = self.get_hardware_configuration()
        
        if "error" not in hardware:
            all_ips = []
            
            for device in hardware.get("devices", []):
                for item in device.get("items", []):
                    for ip in item.get("ip_addresses", []):
                        all_ips.append({
                            "device": device["name"],
                            "item": item["name"],
                            "ip": ip,
                            "source": "hardware_config"
                        })
            
            if all_ips:
                return {
                    "ip_addresses": all_ips,
                    "count": len(all_ips),
                    "primary_ip": all_ips[0]["ip"],
                    "method": "hardware_configuration"
                }
        
        # Method 2: Try to get from device items directly
        if self.connected:
            try:
                print("🔄 Attempting direct device IP extraction...")
                all_ips = []
                
                for device in self.project.Devices:
                    device_name = str(device.Name)
                    
                    for item in device.DeviceItems:
                        item_name = str(item.Name)
                        
                        # Only check CPU items
                        if hasattr(item, 'Classification'):
                            classification = str(item.Classification)
                            if classification != "CPU":
                                continue
                        
                        print(f"  Checking CPU: {item_name}")
                        
                        # Try to get network interface
                        try:
                            network_interface_type = System.Type.GetType(
                                "Siemens.Engineering.HW.Features.NetworkInterface, Siemens.Engineering"
                            )
                            
                            if network_interface_type:
                                get_service = item.GetType().GetMethod("GetService")
                                if get_service:
                                    generic_get_service = get_service.MakeGenericMethod(network_interface_type)
                                    network_interface = generic_get_service.Invoke(item, None)
                                    
                                    if network_interface and hasattr(network_interface, 'Nodes'):
                                        for node in network_interface.Nodes:
                                            # Try multiple attribute names
                                            for attr_name in ['Address', 'IpAddress', 'IP', 'NetworkAddress']:
                                                try:
                                                    if hasattr(node, 'GetAttribute'):
                                                        ip = node.GetAttribute(attr_name)
                                                        if ip:
                                                            ip_str = str(ip)
                                                            print(f"    ✅ Found IP: {ip_str}")
                                                            all_ips.append({
                                                                "device": device_name,
                                                                "item": item_name,
                                                                "ip": ip_str,
                                                                "source": "direct_extraction"
                                                            })
                                                            break
                                                except:
                                                    continue
                        except Exception as e:
                            print(f"    ⚠️ Error extracting IP: {e}")
                
                if all_ips:
                    return {
                        "ip_addresses": all_ips,
                        "count": len(all_ips),
                        "primary_ip": all_ips[0]["ip"],
                        "method": "direct_extraction"
                    }
            
            except Exception as e:
                print(f"❌ Direct extraction failed: {e}")
        
        # Method 3: Return PLC_IP from settings as fallback
        try:
            from .settings import get_plc_config
            plc_config = get_plc_config()
            
            if plc_config and "ip" in plc_config:
                fallback_ip = plc_config["ip"]
                print(f"ℹ️ Using IP from settings (PLC_IP): {fallback_ip}")
                
                return {
                    "ip_addresses": [{
                        "device": "Unknown",
                        "item": "PLC",
                        "ip": fallback_ip,
                        "source": "plc_config_fallback"
                    }],
                    "count": 1,
                    "primary_ip": fallback_ip,
                    "method": "config_file_fallback",
                    "note": "IP address retrieved from settings (PLC_IP) - TIA Portal extraction failed"
                }
        except Exception as e:
            print(f"⚠️ Config fallback failed: {e}")
        
        # No IP found
        return {
            "error": "Could not retrieve IP address from TIA Portal or configuration",
            "ip_addresses": [],
            "count": 0,
            "primary_ip": None,
            "method": "none",
            "troubleshooting": [
                "1. Ensure PLC has IP address configured in TIA Portal",
                "2. Check Device Configuration → Properties → PROFINET interface",
                "3. Verify network interface is configured",
                "4. Set PLC_IP in .env as fallback"
            ]
        }
    
    # ============================================
    # NETWORK CONFIGURATION
    # ============================================
    
    def get_network_configuration(self):
        """Get complete network configuration"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            network_config = {
                "subnets": [],
                "connections": [],
                "timestamp": datetime.now().isoformat()
            }
            
            if hasattr(self.project, 'Subnets'):
                for subnet in self.project.Subnets:
                    subnet_info = {
                        "name": str(subnet.Name),
                        "type": str(subnet.Type) if hasattr(subnet, 'Type') else "Unknown",
                        "devices": []
                    }
                    
                    if hasattr(subnet, 'ConnectedDevices'):
                        for device in subnet.ConnectedDevices:
                            subnet_info["devices"].append(str(device.Name))
                    
                    network_config["subnets"].append(subnet_info)
            
            return network_config
            
        except Exception as e:
            return {"error": str(e)}
    
    def get_profinet_configuration(self):
        """Get PROFINET-specific configuration"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            profinet_config = {
                "io_systems": [],
                "devices": [],
                "timestamp": datetime.now().isoformat()
            }
            
            return profinet_config
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # ALARM CONFIGURATION
    # ============================================
    
    def get_alarm_configuration(self):
        """Get alarm configuration"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            alarm_config = {
                "alarm_classes": [],
                "alarm_texts": [],
                "count": 0,
                "timestamp": datetime.now().isoformat()
            }
            
            if self.plc_software:
                try:
                    if hasattr(self.plc_software, 'AlarmTextLists'):
                        for alarm_list in self.plc_software.AlarmTextLists:
                            list_info = {
                                "name": str(alarm_list.Name),
                                "texts": []
                            }
                            
                            for alarm_text in alarm_list.Texts:
                                text_info = {
                                    "number": alarm_text.Number if hasattr(alarm_text, 'Number') else "N/A",
                                    "text": str(alarm_text.GetText()) if hasattr(alarm_text, 'GetText') else "N/A"
                                }
                                list_info["texts"].append(text_info)
                            
                            alarm_config["alarm_texts"].append(list_info)
                            alarm_config["count"] += len(list_info["texts"])
                
                except Exception as e:
                    print(f"⚠️ Could not access alarm texts: {e}")
            
            return alarm_config
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # HMI INTELLIGENCE
    # ============================================
    
    def get_hmi_configuration(self):
        """Get HMI/SCADA configuration"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            hmi_config = {
                "hmi_devices": [],
                "screens": [],
                "connections": [],
                "timestamp": datetime.now().isoformat()
            }
            
            for device in self.project.Devices:
                device_type = str(device.TypeIdentifier) if hasattr(device, 'TypeIdentifier') else ""
                
                if "HMI" in device_type or "Panel" in device_type or "WinCC" in device_type:
                    hmi_info = {
                        "name": str(device.Name),
                        "type": device_type,
                        "screens": []
                    }
                    
                    try:
                        for item in device.DeviceItems:
                            software_container_type = System.Type.GetType(
                                "Siemens.Engineering.HW.Features.SoftwareContainer, Siemens.Engineering"
                            )
                            
                            get_service_method = item.GetType().GetMethod("GetService")
                            generic_method = get_service_method.MakeGenericMethod(software_container_type)
                            software_container = generic_method.Invoke(item, None)
                            
                            if software_container and hasattr(software_container, 'Software'):
                                hmi_software = software_container.Software
                                
                                if hasattr(hmi_software, 'Screens'):
                                    for screen in hmi_software.Screens:
                                        hmi_info["screens"].append(str(screen.Name))
                    
                    except Exception as e:
                        print(f"⚠️ Could not access HMI software: {e}")
                    
                    hmi_config["hmi_devices"].append(hmi_info)
            
            return hmi_config
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # TECHNOLOGY OBJECTS
    # ============================================
    
    def get_technology_objects(self):
        """Get technology objects (PID, Motion, etc.)"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            tech_objects = {
                "objects": [],
                "count": 0,
                "timestamp": datetime.now().isoformat()
            }
            
            if self.plc_software:
                try:
                    if hasattr(self.plc_software, 'TechnologyObjectFolder'):
                        tech_folder = self.plc_software.TechnologyObjectFolder
                        
                        for tech_obj in tech_folder.TechnologyObjects:
                            obj_info = {
                                "name": str(tech_obj.Name),
                                "type": str(tech_obj.TypeIdentifier) if hasattr(tech_obj, 'TypeIdentifier') else "Unknown"
                            }
                            tech_objects["objects"].append(obj_info)
                    
                    tech_objects["count"] = len(tech_objects["objects"])
                
                except Exception as e:
                    print(f"⚠️ Could not access technology objects: {e}")
            
            return tech_objects
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # WATCH TABLES
    # ============================================
    
    def get_watch_tables(self):
        """Get watch tables"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        try:
            watch_tables = {
                "tables": [],
                "count": 0,
                "timestamp": datetime.now().isoformat()
            }
            
            if self.plc_software:
                try:
                    if hasattr(self.plc_software, 'WatchTableGroup'):
                        watch_group = self.plc_software.WatchTableGroup
                        
                        for watch_table in watch_group.WatchTables:
                            table_info = {
                                "name": str(watch_table.Name),
                                "entries": []
                            }
                            
                            for entry in watch_table.Entries:
                                entry_info = {
                                    "name": str(entry.Name) if hasattr(entry, 'Name') else "N/A",
                                    "address": str(entry.Address) if hasattr(entry, 'Address') else "N/A"
                                }
                                table_info["entries"].append(entry_info)
                            
                            watch_tables["tables"].append(table_info)
                    
                    watch_tables["count"] = len(watch_tables["tables"])
                
                except Exception as e:
                    print(f"⚠️ Could not access watch tables: {e}")
            
            return watch_tables
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # BLOCK STRUCTURE INTELLIGENCE
    # ============================================
    
    def get_all_blocks(self, force_refresh=False):
        """Get all blocks with metadata from TIA Portal or JSON cache"""
        
        # ⭐ USE JSON CACHE MODE
        if self.use_cached_json:
            if self.block_cache:
                print(f"📦 Using blocks from JSON cache ({self.block_cache.get('count', 0)} blocks)")
                return self.block_cache
            else:
                return {"error": "No cached block data available", "count": 0, "blocks": []}
        
        # ⭐ USE LIVE TIA CONNECTION
        if not self.connected:
            return {"error": "Not connected to TIA Portal", "details": self.connection_error}
        
        current_time = time.time()
        cache_age = current_time - self.last_block_update
        
        if not force_refresh and self.block_cache and cache_age < self.cache_duration:
            print(f"📦 Using cached blocks ({cache_age:.1f}s old)")
            return self.block_cache
        
        with self.lock:
            try:
                print(f"🔄 Fetching fresh block data from TIA Portal...")
                blocks = []
                block_group = self.plc_software.BlockGroup
                
                for block in block_group.Blocks:
                    block_info = {
                        "name": str(block.Name),
                        "type": self._get_block_type(block),
                        "number": None,
                        "language": "N/A",
                        "comment": ""
                    }
                    
                    try:
                        if hasattr(block, 'Number'):
                            block_info["number"] = int(block.Number)
                    except:
                        pass
                    
                    try:
                        if hasattr(block, 'ProgrammingLanguage'):
                            block_info["language"] = str(block.ProgrammingLanguage)
                    except:
                        pass
                    
                    try:
                        if hasattr(block, 'Comment') and block.Comment:
                            comment_obj = block.Comment
                            cultures = comment_obj.Cultures
                            if cultures.Count > 0:
                                first_culture = cultures[0]
                                comment_text = comment_obj.GetText(first_culture)
                                if comment_text:
                                    block_info["comment"] = str(comment_text)
                    except:
                        pass
                    
                    blocks.append(block_info)
                
                self.block_cache = {
                    "blocks": blocks, 
                    "count": len(blocks), 
                    "timestamp": datetime.now().isoformat(),
                    "cache_age": 0
                }
                self.last_block_update = current_time
                
                print(f"✅ Loaded {len(blocks)} blocks from TIA Portal")
                
                block_names = [b['name'] for b in blocks]
                print(f"📋 Block names: {block_names}")
                
                return self.block_cache
                
            except Exception as e:
                print(f"❌ Error fetching blocks: {e}")
                return {"error": str(e)}
    
    def _get_block_type(self, block):
        """Get human-readable block type"""
        type_name = str(block.GetType().Name)
        
        type_map = {
            "OrganizationBlock": "OB",
            "FunctionBlock": "FB",
            "Function": "FC",
            "DataBlock": "DB",
            "GlobalDB": "GlobalDB"
        }
        
        for key, value in type_map.items():
            if key in type_name:
                return value
        
        return type_name
    
    def find_block(self, block_name):
        """Find a specific block by name"""
        blocks_data = self.get_all_blocks()
        
        if "error" in blocks_data:
            return blocks_data
        
        for block in blocks_data["blocks"]:
            if block["name"].lower() == block_name.lower():
                return {
                    "block": block,
                    "found": True
                }
        
        return {"found": False, "error": f"Block '{block_name}' not found"}
    
    def get_program_structure(self):
        """Get complete program structure with dependencies"""
        blocks_data = self.get_all_blocks()
        
        if "error" in blocks_data:
            return blocks_data
        
        structure = {
            "organization_blocks": [],
            "function_blocks": [],
            "functions": [],
            "data_blocks": [],
            "other": []
        }
        
        for block in blocks_data["blocks"]:
            block_type = block["type"]
            
            if block_type == "OB":
                structure["organization_blocks"].append(block)
            elif block_type == "FB":
                structure["function_blocks"].append(block)
            elif block_type == "FC":
                structure["functions"].append(block)
            elif block_type == "DB" or block_type == "GlobalDB":
                structure["data_blocks"].append(block)
            else:
                structure["other"].append(block)
        
        return {
            "structure": structure,
            "summary": {
                "total_blocks": blocks_data["count"],
                "organization_blocks": len(structure["organization_blocks"]),
                "function_blocks": len(structure["function_blocks"]),
                "functions": len(structure["functions"]),
                "data_blocks": len(structure["data_blocks"])
            },
            "timestamp": datetime.now().isoformat()
        }
    
    # ============================================
    # DIAGNOSTIC INTELLIGENCE
    # ============================================
    
    def diagnose_tag_issue(self, tag_name):
        """Diagnose issues with a specific tag"""
        tag_info = self.get_tag_metadata(tag_name)
        
        if not tag_info.get("found"):
            return tag_info
        
        tag = tag_info["tag"]
        
        diagnosis = {
            "tag_name": tag["name"],
            "status": "analyzed",
            "findings": [],
            "recommendations": []
        }
        
        if not tag["comment"]:
            diagnosis["findings"].append("⚠️ Tag has no comment")
            diagnosis["recommendations"].append("Add descriptive comment in TIA Portal")
        
        if tag["address"] == "N/A":
            diagnosis["findings"].append("⚠️ No logical address assigned")
        else:
            diagnosis["findings"].append(f"✅ Address: {tag['address']}")
        
        diagnosis["findings"].append(f"📊 Data type: {tag['data_type']}")
        
        return diagnosis
    
    def analyze_program_health(self):
        """Analyze overall program health"""
        tags_data = self.get_all_tags()
        blocks_data = self.get_all_blocks()
        
        if "error" in tags_data or "error" in blocks_data:
            return {"error": "Cannot analyze - data unavailable"}
        
        health_report = {
            "overall_status": "healthy",
            "metrics": {
                "total_tags": tags_data["count"],
                "total_blocks": blocks_data["count"],
                "documented_tags": 0,
                "undocumented_tags": 0,
                "documented_blocks": 0,
                "undocumented_blocks": 0
            },
            "issues": [],
            "recommendations": []
        }
        
        for tag in tags_data["tags"]:
            if tag["comment"]:
                health_report["metrics"]["documented_tags"] += 1
            else:
                health_report["metrics"]["undocumented_tags"] += 1
        
        for block in blocks_data["blocks"]:
            if block.get("comment"):
                health_report["metrics"]["documented_blocks"] += 1
            else:
                health_report["metrics"]["undocumented_blocks"] += 1
        
        if tags_data["count"] > 0:
            doc_percentage = (health_report["metrics"]["documented_tags"] / tags_data["count"]) * 100
            
            if doc_percentage < 50:
                health_report["issues"].append(f"⚠️ Only {doc_percentage:.1f}% of tags are documented")
                health_report["recommendations"].append("Improve tag documentation")
                health_report["overall_status"] = "needs_improvement"
            else:
                health_report["issues"].append(f"✅ {doc_percentage:.1f}% of tags are documented")
        
        if blocks_data["count"] > 0:
            block_doc_percentage = (health_report["metrics"]["documented_blocks"] / blocks_data["count"]) * 100
            
            if block_doc_percentage < 50:
                health_report["issues"].append(f"⚠️ Only {block_doc_percentage:.1f}% of blocks are documented")
                health_report["recommendations"].append("Improve block documentation")
                health_report["overall_status"] = "needs_improvement"
            else:
                health_report["issues"].append(f"✅ {block_doc_percentage:.1f}% of blocks are documented")
        
        return health_report
    
    # ============================================
    # PROJECT INFORMATION
    # ============================================
    
    def get_project_info(self):
        """Get project information"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal", "details": self.connection_error}
        
        try:
            info = {
                "project_name": str(self.project.Name),
                "project_path": str(self.project.Path),
                "plc_name": str(self.plc_software.Name) if self.plc_software else "N/A",
                "connection_status": "connected",
                "tia_version": TIA_VERSION,
                "timestamp": datetime.now().isoformat()
            }
            
            try:
                if hasattr(self.project, 'Version'):
                    info["project_version"] = str(self.project.Version)
            except:
                pass
            
            info["cache_status"] = {
                "tags_cached": self.tag_cache is not None,
                "blocks_cached": self.block_cache is not None,
                "tags_count": self.tag_cache.get("count", 0) if self.tag_cache else 0,
                "blocks_count": self.block_cache.get("count", 0) if self.block_cache else 0
            }
            
            return info
            
        except Exception as e:
            return {"error": str(e)}
    
    # ============================================
    # COMPLETE PROJECT DATA
    # ============================================
    
    def get_complete_project_data(self):
        """Get ALL available project data in one call"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal"}
        
        print("🔄 Gathering complete project data...")
        
        complete_data = {
            "project_info": self.get_project_info(),
            "hardware": self.get_hardware_configuration(),
            "network": self.get_network_configuration(),
            "ip_addresses": self.get_plc_ip_address(),
            "tags": self.get_all_tags(),
            "blocks": self.get_all_blocks(),
            "program_structure": self.get_program_structure(),
            "alarms": self.get_alarm_configuration(),
            "hmi": self.get_hmi_configuration(),
            "technology_objects": self.get_technology_objects(),
            "watch_tables": self.get_watch_tables(),
            "health": self.analyze_program_health(),
            "timestamp": datetime.now().isoformat()
        }
        
        print("✅ Complete project data gathered")
        return complete_data
    
    # ============================================
    # UTILITY FUNCTIONS
    # ============================================
    
    def refresh_all_data(self):
        """Force refresh all cached data"""
        if not self.connected:
            return {"error": "Not connected to TIA Portal", "details": self.connection_error}
        
        print("🔄 Refreshing all TIA data...")
        
        self.tag_cache = None
        self.block_cache = None
        self.last_tag_update = 0
        self.last_block_update = 0
        
        tags_result = self.get_all_tags(force_refresh=True)
        blocks_result = self.get_all_blocks(force_refresh=True)
        
        print("✅ Data refresh complete")
        
        return {
            "status": "refreshed",
            "tags": tags_result.get("count", 0),
            "blocks": blocks_result.get("count", 0),
            "timestamp": datetime.now().isoformat(),
            "errors": {
                "tags": "error" in tags_result,
                "blocks": "error" in blocks_result
            }
        }
    
    def get_status(self):
        """Get TIA Intelligence module status with connection mode info"""
        return {
            "connected": self.connected,
            "connection_mode": self.connection_mode,  # ⭐ NEW: Shows ATTACHED/EXCLUSIVE/JSON_CACHE
            "using_json_cache": self.use_cached_json,  # ⭐ NEW: Shows if reading from files
            "tia_available": TIA_AVAILABLE,
            "project_connected": self.project is not None,
            "plc_software_found": self.plc_software is not None,
            "cached_tags": self.tag_cache.get("count", 0) if self.tag_cache else 0,
            "cached_blocks": self.block_cache.get("count", 0) if self.block_cache else 0,
            "last_tag_update": datetime.fromtimestamp(self.last_tag_update).isoformat() if self.last_tag_update > 0 else "never",
            "last_block_update": datetime.fromtimestamp(self.last_block_update).isoformat() if self.last_block_update > 0 else "never",
            "cache_age_tags": time.time() - self.last_tag_update if self.last_tag_update > 0 else None,
            "cache_age_blocks": time.time() - self.last_block_update if self.last_block_update > 0 else None,
            "connection_error": self.connection_error
        }
    
    def disconnect(self):
        """Disconnect from TIA Portal and cleanup resources"""
        try:
            print("\n🔄 Cleaning up TIA Portal connection...")
            
            if self.project:
                print("  Closing project...")
                self.project.Close()
                print("  ✓ Project closed")
                self.project = None
            
            if self.tia_portal:
                print("  Disconnecting from TIA Portal...")
                self.tia_portal.Dispose()
                print("  ✓ Disconnected")
                self.tia_portal = None
            
            self.connected = False
            self.plc_software = None
            
            # Clear caches
            self.tag_cache = None
            self.block_cache = None
            self.last_tag_update = 0
            self.last_block_update = 0
            
            print("✅ Cleanup completed successfully")
            
        except Exception as e:
            print(f"⚠️ Warning during cleanup: {e}")
            import traceback
            traceback.print_exc()

# ============================================
# SINGLETON INSTANCE
# ============================================

_tia_intelligence = None
_tia_lock = threading.Lock()

def get_tia_intelligence():
    """Get or create TIA Intelligence singleton instance"""
    global _tia_intelligence
    
    with _tia_lock:
        if _tia_intelligence is None:
            _tia_intelligence = TIAIntelligence()
            
            if TIA_AVAILABLE:
                _tia_intelligence.connect()
            else:
                print("⚠️ TIA Openness not available - intelligence module disabled")
        
        return _tia_intelligence

def close_tia_intelligence():
    """Close TIA Intelligence connection"""
    global _tia_intelligence
    
    with _tia_lock:
        if _tia_intelligence:
            _tia_intelligence.disconnect()
            _tia_intelligence = None

# ============================================
# STANDALONE TEST
# ============================================

if __name__ == "__main__":
    print("🧪 Testing TIA Intelligence Module...")
    
    tia = get_tia_intelligence()
    
    if tia.connected:
        print("\n📊 Project Info:")
        print(json.dumps(tia.get_project_info(), indent=2))
        
        print("\n📋 Tags:")
        tags = tia.get_all_tags()
        if "error" not in tags:
            print(f"Found {tags.get('count', 0)} tags")
            if tags.get('count', 0) > 0:
                print(f"Sample tags: {[t['name'] for t in tags['tags'][:5]]}")
        
        print("\n🔧 Blocks:")
        blocks = tia.get_all_blocks()
        if "error" not in blocks:
            print(f"Found {blocks.get('count', 0)} blocks")
            if blocks.get('count', 0) > 0:
                print(f"Block names: {[b['name'] for b in blocks['blocks']]}")
        
        print("\n🏗️ Program Structure:")
        structure = tia.get_program_structure()
        if "error" not in structure:
            print(json.dumps(structure.get('summary', {}), indent=2))
        
        print("\n💊 Health Analysis:")
        health = tia.analyze_program_health()
        if "error" not in health:
            print(json.dumps(health, indent=2))
        
        print("\n🔄 Testing cache refresh:")
        refresh_result = tia.refresh_all_data()
        print(json.dumps(refresh_result, indent=2))
        
        # ✅ NEW: Test hardware configuration
        print("\n🔌 Hardware Configuration:")
        hardware = tia.get_hardware_configuration()
        if "error" not in hardware:
            print(f"Found {len(hardware['devices'])} devices")
        
        # ✅ NEW: Test IP address retrieval
        print("\n🌐 IP Addresses:")
        ip_data = tia.get_plc_ip_address()
        if "error" not in ip_data:
            print(f"Primary IP: {ip_data.get('primary_ip', 'N/A')}")
            for ip_info in ip_data.get('ip_addresses', []):
                print(f"  - {ip_info['device']}/{ip_info['item']}: {ip_info['ip']}")
        
    else:
        print(f"\n❌ Not connected to TIA Portal")
        print(f"Error: {tia.connection_error}")
    
    close_tia_intelligence()
    print("\n✅ Test complete")
