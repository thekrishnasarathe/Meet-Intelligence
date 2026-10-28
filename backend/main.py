"""
FastAPI Backend for Meet Intelligence (Bot + Chrome Extension Unified Server).
Implements the exact endpoints expected for the Tech Lead demo:
  - POST /meetings                        Register or import a bot session.jsonl file
  - POST /meetings/{id}/events            Ingest live events from bot.py or Chrome extension
  - GET  /meetings/{id}                   Retrieve live meeting status & camera/mic headcount
  - GET  /meetings/{id}/transcript        Retrieve merged speaker-attributed transcript turns
  - POST /meetings/{id}/summarize/live    Generate incremental mid-meeting catch-up summary
  - POST /meetings/{id}/summarize/final   Generate final structured report + participation table
  - POST /bot/launch                      Start the Playwright bot for a given Meet link
  - GET  /bot/{id}/status                 Check whether the bot is still running
  - POST /bot/{id}/stop                   Kill the bot process

Supports both:
  1. Groq Free Tier (set GROQ_API_KEY env var or backend/.env)
  2. Local Ollama 100% offline inference (set OLLAMA_MODEL=llama3.2 or qwen2.5, default fallback)
"""
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

app = FastAPI(title="Meet Intelligence API (Bot + Extension)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSIONS: Dict[str, Dict[str, Any]] = {}

SYSTEM_LIVE = (
    "You maintain a rolling summary of a live meeting. You receive PREVIOUS_STATE (JSON) and NEW_TRANSCRIPT. "
    "The meeting speech and transcript may be in English, Hindi, or Hinglish (Hindi written in Latin or Devanagari script). "
    "Accurately interpret Hindi and Hinglish technical terminology, idioms, and context, and output all summaries and points in clear English. "
    "Return ONLY a JSON object: {\"current_topic\": string, \"key_points\": [{\"speaker\": string, \"point\": string}], "
    "\"decisions\": [string], \"action_items\": [{\"owner\": string, \"task\": string, \"due\": string|null}], "
    "\"open_questions\": [string]}. Merge the new transcript into the previous state and keep it under 400 tokens. "
    "Never invent facts, names, owners, or deadlines."
)

SYSTEM_FINAL = (
    "You write meeting reports from speaker-attributed, timestamped transcripts. Use only what the transcript says. "
    "The transcript may contain speech in English, Hindi, or Hinglish (Hindi written in Roman or Devanagari script). "
    "Accurately understand Hindi/Hinglish discussions, translating and synthesizing key takeaways and actions into professional English. "
    "Output Markdown with these sections: \"## Executive summary\", \"## Topic timeline\", "
    "\"## Speaker contributions\", \"## Decisions\", \"## Action items\" (table: Owner | Task | Due), "
    "\"## Open questions\". Never fabricate owners or deadlines."
)


def get_or_create_session(meeting_id: str) -> Dict[str, Any]:
    if meeting_id not in SESSIONS:
        s = {
            "meetingId": meeting_id,
            "startedAt": None,
            "endedAt": None,
            "segments": {},
            "order": [],
            "media": [],
            "liveState": None,
            "cursor": 0,
            "finalReport": None,
        }
        # Hydrate from session_output/events.jsonl if present
        jsonl = REPO_ROOT / "session_output" / "events.jsonl"
        if jsonl.exists():
            try:
                with open(jsonl, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            ev = json.loads(line)
                            mid = (ev.get("meeting_id") or ev.get("meetingId") or "").lower()
                            if mid == meeting_id.lower():
                                apply_event(s, ev)
            except Exception:
                pass
        SESSIONS[meeting_id] = s
    return SESSIONS[meeting_id]


def apply_event(s: Dict[str, Any], ev: Dict[str, Any]) -> None:
    t = ev.get("type")
    if t == "call_started":
        is_ended = bool(s.get("endedAt"))
        last_event_ts = s["media"][-1].get("ts") if s.get("media") else (s.get("startedAt") or None)
        gap_ms = 0
        if last_event_ts and ev.get("ts"):
            gap_ms = parse_iso(ev.get("ts")) - parse_iso(last_event_ts)
        if is_ended or (s.get("startedAt") and gap_ms > 60_000):
            s["startedAt"] = ev.get("ts")
            s["endedAt"] = None
            if not s["segments"]:
                s["segments"] = {}
                s["order"] = []
                s["media"] = []
                s["liveState"] = None
                s["cursor"] = 0
                s["finalReport"] = None
        else:
            s["startedAt"] = s["startedAt"] or ev.get("ts")
            s["endedAt"] = None
    elif t == "call_ended":
        s["endedAt"] = ev.get("ts")
    elif t == "caption":
        if not s["startedAt"]:
            s["startedAt"] = ev.get("start_ts") or ev.get("ts")
        seg_id = ev["id"]
        if seg_id not in s["segments"]:
            s["order"].append(seg_id)
        s["segments"][seg_id] = {
            "id": seg_id,
            "speaker": ev.get("speaker", "Unknown"),
            "text": ev.get("text", ""),
            "start_ts": ev.get("start_ts") or ev.get("ts"),
            "end_ts": ev.get("end_ts") or ev.get("ts"),
            "final": bool(ev.get("final", False)),
        }
    elif t in ("participant_join", "participant_leave", "participant_update", "cam_on", "cam_off", "mic_on", "mic_off"):
        s["media"].append({
            "type": t,
            "id": ev.get("id", "unknown"),
            "name": ev.get("name", "Participant"),
            "cam": ev.get("cam"),
            "mic": ev.get("mic"),
            "device": ev.get("device", "desktop"),
            "ts": ev.get("ts"),
        })
    elif t == "snapshot":
        s["lastSnapshot"] = {
            "total": ev.get("total"),
            "cam": ev.get("cam_on"),
            "mic": ev.get("mic_on"),
            "ts": ev.get("ts"),
        }


def fmt_clock(ms: float) -> str:
    total = max(0, int(ms // 1000))
    h, m, sec = total // 3600, (total % 3600) // 60, total % 60
    return (f"{h:02d}:" if h else "") + f"{m:02d}:{sec:02d}"


def fmt_dur(ms: float) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    return f"{m // 60}h {m % 60:02d}m"


def parse_iso(ts: Optional[str]) -> float:
    if not ts:
        return datetime.now(timezone.utc).timestamp() * 1000.0
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0


def build_turns(s: Dict[str, Any], start_idx: int = 0, end_idx: Optional[int] = None) -> List[Dict[str, Any]]:
    ids = s["order"][start_idx:end_idx]
    t0 = parse_iso(s["startedAt"]) if s.get("startedAt") else None
    if t0 is None and ids:
        first_seg = s["segments"].get(ids[0])
        if first_seg and first_seg.get("start_ts"):
            t0 = parse_iso(first_seg["start_ts"])
    turns: List[Dict[str, Any]] = []
    for seg_id in ids:
        seg = s["segments"].get(seg_id)
        if not seg or not seg.get("text"):
            continue
        st = parse_iso(seg["start_ts"])
        en = parse_iso(seg["end_ts"])
        if turns and turns[-1]["speaker"] == seg["speaker"] and (st - turns[-1]["endMs"]) < 20000:
            turns[-1]["text"] += " " + seg["text"]
            turns[-1]["endMs"] = en
        else:
            turns.append({
                "speaker": seg["speaker"],
                "text": seg["text"],
                "startMs": st,
                "endMs": en,
                "offsetMs": 0 if t0 is None else max(0.0, st - t0),
            })
    return turns


def clean_participant_name(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", raw).strip()
    m_pin = re.match(r"^(?:pin|unpin)\s+(.+?)(?:\s+to\s+(?:your\s+)?(?:main\s+)?screen)?$", s, flags=re.I)
    if m_pin:
        s = m_pin.group(1).strip()
    m_action = re.match(r"^(?:more\s+options|actions|action\s+menu)\s+for\s+(.+)$", s, flags=re.I)
    if m_action:
        s = m_action.group(1).strip()
    m_mute = re.match(r"^(?:mute|unmute)\s+(.+)$", s, flags=re.I)
    if m_mute:
        s = m_mute.group(1).strip()
    m_state = re.match(r"^(.+?)\s+is\s+(?:speaking|talking|muted|unmuted)$", s, flags=re.I)
    if m_state:
        s = m_state.group(1).strip()
    s = re.sub(r"\s*\((?:you|presentation|presenting|meeting host|host|co-host|guest|external)\)$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:meeting host|host|co-host)$", "", s, flags=re.I)
    return s.strip()


def is_noise_name(s: str) -> bool:
    if not s or len(s) <= 1:
        return True
    if re.search(r"^(you're|you are)?\s*(continuously\s+framed|framed|framing)", s, flags=re.I):
        return True
    if re.search(r"^(pin|unpin|jump to bottom|captions?|caption settings|settings|more options|actions|mute|unmute|remove from call|keep|devices?|people|contributors?|search for people)$", s, flags=re.I):
        return True
    if re.search(r"^(participant|tile|video|grid|screen|presentation|presentation tile)$", s, flags=re.I):
        return True
    if re.search(r"^(\d+\s*(people|contributors?|in the call)?)$", s, flags=re.I):
        return True
    if re.search(r"^(minimize|expand self view|show in a tile|change layout|picture in picture)$", s, flags=re.I):
        return True
    return False


def compute_engagement(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    now_ms = parse_iso(s["endedAt"]) if s["endedAt"] else datetime.now(timezone.utc).timestamp() * 1000.0
    events = sorted(s["media"], key=lambda e: parse_iso(e.get("ts")))
    people: Dict[str, Dict[str, Any]] = {}

    def accrue(p: Dict[str, Any], t: float) -> None:
        if p["present"] and p["last"] is not None and t > p["last"]:
            d = t - p["last"]
            p["presentMs"] += d
            if p["cam"]:
                p["camMs"] += d
            if p["mic"]:
                p["micMs"] += d
        p["last"] = t

    for e in events:
        t = parse_iso(e.get("ts"))
        pid = e["id"]
        if pid not in people:
            people[pid] = {
                "id": pid, "name": e.get("name", "Participant"),
                "present": False, "cam": False, "mic": False,
                "device": e.get("device") or "unknown",
                "presentMs": 0.0, "camMs": 0.0, "micMs": 0.0, "last": None,
            }
        p = people[pid]
        accrue(p, t)
        if e.get("name"):
            p["name"] = e["name"]
        if e.get("device"):
            p["device"] = e["device"]
        etype = e["type"]
        if etype == "participant_join":
            p["present"] = True
            p["cam"] = bool(e.get("cam"))
            p["mic"] = bool(e.get("mic"))
            if e.get("device"):
                p["device"] = e["device"]
        elif etype == "participant_leave":
            p["present"] = False
        elif etype == "participant_update":
            if e.get("device"):
                p["device"] = e["device"]
        elif etype == "cam_on":
            p["cam"] = True
        elif etype == "cam_off":
            p["cam"] = False
        elif etype == "mic_on":
            p["mic"] = True
        elif etype == "mic_off":
            p["mic"] = False

    for p in people.values():
        accrue(p, now_ms)

    # Deduplicate self / same name entries
    merged: Dict[str, Dict[str, Any]] = {}
    for p in people.values():
        clean_name = clean_participant_name(p["name"])
        if is_noise_name(clean_name):
            continue
        is_self = p["id"] == "self" or clean_name.lower() == "you"
        key = "__self__" if is_self else clean_name.lower()
        if key not in merged:
            merged[key] = {
                "name": clean_name,
                "present": p["present"],
                "cam": p["cam"],
                "mic": p["mic"],
                "device": p.get("device", "desktop"),
                "presentMs": p["presentMs"],
                "camMs": p["camMs"],
                "micMs": p["micMs"],
            }
        else:
            if p["present"]:
                merged[key]["present"] = True
            if p["cam"]:
                merged[key]["cam"] = True
            if p["mic"]:
                merged[key]["mic"] = True
            if p.get("device"):
                if p["device"] in ("mobile", "phone_dial_in", "likely_mobile"):
                    merged[key]["device"] = p["device"]
                elif p["device"] in ("desktop", "likely_desktop") and merged[key]["device"] not in ("mobile", "phone_dial_in", "likely_mobile"):
                    merged[key]["device"] = p["device"]
            merged[key]["presentMs"] = max(merged[key]["presentMs"], p["presentMs"])
            merged[key]["camMs"] = max(merged[key]["camMs"], p["camMs"])
            merged[key]["micMs"] = max(merged[key]["micMs"], p["micMs"])

    return [
        {
            "name": p["name"],
            "device": p.get("device", "unknown"),
            "present": p["present"],
            "cam": p["cam"],
            "mic": p["mic"],
            "presentMs": round(p["presentMs"]),
            "camMs": round(p["camMs"]),
            "micMs": round(p["micMs"]),
            "camPct": round(100 * p["camMs"] / p["presentMs"]) if p["presentMs"] > 0 else 0,
            "micPct": round(100 * p["micMs"] / p["presentMs"]) if p["presentMs"] > 0 else 0,
        }
        for p in merged.values()
    ]


async def call_llm(system_prompt: str, user_prompt: str, as_json: bool = False) -> str:
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    preferred_model = os.environ.get(
        "GROQ_MODEL",
        "llama-3.1-8b-instant" if as_json else "llama-3.3-70b-versatile",
    )
    candidates = (
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b",
    )
    fallback_models = [preferred_model] + [m for m in candidates if m != preferred_model]
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.2")

    async with httpx.AsyncClient(timeout=60.0) as client:
        if groq_key:
            last_err = ""
            for model in fallback_models:
                payload: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2 if as_json else 0.3,
                }
                if as_json:
                    payload["response_format"] = {"type": "json_object"}
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json=payload,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("choices", [{}])[0].get("message", {}).get("content", "")
                last_err = resp.text
                if resp.status_code not in (429, 404, 503):
                    break
            raise HTTPException(status_code=502, detail=f"Groq API error: {last_err}")

        # Fallback: Local Ollama (100% Free Local Inference)
        try:
            payload = {
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            }
            if as_json:
                payload["format"] = "json"
            resp = await client.post(f"{ollama_url}/api/chat", json=payload)
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("content", "")
        except Exception:
            pass

    raise HTTPException(
        status_code=400,
        detail="Set GROQ_API_KEY env var (`export GROQ_API_KEY=gsk_...`) or start local Ollama (`ollama run llama3.2`).",
    )


class RegisterMeetingReq(BaseModel):
    meeting_id: str
    jsonl_path: Optional[str] = None


@app.post("/meetings")
def register_meeting(req: RegisterMeetingReq):
    s = get_or_create_session(req.meeting_id)
    if req.jsonl_path and os.path.isfile(req.jsonl_path):
        with open(req.jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    apply_event(s, json.loads(line))
    return {"status": "registered", "meeting_id": req.meeting_id, "segments": len(s["order"]), "media_events": len(s["media"])}


@app.post("/meetings/{meeting_id}/events")
def ingest_event(meeting_id: str, ev: Dict[str, Any]):
    s = get_or_create_session(meeting_id)
    apply_event(s, ev)
    return {"status": "ok"}


@app.delete("/meetings/{meeting_id}")
def reset_meeting(meeting_id: str):
    if meeting_id in SESSIONS:
        del SESSIONS[meeting_id]
    return {"status": "reset", "meeting_id": meeting_id}


@app.get("/meetings/{meeting_id}")
def get_meeting_status(meeting_id: str):
    s = get_or_create_session(meeting_id)
    eng = compute_engagement(s)
    present = [p for p in eng if p["present"]]
    snap = s.get("lastSnapshot") or {}
    total = max(len(present), snap.get("total", 0)) if snap.get("total") else len(present)
    cam_on = sum(1 for p in present if p["cam"])
    mic_on = sum(1 for p in present if p["mic"])
    desktop_count = sum(1 for p in present if p.get("device") in ("desktop", "likely_desktop"))
    mobile_count = sum(1 for p in present if p.get("device") in ("mobile", "phone_dial_in", "phone_dialin", "likely_mobile"))
    unknown_count = sum(1 for p in present if p.get("device") not in ("desktop", "likely_desktop", "mobile", "phone_dial_in", "phone_dialin", "likely_mobile"))
    return {
        "meeting_id": meeting_id,
        "started_at": s["startedAt"],
        "ended_at": s["endedAt"],
        "utterance_count": len(s["order"]),
        "live_headcount": {
            "total_present": total,
            "cam_on": cam_on,
            "cam_off": max(0, total - cam_on),
            "mic_on": mic_on,
            "mic_off": max(0, total - mic_on),
            "desktop_count": desktop_count,
            "mobile_count": mobile_count,
            "unknown_count": unknown_count,
        },
        "participant_engagement": eng,
    }


@app.get("/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: str):
    s = get_or_create_session(meeting_id)
    turns = build_turns(s)
    return {
        "meeting_id": meeting_id,
        "turn_count": len(turns),
        "turns": [
            {"timestamp": fmt_clock(t["offsetMs"]), "speaker": t["speaker"], "text": t["text"]}
            for t in turns
        ],
    }


@app.post("/meetings/{meeting_id}/summarize/live")
async def summarize_live_endpoint(meeting_id: str):
    s = get_or_create_session(meeting_id)
    upto = len(s["order"])
    if upto == 0:
        raise HTTPException(status_code=400, detail="No transcript segments captured yet")
    turns = build_turns(s, s["cursor"], upto)
    if not turns and s["liveState"]:
        return s["liveState"]
    delta_text = "\n".join(f"[{fmt_clock(t['offsetMs'])}] {t['speaker']}: {t['text']}" for t in turns)
    prompt = f"PREVIOUS_STATE:\n{json.dumps(s['liveState'] or {})}\n\nNEW_TRANSCRIPT:\n{delta_text}"
    raw = await call_llm(SYSTEM_LIVE, prompt, as_json=True)
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    s["liveState"] = json.loads(cleaned)
    s["cursor"] = upto
    return s["liveState"]


@app.post("/meetings/{meeting_id}/summarize/final")
async def summarize_final_endpoint(meeting_id: str):
    s = get_or_create_session(meeting_id)
    turns = build_turns(s)
    if not turns:
        raise HTTPException(status_code=400, detail="No transcript segments captured yet")
    eng = compute_engagement(s)
    names = ", ".join(r["name"] for r in eng) or "Unknown"
    tx = "\n".join(f"[{fmt_clock(t['offsetMs'])}] {t['speaker']}: {t['text']}" for t in turns)
    prompt = f"Meeting {meeting_id}. Participants: {names}.\n\nTRANSCRIPT:\n{tx}"
    body = await call_llm(SYSTEM_FINAL, prompt, as_json=False)
    part_lines = [
        "## Participation & Media Telemetry",
        "",
        "| Participant | Present | Camera on | Mic unmuted | Device |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in eng:
        dev_val = r.get("device", "unknown")
        if dev_val in ("mobile", "phone_dial_in", "phone_dialin", "likely_mobile"):
            dev = "Mobile"
        elif dev_val in ("desktop", "likely_desktop"):
            dev = "Desktop"
        else:
            dev = "Unknown (camera off)"
        part_lines.append(
            f"| {r['name']} | {fmt_dur(r['presentMs'])} | {fmt_dur(r['camMs'])} ({r['camPct']}%) | {fmt_dur(r['micMs'])} ({r['micPct']}%) | {dev} |"
        )
    markdown = f"# Meeting Report ({meeting_id})\n\n" + body.strip() + "\n\n" + "\n".join(part_lines)
    s["finalReport"] = markdown
    return {"meeting_id": meeting_id, "markdown": markdown, "engagement": eng}


@app.get("/meetings/{meeting_id}/live")
def get_meeting_live(meeting_id: str):
    """Consolidated real-time payload for extensions and interactive dashboard."""
    s = SESSIONS.get(meeting_id)
    if not s:
        s = get_or_create_session(meeting_id)

    proc = BOT_PROCESSES.get(meeting_id)
    bot_info = {
        "running": proc is not None and proc.returncode is None,
        "pid": proc.pid if proc else None,
    }

    eng = compute_engagement(s)
    present = [p for p in eng if p["present"]]
    snap = s.get("lastSnapshot") or {}
    total = max(len(present), snap.get("total", 0)) if snap.get("total") else len(present)
    cam_on = sum(1 for p in present if p["cam"])
    mic_on = sum(1 for p in present if p["mic"])
    desktop_count = sum(1 for p in present if p.get("device") != "mobile")
    mobile_count = sum(1 for p in present if p.get("device") == "mobile")

    turns = build_turns(s)
    recent_turns = [
        {"timestamp": fmt_clock(t["offsetMs"]), "speaker": t["speaker"], "text": t["text"]}
        for t in turns
    ]

    return {
        "meeting_id": meeting_id,
        "bot": bot_info,
        "started_at": s.get("startedAt"),
        "ended_at": s.get("endedAt"),
        "captions_count": len(s.get("order", [])),
        "headcount": {
            "total": total,
            "cam_on": cam_on,
            "cam_off": max(0, total - cam_on),
            "mic_on": mic_on,
            "mic_off": max(0, total - mic_on),
            "desktop_count": desktop_count,
            "mobile_count": mobile_count,
        },
        "participants": present,
        "transcript": recent_turns,
        "live_summary": s.get("liveState"),
        "final_report": s.get("finalReport"),
    }


# --------------- Web Dashboard Routes ---------------

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_view():
    dash_path = TEMPLATES_DIR / "dashboard.html"
    if dash_path.exists():
        return HTMLResponse(content=dash_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Meet Intelligence Dashboard</h1><p>Template not found.</p>")


# --------------- Bot Management ---------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_DIR = REPO_ROOT / "bot"
BOT_PROCESSES: Dict[str, asyncio.subprocess.Process] = {}


def extract_meeting_id(url: str) -> str:
    m = re.search(r"/([a-z]{3}-[a-z]{4}-[a-z]{3})", url)
    return m.group(1) if m else url.strip().split("/")[-1].split("?")[0]


class LaunchBotReq(BaseModel):
    url: str
    name: str = "Notes Bot"


@app.post("/bot/launch")
async def launch_bot(req: LaunchBotReq):
    meeting_id = extract_meeting_id(req.url)
    if meeting_id in BOT_PROCESSES:
        proc = BOT_PROCESSES[meeting_id]
        if proc.poll() is None:
            return {"meeting_id": meeting_id, "pid": proc.pid, "status": "already_running"}

    bot_script = str(BOT_DIR / "runner.py")
    profile = str(REPO_ROOT / "bot-profile")
    out_dir = str(REPO_ROOT / "session_output")
    backend_url = "http://127.0.0.1:8000"

    log_path = REPO_ROOT / "session_output" / "bot.log"
    log_file = open(log_path, "a", encoding="utf-8")

    proc = subprocess.Popen(
        [
            sys.executable, bot_script, req.url,
            "--name", req.name,
            "--backend", backend_url,
            "--profile", profile,
            "--out-dir", out_dir,
        ],
        stdout=log_file,
        stderr=log_file,
    )
    BOT_PROCESSES[meeting_id] = proc
    get_or_create_session(meeting_id)
    return {"meeting_id": meeting_id, "pid": proc.pid, "status": "launched", "out": out_dir}


@app.get("/bot/{meeting_id}/status")
async def bot_status(meeting_id: str):
    proc = BOT_PROCESSES.get(meeting_id)
    if not proc:
        # Check if status.json exists for this meeting
        status_file = REPO_ROOT / "session_output" / "status.json"
        if status_file.exists():
            try:
                data = json.loads(status_file.read_text(encoding="utf-8"))
                if data.get("meeting_id") == meeting_id:
                    return {
                        "meeting_id": meeting_id,
                        "pid": None,
                        "running": data.get("status") == "running",
                        "exit_code": 0 if data.get("status") == "running" else 1,
                    }
            except Exception:
                pass
        return {
            "meeting_id": meeting_id,
            "pid": None,
            "running": False,
            "exit_code": None,
        }

    running = proc.poll() is None
    return {
        "meeting_id": meeting_id,
        "pid": proc.pid,
        "running": running,
        "exit_code": proc.returncode,
    }


@app.post("/bot/{meeting_id}/stop")
async def stop_bot(meeting_id: str):
    proc = BOT_PROCESSES.get(meeting_id)
    if not proc:
        return {"meeting_id": meeting_id, "stopped": True, "detail": "No running process found"}
    if proc.poll() is None:
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    return {"meeting_id": meeting_id, "stopped": True, "exit_code": proc.returncode}
