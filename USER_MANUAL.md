# Meet Intelligence — User Manual 📖🎙️

Welcome to **Meet Intelligence**! This guide walks you through setup and everyday usage step-by-step in plain English. No technical wizardry required.

---

## ⚡ 60-Second Quick Start

```
1. Double-click start.bat       → Starts the backend server
2. Load extension in Chrome     → chrome://extensions → "Load unpacked" → select extension/
3. Join any Google Meet call    → Click "🤖 Deploy Notes Bot"
4. View live notes & summary    → Open the side panel or go to http://localhost:8000/dashboard
```

---

## 📋 Prerequisites

Before you start, make sure you have:
1. **Google Chrome** (or Edge / Brave).
2. **Python 3.10 or higher** installed on your system.
3. **Free Groq API Key**:
   - Sign up at [https://console.groq.com](https://console.groq.com) (100% free, no credit card required).
   - Go to **API Keys** → click **Create API Key** → copy the key starting with `gsk_...`.

---

## 🚀 Step 1: Start the Backend Service

1. Open the `meet-intel` folder on your computer.
2. Double-click **`start.bat`** (or right-click **`start.ps1`** and select *Run with PowerShell*).
3. A command window will appear displaying:
   ```
   ==================================================
     Meet Intelligence - Live Bot & Command Center
   ==================================================
   Backend API:     http://localhost:8000
   Live Dashboard:  http://localhost:8000/dashboard
   Extension:       ...\meet-intel\extension
   ==================================================
   INFO:     Uvicorn running on http://127.0.0.1:8000
   ```
4. **Keep this window open** while using the application.

---

## 🧩 Step 2: Install the Chrome Extension

1. Open Google Chrome.
2. In the address bar, type:
   ```
   chrome://extensions
   ```
   and press Enter.
3. In the top-right corner, switch the **Developer mode** toggle **ON**.
4. In the top-left corner, click **Load unpacked**.
5. Browse to your `meet-intel` folder and select the **`extension`** folder.
6. You will see **Meet Intelligence** added to your extensions list!
7. *(Tip)* Click the **Puzzle piece 🧩** icon in Chrome's top bar and pin **Meet Intelligence** for 1-click access.

---

## 🔑 Step 3: Configure Your Free Groq Key

1. Click the **Meet Intelligence** extension icon in Chrome's top-right toolbar. The side panel will open on the right.
2. Scroll to the **Groq API settings** dropdown and expand it.
3. Paste your Groq API key (`gsk_...`).
4. Click **Load models available to my key**.
5. The extension will automatically set up the best available models (e.g. `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`).

---

## 🤖 Step 4: Joining Calls & 1-Click Bot Deployment

1. Open or join any Google Meet call (`https://meet.google.com/...`).
2. At the top-left of the Google Meet video window, you will see a floating dark pill:
   ```
   [ 🤖 Deploy Notes Bot ]
   ```
3. **Click it once.**
   - The button will update to `[ 🚀 Launching Bot... ]` and then to `[ ● Notes Bot Active (Click to Stop) ]`.
   - The bot enters the call lobby, is admitted, and begins transcribing speech in real-time.
4. **Zero-Echo Guarantee**:
   - The bot runs with full hardware audio isolation.
   - **No sound will ever play out of your speakers from the bot**, even if you and the bot join from the exact same Google account on the same computer. Your voice will stay crystal clear with zero echo.

---

## 📊 Step 5: Live Tracking & Summarization

As people speak in your meeting, the extension and dashboard track everything live:

### 1. Live Headcount & Metrics
- **Total attendees**: Number of people in the call.
- **Camera ON / OFF**: Instant video status.
- **Mic unmuted**: Live speaking indicators.
- **Device Breakdown**: Automatically detects **Desktop 🖥️** vs **Mobile 📱** attendees (portrait video, dial-in phone numbers, and Google Meet mobile badges).
- *(Manual Override)*: Click any participant's device badge in the side panel or dashboard to toggle between Desktop and Mobile anytime.

### 2. Multi-Language Transcription (English, Hindi, Hinglish)
- Accurately captures Devanagari Hindi text (`"है अभी भी कुछ"`, `"अपना"`), English, and Hinglish.
- Seamlessly records participants who have initial letter avatars (e.g. a circle with `"A"`).

### 3. Real-Time Catch-Up ("Summarize so far")
- Missed the first 10 minutes or stepped away?
- Click **Summarize so far** in the side panel or dashboard.
- The AI produces an instant summary of:
  - Current Topic
  - Key Discussion Points
  - Decisions made so far

### 4. Final Meeting Notes & Action Items ("Final report")
- When the meeting ends, click **Final report**.
- Produces:
  - Executive Summary
  - Chronological Topic Timeline
  - Decisions Table
  - Action Items & Next Steps (100% preserved, never dropped)
  - Participation Statistics (exact time and % each person spoke or had their camera on)
- Click **Download report (.md)** to save the markdown file.

---

## 🖥️ Live Web Dashboard

You can also view everything from a dedicated web dashboard (great for secondary monitors):

1. Open your browser and go to:
   ```
   http://localhost:8000/dashboard
   ```
2. View real-time meeting status, control the bot, see live participant rosters, and trigger AI summaries directly from the web browser.

---

## ❓ Frequently Asked Questions & Troubleshooting

### Q1: The extension says "Cannot connect to backend (http://localhost:8000)".
* **Solution**: The backend server is not running. Double-click `start.bat` in the `meet-intel` folder and keep that terminal window open.

### Q2: Can I manually turn off Google Meet captions?
* **Solution**: Yes! You have full control. If you click the `[cc]` button in Google Meet to turn captions off, the extension respects your choice and will **not** turn them back on.

### Q3: Will the bot cause an echo or make my voice distort?
* **Solution**: No. The bot runs with `--mute-audio`, `--no-audio`, and in-browser media blockers. Zero audio ever reaches your computer's sound card from the bot.

### Q4: How do I stop the bot?
* **Solution**: Click the floating pill button at the top-left of Google Meet (`Notes Bot Active (Click to Stop)`), or click **Stop Bot** in the side panel or Web Dashboard.

### Q5: How do I update the Chrome extension after code changes?
* **Solution**: Go to `chrome://extensions` and click the circular **Refresh/Update 🔄** icon on the Meet Intelligence card.

