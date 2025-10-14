"""
Voice Assistant - Auto-start Background Listening
Listens for "copilot" wake word automatically on startup
"""
import speech_recognition as sr
import requests
import time
import json
import threading
import os

from .settings import data_path

VOICE_STATUS_FILE = data_path("voice_status.json")

class VoiceAssistant:
    def __init__(self, wake_word="copilot", api_url="http://127.0.0.1:8000"):
        self.wake_word = wake_word.lower()
        self.api_url = api_url
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.is_running = False
        self.listen_thread = None
        self.session_id = "voice_session_1"
        
        # Adjust for ambient noise on startup
        print("🎤 Calibrating microphone for ambient noise...")
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)
        print("✅ Microphone ready!")
        
    def start(self):
        """Start listening for wake word in background thread"""
        if self.is_running:
            print("⚠️ Voice assistant already running")
            return
            
        self.is_running = True
        
        # ⭐ CLEAR OLD COMMAND DATA on startup
        self._clear_command_history()
        
        self.listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.listen_thread.start()
        print(f"🎤 Voice assistant started! Say '{self.wake_word}' to give commands...")
        
        # Notify frontend that voice assistant is active
        self._notify_frontend_status(True)
        
    def stop(self):
        """Stop listening"""
        self.is_running = False
        if self.listen_thread:
            self.listen_thread.join(timeout=2)
        print("🔇 Voice assistant stopped")
        
        # Notify frontend that voice assistant is inactive
        self._notify_frontend_status(False)
    
    def _clear_command_history(self):
        """Clear old command history from status file"""
        try:
            status_file = VOICE_STATUS_FILE
            status = {
                "active": False,
                "listening": False,
                "timestamp": time.time(),
                "last_command": None,
                "last_response": None,
                "command_timestamp": None
            }
            with open(status_file, 'w') as f:
                json.dump(status, f)
            print("🧹 Cleared old voice command history")
        except Exception as e:
            print(f"⚠️ Could not clear command history: {e}")
        
    def _listen_loop(self):
        """Continuous listening loop for wake word"""
        while self.is_running:
            try:
                with self.microphone as source:
                    print("👂 Listening for wake word...")
                    # Listen with shorter timeout for better responsiveness
                    audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=3)
                    
                try:
                    # Recognize speech using Google Speech Recognition
                    text = self.recognizer.recognize_google(audio).lower()
                    print(f"🔊 Heard: '{text}'")
                    
                    # Check if wake word is in the text
                    if self.wake_word in text:
                        print(f"✅ Wake word detected!")
                        self._notify_frontend_status(True, listening=True)
                        
                        # Extract command after wake word
                        command = text.split(self.wake_word, 1)[-1].strip()
                        
                        if command:
                            # Command provided with wake word
                            print(f"📝 Command: '{command}'")
                            self._send_to_chatbot(command)
                        else:
                            # No command yet, listen for it
                            print("🎤 Listening for command...")
                            command = self._listen_for_command()
                            if command:
                                self._send_to_chatbot(command)
                        
                        self._notify_frontend_status(True, listening=False)
                        
                except sr.UnknownValueError:
                    # Speech not understood - continue listening
                    pass
                except sr.RequestError as e:
                    print(f"⚠️ Speech recognition error: {e}")
                    time.sleep(1)
                    
            except sr.WaitTimeoutError:
                # Timeout - continue listening
                pass
            except Exception as e:
                print(f"❌ Error in listen loop: {e}")
                time.sleep(1)
                
    def _listen_for_command(self):
        """Listen for the actual command after wake word"""
        try:
            with self.microphone as source:
                print("👂 Listening for your command...")
                audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                
            command = self.recognizer.recognize_google(audio)
            print(f"📝 Command received: '{command}'")
            return command
            
        except sr.WaitTimeoutError:
            print("⏱️ Command timeout")
            return None
        except sr.UnknownValueError:
            print("⚠️ Could not understand command")
            return None
        except Exception as e:
            print(f"❌ Error listening for command: {e}")
            return None
            
    def _send_to_chatbot(self, command):
        """Send command to the chatbot API"""
        try:
            print(f"📤 Sending to chatbot: '{command}'")
            
            # Notify frontend that command was received
            self._notify_frontend_command(command, None)
            
            response = requests.post(
                f"{self.api_url}/api/command",
                json={
                    "command": command,
                    "session_id": self.session_id
                },
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                chatbot_response = result.get('response', '')
                print(f"✅ Chatbot response: {chatbot_response[:100]}...")
                
                # Notify frontend with command and response
                self._notify_frontend_command(command, chatbot_response)
            else:
                print(f"⚠️ API error: {response.status_code}")
                self._notify_frontend_command(command, f"Error: {response.status_code}")
                
        except Exception as e:
            print(f"❌ Error sending to chatbot: {e}")
            self._notify_frontend_command(command, f"Error: {str(e)}")
            
    def _notify_frontend_command(self, command, response):
        """Notify frontend of command and response"""
        try:
            status_file = VOICE_STATUS_FILE
            status = {
                "active": True,
                "listening": False,
                "timestamp": time.time(),
                "last_command": command,
                "last_response": response,
                "command_timestamp": time.time()
            }
            with open(status_file, 'w') as f:
                json.dump(status, f)
        except Exception as e:
            print(f"⚠️ Could not update status file: {e}")
            
    def _notify_frontend_status(self, active, listening=False):
        """Notify frontend of voice assistant status"""
        try:
            status_file = VOICE_STATUS_FILE
            # Read existing status to preserve command history
            existing_status = {}
            if os.path.exists(status_file):
                try:
                    with open(status_file, 'r') as f:
                        existing_status = json.load(f)
                except:
                    pass
            
            status = {
                "active": active,
                "listening": listening,
                "timestamp": time.time(),
                "last_command": existing_status.get("last_command"),
                "last_response": existing_status.get("last_response"),
                "command_timestamp": existing_status.get("command_timestamp")
            }
            with open(status_file, 'w') as f:
                json.dump(status, f)
        except Exception as e:
            print(f"⚠️ Could not update status file: {e}")


# Global voice assistant instance
voice_assistant = None

def start_voice_assistant():
    """Start the voice assistant"""
    global voice_assistant
    if voice_assistant is None:
        voice_assistant = VoiceAssistant()
    voice_assistant.start()
    return voice_assistant

def stop_voice_assistant():
    """Stop the voice assistant"""
    global voice_assistant
    if voice_assistant:
        voice_assistant.stop()

def get_voice_assistant():
    """Get the voice assistant instance"""
    return voice_assistant
