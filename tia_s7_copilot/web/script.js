document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const userInput = document.getElementById('userInput');
    const sendButton = document.getElementById('sendButton');
    const chatMessages = document.getElementById('chatMessages');
    const plcStatusList = document.getElementById('plcStatusList');
    const connectionStatusDot = document.querySelector('#connectionStatus .dot');
    const connectionText = document.getElementById('connectionText');
    const chatForm = document.getElementById('chatForm');
    const API_BASE_URL = 'http://localhost:8000/api';
    const micButton = document.getElementById('micButton');

    // ⭐ SESSION MANAGEMENT - Generate NEW session on each page load (refresh = new chat)
    let sessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    console.log('📝 Chat Session ID:', sessionId);

    // ⭐ CLEAR CHAT ON PAGE LOAD - Ensure fresh start
    if (chatMessages) {
        chatMessages.innerHTML = '';
    }

    let isLoading = false;
    let currentLoadingBubble = null;
    let energyData = null;
    let lastProxSensorStates = {};
    let proxSensorData = null;

    // ⭐ STEP TRACKING VARIABLES
    let stepUpdateInterval;

    // ⭐ ANTI-FLICKERING VARIABLES
    let lastPLCUpdate = {};
    let lastEnergyUpdate = {};
    let lastRobotState = {};
    let updateTimeout;

    // Keys for Sidebar
    const plcKeys = ["Start", "Stop", "Arm_1", "Arm_2", "Arm_3", "Conveyor_1", "Conveyor_2", "Total_Prod"];

    const plcKeyMap = {
        "arm_1": "Arm_1",
        "arm_2": "Arm_2",
        "arm_3": "Arm_3",
        "conveyor_1": "Conveyor_1",
        "conveyor_2": "Conveyor_2",
        "production_count": "Total_Prod",
        "start": "Start",
        "stop": "Stop"
    };

    // ---- MICROPHONE FUNCTIONALITY ----
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (SpeechRecognition) {
        const recognition = new SpeechRecognition();
        recognition.continuous = false;
        recognition.interimResults = true;
        recognition.lang = 'en-US';

        micButton.addEventListener('click', () => {
            recognition.start();
            micButton.disabled = true;
        });

        recognition.addEventListener('result', (event) => {
            let interimTranscript = '';
            let finalTranscript = '';

            for (let i = 0; i < event.results.length; ++i) {
                if (event.results[i].isFinal) {
                    finalTranscript += event.results[i][0].transcript;
                } else {
                    interimTranscript += event.results[i][0].transcript;
                }
            }

            userInput.value = finalTranscript + interimTranscript;
        });

        recognition.addEventListener('end', () => {
            micButton.disabled = false;
        });

        recognition.addEventListener('error', (event) => {
            alert('Mic error: ' + event.error);
            micButton.disabled = false;
        });
    } else {
        micButton.addEventListener('click', () => {
            alert('Speech Recognition not supported in this browser.');
        });
    }

    // ⭐ HELPER FUNCTIONS
    function formatStepName(stepName) {
        if (!stepName || stepName === 'IDLE') return 'IDLE';
        return stepName.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, l => l.toUpperCase());
    }

    function getProgressClass(progress) {
        if (progress === 0) return 'idle';
        if (progress < 30) return 'low';
        if (progress < 70) return 'medium';
        return 'high';
    }

    function determineCurrentStage(arms) {
        const activeArms = Object.values(arms).filter(arm => arm.step !== 'IDLE');
        
        if (activeArms.length === 0) return 'Idle';
        if (activeArms.length === 1) return 'Single Arm Operation';
        if (activeArms.length === 2) return 'Dual Arm Coordination';
        if (activeArms.length === 3) return 'Full Assembly Line';
        
        return 'Processing';
    }

    // ⭐ DEBOUNCED UPDATE FUNCTION
    function debouncedUpdate(updateFunction, data) {
        clearTimeout(updateTimeout);
        updateTimeout = setTimeout(() => updateFunction(data), 100);
    }

    // ⭐ DATA COMPARISON HELPER
    function hasDataChanged(newData, oldData) {
        if (!oldData) return true;
        return JSON.stringify(newData) !== JSON.stringify(oldData);
    }

    // ---- SIDEBAR Initialization ----
    function initializePLCStatusList(keys) {
        plcStatusList.innerHTML = '';
        keys.forEach(key => {
            const li = document.createElement('li');
            li.id = `plc_status_${key}`;
            li.innerHTML = `
                <span>${key === 'production_count' ? 'Production Counter' : key.replace(/_/g, ' ')}</span>
                <div>
                    <strong id="plc_status_value_${key}"></strong>
                    <span class="status-indicator" id="plc_status_indicator_${key}"></span>
                </div>
            `;
            plcStatusList.appendChild(li);
        });

        // Robot Speed item
        const robotSpeedItem = document.createElement('li');
        robotSpeedItem.setAttribute('data-status', 'robot-speed');
        robotSpeedItem.innerHTML = `
            <span>Robot Speed</span>
            <div>
                <strong id="robotSpeedValue">0%</strong>
            </div>
        `;
        plcStatusList.appendChild(robotSpeedItem);

        // Operation Mode row
        const operationModeItem = document.createElement('li');
        operationModeItem.setAttribute('data-status', 'operation-mode');
        operationModeItem.innerHTML = `
            <span>Operation Mode</span>
            <div>
                <strong id="robotMode">IDLE</strong>
            </div>
        `;
        plcStatusList.appendChild(operationModeItem);
    }
    initializePLCStatusList(plcKeys);

    // ---- Connection Status ----
    function setConnectionStatus(status, message) {
        connectionStatusDot.className = 'dot ' + status;
        connectionText.textContent = message;
    }

    // ---- Message Bubbles ----
    function addMessageToChat(text, sender, type = 'normal') {
        if (currentLoadingBubble && type !== 'loading') {
            currentLoadingBubble.remove();
            currentLoadingBubble = null;
        }
        const messageBubble = document.createElement('div');
        messageBubble.classList.add('message-bubble', sender);

        if (type === 'loading') {
            messageBubble.innerHTML = `
                <div class="loading-spinner">
                    <div class="dot-spinner"></div>
                    <div class="dot-spinner"></div>
                    <div class="dot-spinner"></div>
                </div>`;
            currentLoadingBubble = messageBubble;
        } else if (sender === 'ai' || sender === 'system') {
            const html = marked.parse(text);
            messageBubble.innerHTML = window.DOMPurify ? DOMPurify.sanitize(html) : html;
        } else {
            const safeText = text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
            messageBubble.innerHTML = `<p>${safeText}</p>`;
        }

        chatMessages.appendChild(messageBubble);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    // ⭐ EXPOSE addMessageToChat to global scope for voice assistant
    window.addMessageToChat = addMessageToChat;

    // ---- PLC SIDEBAR STATUS (ANTI-FLICKERING VERSION) ----
    async function updatePLCStatus(statusData) {
        // ⭐ SKIP UPDATE IF DATA HASN'T CHANGED
        if (!hasDataChanged(statusData, lastPLCUpdate)) {
            return;
        }
        lastPLCUpdate = { ...statusData };

        // ⭐ SELECTIVE UPDATES INSTEAD OF FULL REBUILD
        let robotSpeed = '-';
        let operationMode = '-';
        let armStatuses = {
            'Arm_1': false,
            'Arm_2': false, 
            'Arm_3': false
        };

        try {
            const robotResponse = await fetch(`${API_BASE_URL}/robot_running_status`);
            if (robotResponse.ok) {
                const robotData = await robotResponse.json();
                const robots = robotData.robots || {};
                
                armStatuses['Arm_1'] = robots.arm_1?.is_moving || false;
                armStatuses['Arm_2'] = robots.arm_2?.is_moving || false;
                armStatuses['Arm_3'] = robots.arm_3?.is_moving || false;
            }
        } catch (error) {
            console.log('⚠️ Could not get robot status, using PLC fallback');
        }

        // ⭐ UPDATE ONLY CHANGED VALUES
        if (statusData && typeof statusData === 'object') {
            for (const key in statusData) {
                const normalized = key.toLowerCase().replace(/_/g, '');
                
                if (normalized === 'robotspeed') {
                    robotSpeed = Number(statusData[key]).toLocaleString();
                    continue;
                }
                if (normalized === 'operationmode') {
                    operationMode = String(statusData[key]).toUpperCase();
                    continue;
                }
                
                let value = statusData[key];
                if (key === 'Arm_1' || key === 'Arm_2' || key === 'Arm_3') {
                    value = armStatuses[key];
                }
                
                // ⭐ UPDATE ONLY IF ELEMENT EXISTS AND VALUE CHANGED
                const valueElement = document.getElementById(`plc_status_value_${key}`);
                const indicatorElement = document.getElementById(`plc_status_indicator_${key}`);
                
                if (valueElement) {
                    const newText = typeof value === 'boolean' ? (value ? 'ON' : 'OFF') : value;
                    if (valueElement.textContent !== String(newText)) {
                        valueElement.textContent = newText;
                    }
                }
                
                if (indicatorElement && typeof value === 'boolean') {
                    const newClass = `status-indicator ${value ? 'on' : 'off'}`;
                    if (indicatorElement.className !== newClass) {
                        indicatorElement.className = newClass;
                    }
                }
            }
        }

        // ⭐ UPDATE ROBOT SPEED ONLY IF CHANGED
        const robotSpeedElement = document.getElementById('robotSpeedValue');
        if (robotSpeedElement) {
            const newSpeedText = `${robotSpeed}%`;
            if (robotSpeedElement.textContent !== newSpeedText) {
                robotSpeedElement.textContent = newSpeedText;
            }
        }

        // ⭐ UPDATE OPERATION MODE ONLY IF CHANGED
        const robotModeElement = document.getElementById('robotMode');
        if (robotModeElement) {
            if (robotModeElement.textContent !== operationMode) {
                robotModeElement.textContent = operationMode;
            }
        }
    }

    // Helper for formatting speed with commas or locale if needed
    function formatSpeed(speed) {
        return Number(speed).toLocaleString();
    }

    // ---- ROBOT STATE Section (ANTI-FLICKERING VERSION) ----
    function updateRobotState() {
        fetch(`${API_BASE_URL}/robot_state`)
            .then(response => response.json())
            .then(data => {
                // ⭐ SKIP UPDATE IF DATA HASN'T CHANGED
                if (!hasDataChanged(data, lastRobotState)) {
                    return;
                }
                lastRobotState = { ...data };

                updateRobotSpeed(data.current_speed);
                updateOperationMode(data.overall_mode);
                updateMainContentRobotMode(data);
            })
            .catch(error => {
                console.error('Error updating robot state:', error);
                createFallbackRobotStatus();
            });
    }

    function updateRobotSpeed(speed) {
        const speedElement = document.getElementById('robotSpeedValue');
        if (speedElement) {
            speedElement.classList.remove('idle', 'low-speed', 'high-speed');
            let newText, newClass;
            
            if (speed === 0) {
                newText = "0%";
                newClass = 'idle';
            } else if (speed <= 50) {
                newText = speed.toLocaleString() + "%";
                newClass = 'low-speed';
            } else {
                newText = speed.toLocaleString() + "%";
                newClass = 'high-speed';
            }
            
            // ⭐ UPDATE ONLY IF CHANGED
            if (speedElement.innerHTML !== newText) {
                speedElement.innerHTML = newText;
            }
            if (newClass) {
                speedElement.classList.add(newClass);
            }
        }
    }

    function updateOperationMode(mode) {
        let operationModeItem = document.querySelector('[data-status="operation-mode"]');
        if (!operationModeItem) {
            operationModeItem = document.createElement('li');
            operationModeItem.setAttribute('data-status', 'operation-mode');
            operationModeItem.innerHTML = `
                <span>Operation Mode</span>
                <div>
                    <strong id="robotMode">IDLE</strong>
                </div>
            `;
            const robotSpeedItem = document.querySelector('[data-status="robot-speed"]');
            if (robotSpeedItem) {
                robotSpeedItem.insertAdjacentElement('afterend', operationModeItem);
            } else {
                plcStatusList.appendChild(operationModeItem);
            }
        }
        
        const modeElement = document.getElementById('robotMode');
        if (modeElement) {
            let newText;
            if (mode === 'low_speed') {
                newText = 'LOW SPEED MODE';
            } else if (mode === 'high_speed') {
                newText = 'HIGH SPEED MODE';
            } else if (mode === 'idle') {
                newText = 'IDLE';  // ← FIXED: Don't add "MODE" to IDLE
            } else {
                newText = mode.toUpperCase().replace('_', ' ');  // ← FIXED: Removed extra " + ' MODE'"
            }
            
            // ⭐ UPDATE ONLY IF CHANGED
            if (modeElement.textContent !== newText) {
                modeElement.textContent = newText;
                modeElement.classList.remove('low-speed', 'high-speed', 'idle');
                modeElement.classList.add(mode.toLowerCase().replace('_', '-'));
            }
        }
    }

    function createFallbackRobotStatus() {
        const speedElement = document.getElementById('robotSpeedValue');
        if (speedElement && speedElement.textContent !== "OFFLINE") {
            speedElement.textContent = "OFFLINE";
            speedElement.className = 'idle';
        }
        const modeElement = document.getElementById('robotMode');
        if (modeElement && modeElement.textContent !== "OFFLINE") {
            modeElement.textContent = "OFFLINE";
            modeElement.className = 'idle';
        }
    }

    function updateMainContentRobotMode(robotData) {
        const currentModeElement = document.getElementById('currentMode');
        if (currentModeElement) {
            currentModeElement.textContent = robotData.overall_mode.toUpperCase();
        }
        const currentSpeedElement = document.getElementById('currentSpeed');
        if (currentSpeedElement) {
            if (robotData.current_speed === 0) {
                currentSpeedElement.textContent = "0";
            } else if (robotData.current_speed === 100000) {
                currentSpeedElement.textContent = "1000";
            } else if (robotData.current_speed === 5000000) {
                currentSpeedElement.textContent = "50000";
            } else {
                currentSpeedElement.textContent = robotData.current_speed.toLocaleString();
            }
        }
        const robotModeStatus = document.getElementById('robotModeStatus');
        if (robotModeStatus) {
            const statusDot = robotModeStatus.querySelector('.status-dot');
            const statusText = robotModeStatus.querySelector('span:last-child');
            if (statusDot && statusText) {
                statusDot.className = 'status-dot';
                if (robotData.overall_mode === 'idle') {
                    statusDot.classList.add('idle');
                    statusText.textContent = 'Idle';
                } else {
                    statusDot.classList.add('online');
                    // ← NEW: Handle low_speed and high_speed modes
                    if (robotData.overall_mode === 'low_speed') {
                        statusText.textContent = 'Low Speed Mode';
                    } else if (robotData.overall_mode === 'high_speed') {
                        statusText.textContent = 'High Speed Mode';
                    } else {
                        statusText.textContent = robotData.overall_mode.replace('_', ' ').toUpperCase() + ' Mode';
                    }
                }
            }    
        }
        for (let i = 1; i <= 3; i++) {
            const armStatusElement = document.getElementById(`armStatus${i}`);
            if (armStatusElement) {
                const armStateSpan = armStatusElement.querySelector('.arm-state');
                if (armStateSpan) {
                    const isRunning = robotData.overall_mode !== 'idle';
                    armStateSpan.textContent = isRunning ? robotData.overall_mode.toUpperCase() : 'IDLE';
                    armStatusElement.className = 'arm-status';
                    if (isRunning) {
                        armStatusElement.classList.add('active');
                    }
                }
            }
        }
    }

    // ---- ENERGY STATUS Section (ANTI-FLICKERING VERSION) ----
    function updateEnergyStatus(data) {
        // ⭐ SKIP UPDATE IF DATA HASN'T CHANGED
        if (!hasDataChanged(data, lastEnergyUpdate)) {
            return;
        }
        lastEnergyUpdate = data ? { ...data } : null;

        const totalPowerElement = document.getElementById('totalPowerValue');
        const robotEnergyList = document.getElementById('robotEnergyList');
        const conveyorEnergyList = document.getElementById('conveyorEnergyList');
        
        if (!data) {
            if (totalPowerElement && totalPowerElement.textContent !== 'OFFLINE') {
                totalPowerElement.textContent = 'OFFLINE';
            }
            if (robotEnergyList && robotEnergyList.innerHTML !== '<li><span>Energy data unavailable</span></li>') {
                robotEnergyList.innerHTML = '<li><span>Energy data unavailable</span></li>';
            }
            if (conveyorEnergyList && conveyorEnergyList.innerHTML !== '<li><span>Energy data unavailable</span></li>') {
                conveyorEnergyList.innerHTML = '<li><span>Energy data unavailable</span></li>';
            }
            return;
        }
        
        // ⭐ UPDATE TOTAL POWER ONLY IF CHANGED
        if (totalPowerElement) {
            const newPowerText = `${data.total_power}W`;
            if (totalPowerElement.textContent !== newPowerText) {
                totalPowerElement.textContent = newPowerText;
            }
        }
        
        // ⭐ UPDATE ROBOT ENERGY LIST ONLY IF NEEDED
        if (robotEnergyList) {
            const fragment = document.createDocumentFragment();
            let hasChanges = false;
            
            for (let i = 1; i <= 3; i++) {
                const robot = data.robots[`arm_${i}`];
                if (!robot) continue;
                
                const existingItem = robotEnergyList.querySelector(`li:nth-child(${i})`);
                const newHTML = `
                    <div class="robot-info">
                        <span class="robot-status-dot ${robot.running ? 'running' : 'idle'}"></span>
                        <span class="robot-name">Robot ${i}</span>
                    </div>
                    <span class="robot-power">${robot.power}W</span>
                `;
                
                if (!existingItem || existingItem.innerHTML !== newHTML) {
                    hasChanges = true;
                    const listItem = document.createElement('li');
                    listItem.className = robot.running ? 'running' : 'idle';
                    listItem.innerHTML = newHTML;
                    fragment.appendChild(listItem);
                }
            }
            
            if (hasChanges) {
                robotEnergyList.innerHTML = '';
                robotEnergyList.appendChild(fragment);
            }
        }
        
        // ⭐ UPDATE CONVEYOR ENERGY LIST ONLY IF NEEDED
        if (conveyorEnergyList) {
            const fragment = document.createDocumentFragment();
            let hasChanges = false;
            
            for (let i = 1; i <= 2; i++) {
                const conveyor = data.conveyors[`conveyor_${i}`];
                if (!conveyor) continue;
                
                const existingItem = conveyorEnergyList.querySelector(`li:nth-child(${i})`);
                const newHTML = `
                    <div class="robot-info">
                        <span class="robot-status-dot ${conveyor.running ? 'running' : 'idle'}"></span>
                        <span class="robot-name">Conveyor ${i}</span>
                    </div>
                    <span class="robot-power">${conveyor.power}W</span>
                `;
                
                if (!existingItem || existingItem.innerHTML !== newHTML) {
                    hasChanges = true;
                    const listItem = document.createElement('li');
                    listItem.className = conveyor.running ? 'running' : 'idle';
                    listItem.innerHTML = newHTML;
                    fragment.appendChild(listItem);
                }
            }
            
            if (hasChanges) {
                conveyorEnergyList.innerHTML = '';
                conveyorEnergyList.appendChild(fragment);
            }
        }
    }

    function updateMainContentEnergyMonitoring(data) {
        const totalPowerMain = document.getElementById('totalPower');
        if (totalPowerMain) totalPowerMain.textContent = data ? data.total_power : '0';
        for (let i = 1; i <= 3; i++) {
            const armElement = document.getElementById(`arm${i}Energy`);
            if (armElement && data && data.robots[`arm_${i}`]) {
                const robot = data.robots[`arm_${i}`];
                const powerSpan = armElement.querySelector('.power');
                const statusSpan = armElement.querySelector('.energy-status');
                if (powerSpan) powerSpan.textContent = robot.power;
                if (statusSpan) {
                    statusSpan.textContent = robot.running ? 'RUNNING' : 'IDLE';
                    statusSpan.className = `energy-status ${robot.running ? 'running' : 'idle'}`;
                }
            }
        }
        for (let i = 1; i <= 2; i++) {
            const conveyorElement = document.getElementById(`conveyor${i}Energy`);
            if (conveyorElement && data && data.conveyors[`conveyor_${i}`]) {
                const conveyor = data.conveyors[`conveyor_${i}`];
                const powerSpan = conveyorElement.querySelector('.power');
                const statusSpan = conveyorElement.querySelector('.energy-status');
                if (powerSpan) powerSpan.textContent = conveyor.power;
                if (statusSpan) {
                    statusSpan.textContent = conveyor.running ? 'RUNNING' : 'IDLE';
                    statusSpan.className = `energy-status ${conveyor.running ? 'running' : 'idle'}`;
                }
            }
        }
    }

    // ⭐ ADD ONLY THIS PROX SENSOR FETCH FUNCTION
    async function fetchProxSensors() {
        try {
            const response = await fetch(`${API_BASE_URL}/prox_sensors`);
            if (!response.ok) throw new Error("Failed to fetch prox sensors");
            const data = await response.json();
            
            if (!hasDataChanged(data, lastProxSensorStates)) {
                return;
            }
            lastProxSensorStates = { ...data };
            
            // Just store the data - your existing step tracking will handle the rest
            proxSensorData = data;
            
        } catch (error) {
            console.error("Error fetching prox sensors:", error);
        }
    }

    // ---- API and Chat/Command ----
    async function fetchEnergyStatus() {
        try {
            const response = await fetch(`${API_BASE_URL}/energy_status`);
            if (!response.ok) throw new Error("Failed to fetch energy status");
            const data = await response.json();
            updateEnergyStatus(data);
            updateMainContentEnergyMonitoring(data);
            energyData = data;
        } catch (error) {
            console.error("Error fetching energy status:", error);
            updateEnergyStatus(null);
            updateMainContentEnergyMonitoring(null);
        }
    }

    async function sendCommand(commandText) {
        if (isLoading || !commandText.trim()) return;
        isLoading = true;
        addMessageToChat(commandText, 'user');
        addMessageToChat('Thinking...', 'ai', 'loading');
        userInput.value = '';
        try {
            setConnectionStatus('connecting', 'Sending...');
            const response = await fetch(`${API_BASE_URL}/command`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    command: commandText,
                    session_id: sessionId  // Include session ID for conversation history
                })
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: "Unknown server error" }));
                throw new Error(errorData.detail || `HTTP error ${response.status}`);
            }
            const data = await response.json();
            addMessageToChat(data.response, 'ai');
            debouncedUpdate(updatePLCStatus, data.plc_status);
            setConnectionStatus('connected', 'Online');
        } catch (error) {
            console.error("Error sending command:", error);
            addMessageToChat(`Error: ${error.message}`, 'system');
            setConnectionStatus('error', 'Error');
        } finally {
            isLoading = false;
            if (currentLoadingBubble) {
                currentLoadingBubble.remove();
                currentLoadingBubble = null;
            }
        }
    }

    async function fetchInitialGreeting() {
        isLoading = true;
        addMessageToChat('Connecting to TIA S7 Copilot...', 'system');
        setConnectionStatus('connecting', 'Initializing...');
        try {
            const response = await fetch(`${API_BASE_URL}/initial_greeting`);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ detail: "Failed to connect" }));
                throw new Error(errorData.detail || `HTTP error ${response.status}`);
            }
            const data = await response.json();
            addMessageToChat(data.ai_response, 'ai');
            debouncedUpdate(updatePLCStatus, data.plc_status);
            setConnectionStatus('connected', 'Online');
        } catch (error) {
            console.error("Error fetching initial greeting:", error);
            addMessageToChat(`Initialization Error: ${error.message}`, 'system');
            setConnectionStatus('error', 'Offline');
            fetchPLCStatusOnly();
        } finally {
            isLoading = false;
        }
    }

    function truncateText(text, maxLength) {
        return text.length > maxLength ? text.substring(0, maxLength) + '...' : text;
    }

    async function fetchPLCStatusOnly() {
        try {
            const response = await fetch(`${API_BASE_URL}/plc_status`);
            if (!response.ok) throw new Error("Failed to fetch PLC status");
            const statusData = await response.json();
            debouncedUpdate(updatePLCStatus, statusData);
        } catch (error) {
            console.error("Error fetching PLC status:", error);
            debouncedUpdate(updatePLCStatus, null);
        }
    }

    // ---- HANDLERS ----
    chatForm.addEventListener('submit', function(e) {
        e.preventDefault();
        sendCommand(userInput.value);
    });

    // ⭐ PERFECT MIXED SUGGESTIONS - 5 Static + 4 Rotating
    const staticSuggestions = [
        "Start",
        "Stop", 
        "Reset",
        "What step is being executed?",
        "What's the system status?", 
        "Show me energy consumption"
    ];

    const rotatingSuggestions = [
        "What's our current production rate?",
        "Show me cost analysis per part",
        "If I increase speed by 20%, what happens?",
        "What's the energy cost per part?",
        "How can we reduce energy consumption?",
        "What is my current cycle time?",
        "How many parts per hour are we making??",
        "Increase speed by 10%",
        "If we run for 20 hours, how much will it cost?",
        "Set speed to 50%",
        "How many parts in 8 hours?"
    ];

    // Update suggestion buttons - 5 static + 4 rotating = 9 total
    function updateSmartSuggestions() {
        const suggestionsContainer = document.querySelector('.suggestions-row');
        if (suggestionsContainer) {
            // Get 4 random rotating suggestions
            const randomRotating = rotatingSuggestions
                .sort(() => 0.5 - Math.random())
                .slice(0, 4);  // Keep 4 rotating
            
            // Combine: 5 static + 4 rotating = 9 total
            const allSuggestions = [...staticSuggestions, ...randomRotating];
            
            suggestionsContainer.innerHTML = allSuggestions
                .map((suggestion, index) => {
                    // Add special styling for static vs rotating
                    const buttonClass = index < 5 ? 'suggestion-btn static' : 'suggestion-btn rotating';  // Changed from 3 to 5
                    return `
                        <button class="${buttonClass}" data-suggestion="${suggestion}">
                            ${suggestion}
                        </button>
                    `;
                }).join('');
            
            // Re-attach event listeners
            attachSuggestionListeners();
        }
    }

    function attachSuggestionListeners() {
        const suggestionButtons = document.querySelectorAll('.suggestion-btn');

        suggestionButtons.forEach(button => {
            button.addEventListener('click', function() {
                const suggestion = this.getAttribute('data-suggestion');
                userInput.value = suggestion;
                sendCommand(suggestion);
            });
        });
    }

    async function updateIntelligentStatus() {
        try {
            // ⭐ USE STEP STATUS API INSTEAD
            const stepResponse = await fetch(`${API_BASE_URL}/step_status`);
            const stepData = await stepResponse.json();
            
            // Get proximity sensor data for workflow stage detection
            const proxResponse = await fetch(`${API_BASE_URL}/prox_sensors`);
            const proxData = await proxResponse.json();
            
            // Get PLC status for production counter
            const plcResponse = await fetch(`${API_BASE_URL}/plc_status`);
            const plcData = await plcResponse.json();
            
            console.log('📊 Step Data:', stepData);
            console.log('📍 Prox Data:', proxData);
            
            // ⭐ DETERMINE CURRENT STAGE FROM PROX SENSORS
            let currentStage = determineStageFromProxSensors(proxData, stepData);
            let nextAction = determineNextAction(proxData, stepData, plcData);
            
            // Calculate overall progress
            const arm1Progress = stepData.arms?.arm_1?.progress || 0;
            const arm2Progress = stepData.arms?.arm_2?.progress || 0;
            const arm3Progress = stepData.arms?.arm_3?.progress || 0;
            const totalProgress = (arm1Progress + arm2Progress + arm3Progress) / 3;
            
            // Update AI Intelligence Panel
            const currentStageElement = document.getElementById('current-stage');
            if (currentStageElement) {
                const newStage = formatStageName(currentStage);
                if (currentStageElement.textContent !== newStage) {
                    currentStageElement.textContent = newStage;
                }
            }
            
            // Update Next Action
            updateNextActionWithScroll(nextAction);
            
            // Update Overall Progress
            const overallProgressElement = document.getElementById('overall-progress');
            if (overallProgressElement) {
                const newProgress = `${totalProgress.toFixed(1)}%`;
                if (overallProgressElement.textContent !== newProgress) {
                    overallProgressElement.textContent = newProgress;
                }
            }
            
            // ⭐ UPDATE LIVE STEP TRACKING
            if (stepData.arms) {
                updateDetailedArmStatus('arm1', stepData.arms.arm_1);
                updateDetailedArmStatus('arm2', stepData.arms.arm_2);
                updateDetailedArmStatus('arm3', stepData.arms.arm_3);
                
                // Update sidebar step info (under power)
                updateArmStepInfo('arm1', stepData.arms.arm_1);
                updateArmStepInfo('arm2', stepData.arms.arm_2);
                updateArmStepInfo('arm3', stepData.arms.arm_3);
            }
            
            // Update summary section
            const activeCount = [arm1Progress, arm2Progress, arm3Progress].filter(p => p > 0).length;
            
            const activeArmsElement = document.getElementById('active-arms');
            if (activeArmsElement) {
                const newActiveText = `Active Arms: ${activeCount}`;
                if (activeArmsElement.textContent !== newActiveText) {
                    activeArmsElement.textContent = newActiveText;
                }
            }
            
            const totalProgressSummaryElement = document.getElementById('total-progress-summary');
            if (totalProgressSummaryElement) {
                const newProgressText = `System Progress: ${totalProgress.toFixed(1)}%`;
                if (totalProgressSummaryElement.textContent !== newProgressText) {
                    totalProgressSummaryElement.textContent = newProgressText;
                }
            }
            
            const currentOperationElement = document.getElementById('current-operation');
            if (currentOperationElement) {
                const newOperationText = `Current Operation: ${formatStageName(currentStage)}`;
                if (currentOperationElement.textContent !== newOperationText) {
                    currentOperationElement.textContent = newOperationText;
                }
            }
            
        } catch (error) {
            console.error('Error updating intelligent status:', error);
        }
    }

    // ⭐ NEW HELPER FUNCTION: Determine stage from proximity sensors
    function determineStageFromProxSensors(proxData, stepData) {
        if (!proxData || proxData.error) {
            return 'SYSTEM_IDLE';
        }
        
        // Check proximity sensors in workflow order
        if (proxData.Proximity_Sensor4) {
            return 'PRODUCTION_COMPLETE';
        }
        if (proxData.Proximity_Sensor3) {
            return 'FINAL_TRANSPORT';
        }
        if (proxData.Proximity_Sensor2) {
            return 'ASSEMBLY_PHASE';
        }
        if (proxData.Proximity_Sensor1) {
            return 'TRANSPORT_TO_POINT_B';
        }
        
        // Check arm activity if no prox sensors active
        const arm1Active = stepData.arms?.arm_1?.name !== 'IDLE';
        const arm2Active = stepData.arms?.arm_2?.name !== 'IDLE';
        const arm3Active = stepData.arms?.arm_3?.name !== 'IDLE';
        
        if (arm1Active && !arm2Active && !arm3Active) {
            return 'HOLDER_PLACEMENT';
        }
        if (arm2Active && arm3Active) {
            return 'PCB_ASSEMBLY';
        }
        if (arm2Active) {
            return 'ASSEMBLY_PHASE';
        }
        
        return 'SYSTEM_IDLE';
    }

    // ⭐ NEW HELPER FUNCTION: Determine next action
    function determineNextAction(proxData, stepData, plcData) {
        if (!proxData || proxData.error) {
            return 'System ready - waiting for start signal';
        }
        
        // Workflow-based next actions
        if (proxData.Proximity_Sensor4) {
            return 'Production cycle complete - ready for next cycle';
        }
        if (proxData.Proximity_Sensor3) {
            return 'Conveyor 2 transporting to completion';
        }
        if (proxData.Proximity_Sensor2) {
            return 'Arms 2 & 3 coordinating PCB assembly';
        }
        if (proxData.Proximity_Sensor1) {
            return 'Conveyor 1 transporting holder to Point B';
        }
        
        // Check arm steps for detailed actions
        const arm1Step = stepData.arms?.arm_1?.name || 'IDLE';
        const arm2Step = stepData.arms?.arm_2?.name || 'IDLE';
        const arm3Step = stepData.arms?.arm_3?.name || 'IDLE';
        
        if (arm1Step !== 'IDLE') {
            return `Arm 1: ${formatStepName(arm1Step)}`;
        }
        if (arm2Step !== 'IDLE') {
            return `Arm 2: ${formatStepName(arm2Step)}`;
        }
        if (arm3Step !== 'IDLE') {
            return `Arm 3: ${formatStepName(arm3Step)}`;
        }
        
        // Check if system is stopped
        if (plcData?.Stop) {
            return 'System stopped - press Start to resume';
        }
        
        return 'System idle - ready for operation';
    }

    // ⭐ UPDATE THIS FUNCTION (replace existing one)
    function updateDetailedArmStatus(armId, armData) {
        console.log(`🤖 Updating ${armId}:`, armData);
        
        if (!armData) return;
        
        const stepElement = document.getElementById(`${armId}-step-detailed`);
        const progressText = document.getElementById(`${armId}-progress-text`);
        const progressBar = document.getElementById(`${armId}-progress-bar`);
        
        if (stepElement) {
            const newStepText = formatStepName(armData.name || 'IDLE');
            if (stepElement.textContent !== newStepText) {
                stepElement.textContent = newStepText;
                stepElement.style.color = armData.name === 'IDLE' ? 'var(--muted)' : 'var(--accent-ink)';
            }
        }
        
        if (progressText) {
            const newProgressText = `${armData.progress || 0}%`;
            if (progressText.textContent !== newProgressText) {
                progressText.textContent = newProgressText;
            }
        }
        
        if (progressBar) {
            const newWidth = `${armData.progress || 0}%`;
            const newClass = `progress-fill-compact ${getProgressClass(armData.progress || 0)}`;
            if (progressBar.style.width !== newWidth || progressBar.className !== newClass) {
                progressBar.style.width = newWidth;
                progressBar.className = newClass;
            }
        }
        
        // Update parent container status
        const armContainer = stepElement?.closest('.arm-status-compact');
        if (armContainer) {
            if (armData.progress > 0) {
                armContainer.setAttribute('data-status', 'active');
            } else {
                armContainer.removeAttribute('data-status');
            }
        }
    }

    // ⭐ HELPER FUNCTION: Update sidebar step info (under power section)
    function updateArmStepInfo(armId, armData) {
        if (!armData) return;
        
        const stepElement = document.getElementById(`${armId}-step`);
        const progressElement = document.getElementById(`${armId}-progress`);
        
        if (stepElement) {
            const newStepText = formatStepName(armData.name || 'IDLE');  // ← CORRECT: Uses armData.name
            if (stepElement.textContent !== newStepText) {
                stepElement.textContent = newStepText;
                stepElement.style.color = armData.name === 'IDLE' ? 'var(--muted)' : 'var(--accent-ink)';
            }
        }
        if (progressElement) {
            const newProgressText = `${armData.progress || 0}%`;
            if (progressElement.textContent !== newProgressText) {
                progressElement.textContent = newProgressText;
                progressElement.style.color = armData.progress > 0 ? 'var(--accent-ink)' : 'var(--muted)';
            }
        }
    }

    // ⭐ HELPER FUNCTION: Format stage names
    function formatStageName(stage) {
        return stage.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, l => l.toUpperCase());
    }

    // ⭐ HELPER FUNCTION: Update next action with scroll animation
    function updateNextActionWithScroll(text) {
        const element = document.getElementById('next-action');
        if (!element) return;
        
        // Update only if text changed
        if (element.textContent === text) return;
        
        element.textContent = text;

        setTimeout(() => {
            const containerWidth = element.clientWidth;
            const textWidth = element.scrollWidth;
            const isOverflowing = textWidth > containerWidth;

            if (isOverflowing) {
                const hiddenWidth = textWidth - containerWidth;
                const safeScrollDistance = Math.min(hiddenWidth + 10, 25);
                
                element.style.setProperty('--scroll-distance', `-${safeScrollDistance}px`);
                element.classList.add('scrollable');
            } else {
                element.classList.remove('scrollable');
            }
        }, 10);
    }

    // Initialize suggestions immediately
    updateSmartSuggestions();

    // Only rotate the last 2 buttons every 5 seconds
    setInterval(updateSmartSuggestions, 3000);

    // ---- INITIAL LOAD ----
    fetchInitialGreeting();
    fetchEnergyStatus();

    // ---- POLLING (REDUCED FREQUENCY) ----
    setInterval(() => {
        fetchPLCStatusOnly();
        updateRobotState();
        fetchEnergyStatus();
        fetchProxSensors();
    }, 1500); // ⭐ REDUCED FROM 500ms TO 1500ms

    setInterval(() => {
        updateIntelligentStatus();
    }, 2000); // ⭐ REDUCED FROM 1000ms TO 2000ms

    setTimeout(() => {
        console.log("🎯 Starting intelligent status updates...");
        updateIntelligentStatus();
    }, 2000);

    // ⭐ CLEANUP
    window.addEventListener('beforeunload', function() {
        if (stepUpdateInterval) {
            clearInterval(stepUpdateInterval);
        }
        clearTimeout(updateTimeout);
    });

    window.testIntelligentStatus = function() {
        console.log("🧪 Testing intelligent status manually...");
        updateIntelligentStatus();
    };
});
