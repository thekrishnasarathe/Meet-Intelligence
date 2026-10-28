# Meet Intelligence 🎙️✨

> **Speaker-separated transcripts, camera/mic engagement tracking, live catch-up summaries, and structured final reports for any Google Meet link — built to cost $0.**

Works on any Google Meet call (personal `@gmail.com` or Google Workspace) without requiring paid Google Workspace tiers, host OAuth permissions, or expensive third-party transcription APIs.

---

## 🌟 Key Highlights & Features

- **🚀 1-Click Bot Deployment**: Deploy an autonomous transcription bot with a single click from the floating in-page Meet pill, the Chrome side panel, or the interactive web dashboard.
- **🔇 Zero-Echo Audio Isolation**: Run the Playwright bot on the **exact same PC and Google account** as yourself without audio reverberation, feedback loops, or voice distortion. Hardware audio output, WebRTC audio sinks, and WebAudio contexts are permanently muted in the bot context.
- **🇮🇳 Full Hindi & Hinglish Transcription**: Captures Hindi Devanagari script, English, and Hinglish. Supports participants with initial letter avatars (no profile picture) and short-phrase streaming without sentence truncation.
- **📱 Multi-Layered Device Differentiation**: Automatically classifies participants as **Desktop 🖥️**, **Mobile 📱**, or **Unknown ❓** using video aspect ratios, Google Meet DOM Material symbols, dial-in patterns, and client user-agents, with 1-click interactive manual overrides.
- **⚡ Token-Budgeted LLM Reports**: Map-Reduce summarization pipeline built under strict 7,000-token caps with a 6,000 TPM rate-limiter, ensuring long meetings (170,000+ tokens) never crash or drop action items.
- **🎛️ Live Web Dashboard**: Interactive real-time dashboard at `http://localhost:8000/dashboard` featuring live headcount, camera/mic metrics, device breakdown, rolling transcript, and instant AI catch-ups.
- **🔒 Non-Intrusive Caption Controls**: Scraper respects user preferences — if you manually turn off captions in Google Meet, the extension never forces them back on.

---

## 📂 Architecture Overview

```
meet-intel/
├── extension/             # Chrome Extension (Manifest V3)
│   ├── manifest.json      # MV3 configuration (storage, sidePanel, permissions)
│   ├── meet-core.js       # Shared scraper engine: captions, speakers, camera/mic, device detection
│   ├── content.js         # Injects meet-core & 1-click floating deploy pill into Meet tabs
│   ├── background.js      # Service worker persisting sessions & bridging extension with backend
│   ├── lib.js             # Turn merging, transcript formatting, engagement math, JSONL replay
│   ├── sidepanel.html     # Live side panel UI (headcount, transcript, summaries, 1-click bot)
│   ├── sidepanel.js       # Side panel logic, Groq LLM calls, model fallbacks, export
│   └── sidepanel.css      # Modern dark/light responsive styling
├── bot/                   # Playwright Google Meet Bot v2 & Pipeline
│   ├── runner.py          # Bot lifecycle orchestrator, supervision loop, consent notice, status heartbeat
│   ├── browser.py         # Controlled Playwright browser, audio isolation, layout & caption setup
│   ├── selectors.json     # Centralized selector and label configuration
│   ├── scraper.js         # In-page scraper v2 (schema v2 events, heartbeats, timestamps)
│   ├── roster.py          # People panel roster tracker + debounced join/leave diff
│   ├── fusion.py          # Multi-source signal fusion with hysteresis and device detection
│   ├── store.py           # Append-only events.jsonl + SQLite WAL database
│   ├── analytics.py       # Engagement metrics, concurrency curves, markdown participation tables
│   ├── report.py          # Token-budgeted report pipeline (Map-Reduce under 7000 tokens, 6000 TPM pacer)
│   ├── bot.py             # CLI runner with headless and persistent profile support
│   ├── summarize.py       # Standalone CLI summarizer for JSONL sessions
│   ├── Dockerfile         # Docker container for running bot headlessly under xvfb
│   └── requirements.txt   # Bot dependencies (playwright, httpx)
├── backend/               # Unified FastAPI REST API & Web Command Center
│   ├── main.py            # Event ingest, transcript API, summarization, bot management, dashboard
│   ├── templates/         # HTML templates
│   │   └── dashboard.html # Interactive real-time web dashboard
│   ├── .env               # Environment configuration (GROQ_API_KEY)
│   ├── Dockerfile         # Docker container for backend service
│   └── requirements.txt   # FastAPI, uvicorn, httpx, python-dotenv
├── tests/                 # Automated test suite (135+ tests)
│   ├── core.test.js       # Unit tests for scraper logic, letter avatars, device detection (62 checks)
│   ├── sidepanel.test.js  # Integration tests for side panel UI & Groq integration (26 checks)
│   ├── report_test.py     # Token-budgeted report pipeline & 6000 TPM pacer validation (47 checks)
│   ├── test_bot_core.py   # Unit tests for fusion, roster, store, and analytics (7 tests)
│   ├── accuracy.py        # Accuracy evaluation harness against scripted scenarios
│   └── scenarios/         # Ground truth test meeting scenarios
├── start.bat              # 1-Click Windows Batch launcher
├── start.ps1              # 1-Click Windows PowerShell launcher
└── docker-compose.yml     # Multi-container orchestration (backend + bot)
```

---

## 🚀 Quickstart (Windows 1-Click Launch)

To start the backend API and web dashboard immediately:

### Option A: Double-Click `start.bat`
Simply double-click `start.bat` in the project root.

### Option B: PowerShell
```powershell
.\start.ps1
```

Once running:
- **Backend API**: `http://localhost:8000`
- **Live Web Dashboard**: `http://localhost:8000/dashboard`
- **Extension Path**: `./extension`

---

## 🛠️ How to Use It

You can use Meet Intelligence in **four different ways**, depending on your workflow:

### Option 1: Chrome Extension + In-Page Bot Pill (Recommended)

Best for active participants who attend calls in Google Chrome, Brave, or Microsoft Edge.

#### 1. Install the Extension
1. Open Chrome and navigate to `chrome://extensions`.
2. Toggle **Developer mode** ON (top right corner).
3. Click **Load unpacked** (top left corner).
4. Select the `extension/` folder in this repository.

#### 2. Configure Your Free Groq API Key
1. Click the **Meet Intelligence** icon in your browser toolbar to open the side panel.
2. Expand the **Groq API settings** dropdown.
3. Paste your free Groq API key (`gsk_...`).
4. Click **Load models available to my key**. The extension will configure the best models (e.g. `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen/qwen3.8-27b`).

#### 3. Join Any Meeting
1. Open any Google Meet call (`https://meet.google.com/xxx-yyyy-zzz`).
2. You will see a floating pill at the top-left: **🤖 Deploy Notes Bot**.
   - Click it once to spawn a background Playwright bot into the current meeting.
   - Click again anytime to stop the bot.
3. The side panel automatically tracks:
   - **Live Headcount**: Total participants, camera active, microphone unmuted.
   - **Device Breakdown**: Count of Desktop 🖥️ vs. Mobile 📱 attendees.
   - **Speaker Transcript**: Hindi, Hinglish, and English speech attributed by speaker and timestamp.
   - **Participation Table**: Exact time and % each attendee spent with camera ON, mic unmuted, and their detected device.

#### 4. Summarize & Export
- **Summarize so far**: Mid-call rolling catch-up (current topic, key discussion points, decisions).
- **Final report**: Complete executive summary, topic timeline, decisions, action items, and participation table.
- **Download Report (.md)**: Saves the formatted markdown meeting notes to disk.

---

### Option 2: Live Web Dashboard

Best for monitoring meetings from a secondary screen or browser window without opening DevTools:

1. Navigate to: `http://localhost:8000/dashboard`
2. Enter your meeting code (e.g. `znm-jeaj-htq` or `qfx-wccf-bwu`) or let the extension automatically route live events.
3. Manage the bot lifecycle (**Launch Bot**, **Stop Bot**), view live telemetry cards, stream real-time transcript updates, and trigger LLM catch-ups.

---

### Option 3: Standalone Playwright Bot (Link-Only)

Best for joining a meeting link automatically in the background without needing a user to be present in the call:

#### 1. Setup Environment
```bash
cd bot
pip install -r requirements.txt
playwright install chromium
```

#### 2. One-Time Google Account Login (Optional)
If your organization requires Google sign-in to admit guests:
```bash
python bot.py --login
```
Sign in once; credentials persist in `bot-profile/`.

#### 3. Launch the Bot into a Call
```bash
python bot/runner.py https://meet.google.com/abc-defg-hij --name "Notes Bot" --backend http://localhost:8000
```
The bot will:
- Mute its camera and microphone pre-join.
- Disable all local audio outputs (zero echo on your PC).
- Join the call and configure Hindi / Hinglish captions.
- Stream events to `session_output/events.jsonl`, `status.json`, and the backend.
- Automatically produce `final_report.md` upon call completion.

---

### Option 4: Docker & Docker Compose

Run the entire backend and bot stack in isolated containers:

```bash
# 1. Start the FastAPI backend
docker compose up -d api

# 2. Run the bot headlessly for a meeting
docker compose run --rm bot https://meet.google.com/abc-defg-hij --name "Docker Bot" --backend http://api:8000
```

---

## 🧪 Automated Testing & Accuracy Verification

The repository contains an automated test suite with **135+ checks**:

### 1. In-Page Scraper & Extension Tests (62 checks)
```bash
node tests/core.test.js
```
- Tests call lifecycle events (`call_started`, `call_ended`).
- Validates caption extraction for participants with circular photos and **letter avatars** (`<div>A</div>`).
- Verifies Hindi character boundaries and adaptive text merging (`mergeText`).
- Validates desktop vs mobile device detection via Material SVG paths and aspect ratios.
- Verifies that manual user caption controls are preserved.

### 2. Side Panel UI & Groq LLM Tests (26 checks)
```bash
node tests/sidepanel.test.js
```
- Validates side panel DOM rendering and headcount stats.
- Tests Groq API client integration, prompt structures, and multi-model fallbacks.
- Tests 1-click device override toggles and session persistence.

### 3. Token-Budgeted Report Pipeline Tests (47 checks)
```bash
python tests/report_test.py
```
- Verifies long meeting summarization (170,000+ tokens, 14x single-call cap) under strict 7,000-token caps.
- Tests sliding-window 6,000 TPM token pacer with fake clock simulation.
- Validates HTTP 429 rate limit backoff and checkpoint resume after interruptions.
- Ensures 100% of action items and decisions survive chunked Map-Reduce passes.

### Run All Tests:
```bash
node tests/core.test.js
node tests/sidepanel.test.js
python tests/report_test.py
```

---

## 🔇 Zero-Echo Audio Architecture

When running a meeting bot on the same machine where you are speaking, audio feedback loops can severely distort communication. Meet Intelligence solves this through a multi-tiered isolation strategy:

1. **Chromium Hardware Flags**:
   Chromium is launched with `--mute-audio`, `--no-audio`, `--disable-audio-output`, and `--autoplay-policy=no-user-gesture-required`.
2. **DOM-Level Media Silencing**:
   A Playwright initialization script intercepts `HTMLMediaElement.prototype.play` and attaches a `MutationObserver` that forces `.muted = true` and `.volume = 0` on every `<audio>` and `<video>` element created by Google Meet.
3. **WebAudio Suspension**:
   Overrides `AudioContext.prototype.resume` to prevent sound synthesis from reaching the host audio driver.

---

## 📱 Device Classification Strategy

Because Google Meet does not expose the client OS directly in the DOM, Meet Intelligence implements multi-layered, honest observability:

| Classification | Detection Mechanism | Confidence |
| :--- | :--- | :--- |
| **Mobile 📱** | Dial-in telephone numbers in name | High |
| **Mobile 📱** | Google Meet Material SVG badge (`smartphone`, `phone_android`) | High |
| **Mobile 📱** | Video aspect ratio in portrait orientation (`height > width`) | Moderate |
| **Desktop 🖥️** | Standard landscape video stream (`width >= height`) | Moderate |
| **Unknown ❓** | Camera off and no dial-in icon | Honest (Never guessed) |
| **Manual Override** | 1-click toggle on any participant badge in Side Panel or Dashboard | Direct User Intent |

---

## 🔑 Groq LLM Multi-Model Resilience

Meet Intelligence features automatic multi-model fallback. If a model encounters high traffic or rate limits (HTTP 429/503), the engine automatically falls back in real time:

1. `llama-3.3-70b-versatile` (Deep reasoning & structured reports)
2. `llama-3.1-8b-instant` (Ultra-fast streaming catch-ups)
3. `qwen/qwen3.8-27b` (Verified active model on Groq free tier)
4. `openai/gpt-oss-20b`

---

## 🛡️ Privacy & Compliance Notes

- **Client-Side Speech Recognition**: Transcription utilizes Google Meet's built-in live captions rendered in the browser. No raw audio streams are uploaded to external speech-to-text providers.
- **Recording Consent**: When enabled, the bot automatically announces itself in the meeting chat upon joining (`"Notes Bot is transcribing this meeting."`) to comply with recording consent standards.
- **Microphone Indicators**: The system observes the mute button state in Google Meet's DOM, reflecting when microphones are unmuted.
