# Security and safety

TIA S7 Copilot is a research and demonstration project for a lab assembly cell. It controls real
machinery, so read this before connecting it to hardware.

## Machine safety

- **The software stop is not a safety function.** Typing or saying "stop" sets a PLC bit and
  halts the robot sequences. It is not certified and depends on the PC, the network and the
  PLC program. The hard-wired emergency-stop circuit must remain the primary protection.
- Commands are classified by a language model. The backend clamps speed values, but the
  question-versus-command distinction relies on model output, not an independent interlock.
  Emergency keywords are matched before classification, even inside a question. Prompt rules
  do not guarantee read-only behavior. Supervise the cell whenever the copilot can drive it.

## Network exposure

- **Neither service has authentication.** Anyone who can reach port 8000 or 8001 can start, stop
  and reset the cell or change its speed. `scripts/start_app.bat` binds both services to
  `0.0.0.0`, exposing them on all host interfaces. The current dashboard assumes a local
  browser and uses localhost API URLs; this binding is not remote-dashboard configuration.
- Run the PC on an isolated machine network, or block ports 8000 and 8001 in the firewall. If you
  only need local access, start the copilot API with `--host 127.0.0.1`. The robot controller
  still binds port 8001 to all interfaces; protect that port separately.
- Snap7 needs *PUT/GET access* enabled on the S7-1500. With that setting, every device that can
  reach the PLC on TCP port 102 can read and write its memory. Keep the PLC on a segmented network.
- Browser CORS is limited to `http://localhost:8000` and `http://127.0.0.1:8000` by default
  (`CORS_ALLOW_ORIGINS`). CORS is not authentication and does not block non-browser clients.

## Secrets and data

- Credentials come only from environment variables or a local `.env` file. `.env` is
  git-ignored; commit only `.env.example`. If a key was ever committed or shared, rotate it.
- LLM-backed requests send chat text and cell context (PLC values, speeds, energy figures and
  available engineering metadata) to the configured Azure OpenAI deployment. Direct commands
  such as `stop` bypass that call.
- The optional voice assistant sends microphone audio to the Google Web Speech API for
  transcription. See [docs/voice-assistant.md](docs/voice-assistant.md).
- Runtime state is written to `data/` (or `COPILOT_DATA_DIR`) and is git-ignored.

## Front end

- LLM replies are rendered as Markdown with marked and then sanitised with DOMPurify before
  they are inserted into the page.
- Both libraries are loaded from jsDelivr with pinned versions and Subresource Integrity hashes.

## Reporting a vulnerability

Where private reporting is enabled, use GitHub's *Report a vulnerability* button
(Security → Advisories). If it is unavailable, open an issue asking for a private contact
without posting exploit details, credentials or plant data. Repository owners must enable
private vulnerability reporting in GitHub settings after publication.
