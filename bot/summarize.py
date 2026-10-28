"""
Standalone summarizer for bot session.jsonl files.
Uses the same prompts and output format as the Chrome extension sidepanel.

Usage:
  python summarize.py session.jsonl
  python summarize.py session.jsonl --key gsk_... --output report.md
  GROQ_API_KEY=gsk_... python summarize.py session.jsonl
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

SYSTEM_FINAL = (
    "You write meeting reports from speaker-attributed, timestamped transcripts. Use only what the transcript says. "
    "The transcript may contain speech in English, Hindi, or Hinglish (Hindi written in Roman or Devanagari script). "
    "Accurately understand Hindi/Hinglish discussions, synthesizing decisions and actions into professional English. "
    'Output Markdown with exactly these sections: "## Executive summary", "## Topic timeline" (bullets like '
    '"[mm:ss] topic - one line"), "## Speaker contributions" (per speaker: main points and stance), "## Decisions", '
    '"## Action items" (a table with columns Owner | Task | Due), "## Open questions". Write "None recorded." for an '
    'empty section. Captions are machine generated; do not over-interpret odd words. The speaker "You" is the person '
    "who recorded the meeting."
)

FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
]


def parse_iso(ts):
    if not ts:
        return datetime.now(timezone.utc).timestamp() * 1000.0
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0


def fmt_clock(ms):
    total = max(0, int(ms // 1000))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return (f"{h:02d}:" if h else "") + f"{m:02d}:{s:02d}"


def fmt_dur(ms):
    s = max(0, round(ms / 1000))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    return f"{m // 60}h {m % 60:02d}m"


def load_session(filepath):
    """Read a JSONL event file and replay into a session object."""
    session = {
        "meetingId": "unknown",
        "startedAt": None,
        "endedAt": None,
        "segments": {},
        "order": [],
        "media": [],
    }
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            session["meetingId"] = ev.get("meetingId", session["meetingId"])
            t = ev.get("type")
            if t == "call_started":
                if session.get("endedAt"):
                    session["startedAt"] = ev.get("ts")
                    session["endedAt"] = None
                    session["segments"] = {}
                    session["order"] = []
                    session["media"] = []
                else:
                    session["startedAt"] = session["startedAt"] or ev.get("ts")
                    session["endedAt"] = None
            elif t == "call_ended":
                session["endedAt"] = ev.get("ts")
            elif t == "caption":
                if not session["startedAt"]:
                    session["startedAt"] = ev.get("start_ts") or ev.get("ts")
                seg_id = ev["id"]
                if seg_id not in session["segments"]:
                    session["order"].append(seg_id)
                session["segments"][seg_id] = {
                    "id": seg_id,
                    "speaker": ev.get("speaker", "Unknown"),
                    "text": ev.get("text", ""),
                    "start_ts": ev.get("start_ts") or ev.get("ts"),
                    "end_ts": ev.get("end_ts") or ev.get("ts"),
                    "final": bool(ev.get("final", False)),
                }
            elif t in (
                "participant_join", "participant_leave", "participant_update",
                "cam_on", "cam_off", "mic_on", "mic_off",
            ):
                session["media"].append({
                    "type": t,
                    "id": ev.get("id", "unknown"),
                    "name": ev.get("name", "Participant"),
                    "cam": ev.get("cam"),
                    "mic": ev.get("mic"),
                    "device": ev.get("device", "desktop"),
                    "ts": ev.get("ts"),
                })
            elif t == "snapshot":
                session["lastSnapshot"] = {
                    "total": ev.get("total"),
                    "cam": ev.get("cam_on"),
                    "mic": ev.get("mic_on"),
                    "ts": ev.get("ts"),
                }
    return session


def build_turns(session):
    ids = session["order"]
    t0 = parse_iso(session["startedAt"]) if session.get("startedAt") else None
    if t0 is None and ids:
        first_seg = session["segments"].get(ids[0])
        if first_seg and first_seg.get("start_ts"):
            t0 = parse_iso(first_seg["start_ts"])
    turns = []
    for seg_id in ids:
        seg = session["segments"].get(seg_id)
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


def compute_engagement(session):
    now_ms = (
        parse_iso(session["endedAt"])
        if session["endedAt"]
        else datetime.now(timezone.utc).timestamp() * 1000.0
    )
    events = sorted(session["media"], key=lambda e: parse_iso(e.get("ts")))
    people = {}

    def accrue(p, t):
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
                "name": e.get("name", "Participant"),
                "present": False, "cam": False, "mic": False,
                "device": e.get("device", "desktop"),
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

    merged = {}
    for p in people.values():
        clean_name = clean_participant_name(p["name"])
        if is_noise_name(clean_name):
            continue
        is_self = p.get("id") == "self" or clean_name.lower() == "you"
        key = "__self__" if is_self else clean_name.lower()
        if key not in merged:
            merged[key] = {
                "name": clean_name,
                "device": p.get("device", "desktop"),
                "presentMs": p["presentMs"],
                "camMs": p["camMs"],
                "micMs": p["micMs"],
            }
        else:
            if p.get("device") and p["device"] != "desktop":
                merged[key]["device"] = p["device"]
            merged[key]["presentMs"] = max(merged[key]["presentMs"], p["presentMs"])
            merged[key]["camMs"] = max(merged[key]["camMs"], p["camMs"])
            merged[key]["micMs"] = max(merged[key]["micMs"], p["micMs"])

    return [
        {
            "name": p["name"],
            "device": p.get("device", "desktop"),
            "presentMs": round(p["presentMs"]),
            "camMs": round(p["camMs"]),
            "micMs": round(p["micMs"]),
            "camPct": round(100 * p["camMs"] / p["presentMs"]) if p["presentMs"] > 0 else 0,
            "micPct": round(100 * p["micMs"] / p["presentMs"]) if p["presentMs"] > 0 else 0,
        }
        for p in merged.values()
    ]


def call_groq(api_key, prompt, model=None):
    models = [model] + FALLBACK_MODELS if model else list(FALLBACK_MODELS)
    models = list(dict.fromkeys(models))  # dedupe, preserve order
    last_err = ""
    with httpx.Client(timeout=60.0) as client:
        for m in models:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": m,
                    "messages": [
                        {"role": "system", "content": SYSTEM_FINAL},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if text:
                    print(f"  Used model: {m}", file=sys.stderr)
                    return text
            last_err = resp.text
            if resp.status_code not in (429, 404, 503):
                break
    print(f"Groq API error: {last_err}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("input", help="session.jsonl file from the bot")
    ap.add_argument(
        "--key",
        default=os.environ.get("GROQ_API_KEY", ""),
        help="Groq API key (or set GROQ_API_KEY env var)",
    )
    ap.add_argument(
        "--model", default="llama-3.3-70b-versatile", help="preferred Groq model"
    )
    ap.add_argument(
        "--output", "-o", default="", help="output file path (default: print to stdout)"
    )
    args = ap.parse_args()

    if not args.key:
        ap.error("Provide --key or set GROQ_API_KEY env var")

    print(f"Reading {args.input}...", file=sys.stderr)
    session = load_session(args.input)
    turns = build_turns(session)
    if not turns:
        print("No transcript segments found in the file.", file=sys.stderr)
        sys.exit(1)

    eng = compute_engagement(session)
    start = parse_iso(session["startedAt"])
    end = parse_iso(session["endedAt"]) if session["endedAt"] else parse_iso(None)
    duration = end - start
    names = ", ".join(r["name"] for r in eng) or "Unknown"

    print(
        f"Meeting {session['meetingId']}, {len(turns)} turns, "
        f"{len(eng)} participants, {fmt_dur(duration)}",
        file=sys.stderr,
    )
    print("Calling Groq for summary...", file=sys.stderr)

    transcript = "\n".join(
        f"[{fmt_clock(t['offsetMs'])}] {t['speaker']}: {t['text']}" for t in turns
    )
    prompt = (
        f"Meeting {session['meetingId']}, duration {fmt_dur(duration)}. "
        f"Participants: {names}.\n\nTRANSCRIPT:\n{transcript}"
    )
    body = call_groq(args.key, prompt, args.model)

    # Build participation table (same format as sidepanel and backend).
    part_lines = [
        "## Participation",
        "",
        "| Participant | Present | Camera on | Mic unmuted | Device |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in eng:
        dev = "Mobile" if r.get("device") == "mobile" else "Desktop"
        part_lines.append(
            f"| {r['name']} | {fmt_dur(r['presentMs'])} "
            f"| {fmt_dur(r['camMs'])} ({r['camPct']}%) "
            f"| {fmt_dur(r['micMs'])} ({r['micPct']}%) | {dev} |"
        )

    header = (
        f"# Meeting report\n\n"
        f"- Meeting: {session['meetingId']}\n"
        f"- Duration: {fmt_dur(duration)}\n"
        f"- Participants: {names}\n\n"
    )
    report = header + body.strip() + "\n\n" + "\n".join(part_lines) + "\n"

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report saved to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
