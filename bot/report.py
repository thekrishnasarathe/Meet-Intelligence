"""
report.py - meeting report that works for meetings of ANY length under a hard per-call token cap.

Why reports failed on long meetings: the whole transcript was sent to the model in one call, so a
long meeting went past the 7000-token limit. This pipeline never sends more than MAX_INPUT_TOKENS in
any single call, and every step has a fallback so a report is ALWAYS produced.

  1. MAP     split the transcript at speaker-turn boundaries into ~3.8k-token chunks and extract compact
             structured notes from each chunk (topics, speaker points, decisions, action items, questions).
             Runs while the meeting is live (feed()), so almost nothing is left to do when it ends.
  2. REDUCE  merge the narrative part of the notes (topics + speaker points) in batches, repeatedly, until it
             fits NOTES_BUDGET. Decisions/actions/questions are NOT pushed through the model again: they are
             merged and de-duplicated in code, so none can be lost or paraphrased away.
  3. FINAL   two small calls (executive summary, speaker contributions). Timeline, decisions, action items,
             open questions and participation tables are rendered by code.

Because every model output is capped at MAX_OUTPUT_TOKENS, every note object is small, so each REDUCE round at
least halves the number of parts and the loop terminates. Failures degrade the report instead of stopping it.
"""
import difflib
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

MAX_INPUT_TOKENS = 7000   # hard cap for system + prompt of ANY single call (set to your real limit)
MAX_OUTPUT_TOKENS = 1500  # cap for any single answer
CHUNK_TOKENS = 3800       # transcript tokens per MAP call
REDUCE_BATCH_TOKENS = 4500
NOTES_BUDGET = 4200       # merged narrative must fit this before the FINAL calls
MAX_REDUCE_ROUNDS = 8


def est_tokens(text: str) -> int:
    """Deliberately conservative (3 chars per token also covers Hinglish and other scripts)."""
    return math.ceil(len(text) / 3.0)


class PromptTooLarge(Exception):
    pass


class TokenPacer:
    """Sliding 60-second window rate limiter for tokens."""

    def __init__(self, tpm: int, time_fn=time.time, sleep_fn=time.sleep):
        self.tpm = tpm
        self.time_fn = time_fn
        self.sleep_fn = sleep_fn
        self.history: list[tuple[float, int]] = []

    def acquire(self, n: int) -> None:
        if n > self.tpm:
            raise PromptTooLarge(f"Token count {n} exceeds TPM limit {self.tpm}")
        while True:
            now = self.time_fn()
            cutoff = now - 60.0
            while self.history and self.history[0][0] <= cutoff:
                self.history.pop(0)
            current_tokens = sum(tok for _, tok in self.history)
            if current_tokens + n <= self.tpm:
                self.history.append((now, n))
                return
            oldest_ts, _ = self.history[0]
            wait = (oldest_ts + 60.0) - now
            if wait > 0:
                self.sleep_fn(wait)
            else:
                self.sleep_fn(0.001)


def clock(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ----------------------------------------------------------------------------- transcript helpers
def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def turns_from_jsonl(path, merge_gap_s: float = 20.0, meeting_id: Optional[str] = None):
    """Collapse caption upserts (same id -> latest text), then merge consecutive same-speaker segments."""
    segs, t0 = {}, None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        mid = (ev.get("meeting_id") or ev.get("meetingId") or "").lower()
        if meeting_id and mid and mid != meeting_id.lower():
            continue
        if ev.get("type") == "call_started" and t0 is None:
            t0 = _ts(ev["ts"])
        if ev.get("type") == "caption" and ev.get("text"):
            segs[ev["id"]] = ev
    ordered = sorted(segs.values(), key=lambda e: _ts(e["start_ts"]))
    if not ordered:
        return []
    t0 = t0 if t0 is not None else _ts(ordered[0]["start_ts"])
    turns = []
    for e in ordered:
        start, end = _ts(e["start_ts"]), _ts(e["end_ts"])
        if turns and turns[-1]["speaker"] == e["speaker"] and start - turns[-1]["end"] < merge_gap_s:
            turns[-1]["text"] += " " + e["text"]
            turns[-1]["end"] = end
        else:
            turns.append({"t": max(0.0, start - t0), "end": end, "speaker": e["speaker"], "text": e["text"]})
    return turns


def render(turn) -> str:
    return f"[{clock(turn['t'])}] {turn['speaker']}: {turn['text']}"


def _split_text(text: str, max_chars: int):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:  # one absurdly long "sentence": hard split
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:max_chars])
            s = s[max_chars:]
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_turns(turns, chunk_tokens: int = CHUNK_TOKENS):
    """Group turns into chunks of at most chunk_tokens, splitting only an over-long single turn."""
    chunks, cur, cur_tok = [], [], 0
    for t in turns:
        pieces = [t]
        if est_tokens(render(t)) > chunk_tokens:
            max_chars = chunk_tokens * 3 - 120
            pieces = [dict(t, text=s) for s in _split_text(t["text"], max_chars)]
        for p in pieces:
            n = est_tokens(render(p))
            if cur and cur_tok + n > chunk_tokens:
                chunks.append(cur)
                cur, cur_tok = [], 0
            cur.append(p)
            cur_tok += n
    if cur:
        chunks.append(cur)
    return chunks


# ----------------------------------------------------------------------------- notes helpers
EMPTY = {"topics": [], "speaker_points": [], "decisions": [], "action_items": [], "open_questions": []}


def parse_notes(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    data = json.loads(raw)  # JSONDecodeError is a ValueError: truncated/garbled output triggers a split-and-retry
    if not isinstance(data, dict):
        raise ValueError("notes must be a JSON object")
    notes = {k: list(data.get(k) or []) for k in EMPTY}
    notes["topics"] = [t for t in notes["topics"] if isinstance(t, dict)]
    notes["speaker_points"] = [s for s in notes["speaker_points"] if isinstance(s, dict) and s.get("speaker")]
    notes["action_items"] = [a for a in notes["action_items"] if isinstance(a, dict) and a.get("task")]
    notes["decisions"] = [str(d) for d in notes["decisions"]]
    notes["open_questions"] = [str(q) for q in notes["open_questions"]]
    return notes


def merge_plain(a: dict, b: dict) -> dict:
    out = {k: list(a.get(k, [])) + list(b.get(k, [])) for k in EMPTY}
    by_speaker = {}
    for sp in out["speaker_points"]:
        by_speaker.setdefault(sp["speaker"], []).extend(sp.get("points") or [])
    out["speaker_points"] = [{"speaker": s, "points": p} for s, p in by_speaker.items()]
    return out


def hard_trim(part: dict, max_topics: int = 12, max_points: int = 6) -> dict:
    """Deterministic fallback compression (no model): evenly sample topics, cap points per speaker."""
    topics = part.get("topics", [])
    if len(topics) > max_topics:
        step = len(topics) / max_topics
        topics = [topics[int(i * step)] for i in range(max_topics)]
    sps = [{"speaker": s["speaker"], "points": (s.get("points") or [])[:max_points]}
           for s in part.get("speaker_points", [])]
    return {"topics": topics, "speaker_points": sps}


def dedupe(items, key=lambda x: x, threshold: float = 0.9):
    """Drop near-duplicates. Items whose numbers differ are never merged ("report 12" vs "report 13")."""
    kept, keys = [], []
    for it in items:
        k = re.sub(r"[^a-z0-9 ]", "", str(key(it)).lower()).strip()
        if not k:
            continue
        digits = re.findall(r"\d+", k)
        if any(re.findall(r"\d+", o) == digits and difflib.SequenceMatcher(None, k, o).ratio() >= threshold
               for o in keys):
            continue
        kept.append(it)
        keys.append(k)
    return kept


# ----------------------------------------------------------------------------- prompts
MAP_SYSTEM = (
    "You extract structured notes from ONE segment of a meeting transcript. "
    "The meeting speech may be in English, Hindi, or Hinglish (Hindi written in Latin or Devanagari script). "
    "Accurately interpret Hindi/Hinglish technical terminology, idioms, and context, and output notes in clear English. "
    "Return ONLY a JSON object with keys: "
    '"topics" [{"at": "mm:ss", "topic": str, "summary": str}], "speaker_points" [{"speaker": str, "points": [str]}], '
    '"decisions" [str], "action_items" [{"owner": str|null, "task": str, "due": str|null}], "open_questions" [str]. '
    "Use only the transcript. Keep each summary under 25 words and each point under 20 words. "
    "Captions are machine generated and may contain recognition errors."
)
REDUCE_SYSTEM = (
    "You merge notes from consecutive parts of one meeting into ONE object with keys \"topics\" "
    '[{"at": "mm:ss", "topic": str, "summary": str}] and "speaker_points" [{"speaker": str, "points": [str]}]. '
    "Merge duplicate or adjacent topics, keep the earliest timestamp of each, keep at most {max_topics} topics and at "
    "most {max_points} points per speaker. Return ONLY JSON. Never invent content."
)
SUMMARY_SYSTEM = (
    "Write the executive summary of a meeting in at most 150 words of plain prose, from the notes you are given. "
    "Mention the main outcomes and what happens next. Do not add facts. Output only the summary text."
)
SPEAKERS_SYSTEM = (
    "From the notes, write a Markdown bullet list: one bullet per speaker with their main points and stance, at most "
    "40 words each. Do not add facts. Output only the bullet list."
)


# ----------------------------------------------------------------------------- Gemini adapter
def gemini_llm(api_key: str, model: str, retries: int = 5, sleep=time.sleep, extra_config=None):
    """Returns llm(system, prompt, json_mode, max_out) -> str. Retries 429/5xx with exponential backoff.

    extra_config is merged into generationConfig. Thinking models count thinking tokens against
    maxOutputTokens, so set a low/zero thinking budget here for extraction (see current Gemini docs for the
    exact field of your model) or answers can be cut off.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def call(system, prompt, json_mode, max_out):
        cfg = {"temperature": 0.2, "maxOutputTokens": max_out}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        cfg.update(extra_config or {})
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": cfg,
        }).encode()
        delay = 2.0
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": api_key})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    sleep(delay)
                    delay *= 2
                    continue
                raise
            cand = (data.get("candidates") or [{}])[0]
            if cand.get("finishReason") == "MAX_TOKENS":
                raise ValueError("model output was truncated")
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            if not text:
                raise ValueError("empty model response")
            return text
        raise RuntimeError("retries exhausted")

    return call


def groq_llm(api_key: str, model: str, retries: int = 5, sleep_fn=time.sleep):
    """Returns llm(system, prompt, json_mode, max_out) -> str for Groq.

    POSTs to https://api.groq.com/openai/v1/chat/completions with Bearer auth and a custom User-Agent.
    Sends max_completion_tokens, and response_format: json_object when JSON mode is on.
    On 429, sleeps for Retry-After plus 0.5s and retries; raises RuntimeError if wait > 120s.
    On 413, raises PromptTooLarge.
    If finish_reason is length, raises ValueError.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"

    def call(system, prompt, json_mode, max_out):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_out,
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "MeetIntelBot/2.0",
        }

        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 413:
                    raise PromptTooLarge("Payload Too Large (HTTP 413)") from e
                if e.code == 429:
                    raw_err = ""
                    try:
                        raw_err = e.read().decode("utf-8", errors="ignore")
                    except Exception:
                        pass
                    retry_after_str = e.headers.get("Retry-After") if e.headers else None
                    if retry_after_str is not None:
                        try:
                            wait_time = float(retry_after_str)
                        except (ValueError, TypeError):
                            wait_time = 2.0
                    else:
                        m = re.search(r"try again in (\d+(?:\.\d+)?)s", raw_err)
                        if m:
                            wait_time = float(m.group(1))
                        elif any(term in raw_err.lower() for term in ("daily", "per day", "tpd", "rpd")):
                            raise RuntimeError(f"Daily rate limit reached: {raw_err}")
                        else:
                            wait_time = 2.0
                    if wait_time > 120:
                        raise RuntimeError(f"Rate limit retry wait {wait_time}s exceeds 120s limit")
                    sleep_fn(wait_time + 0.5)
                    continue
                if e.code in (500, 502, 503, 504) and attempt < retries:
                    sleep_fn(2.0 * attempt)
                    continue
                raise

            choices = data.get("choices") or []
            if not choices:
                raise ValueError("empty model response: no choices")
            cand = choices[0]
            if cand.get("finish_reason") == "length":
                raise ValueError("model output exceeded max_completion_tokens (finish_reason=length)")
            text = (cand.get("message") or {}).get("content", "")
            if not text:
                raise ValueError("empty model response")
            return text

    return call


# ----------------------------------------------------------------------------- builder
class ReportBuilder:
    def __init__(self, llm, checkpoint=None, *,
                 max_input: int = MAX_INPUT_TOKENS,
                 max_output: int = MAX_OUTPUT_TOKENS,
                 tpm: int | None = None,
                 pacer: TokenPacer | None = None,
                 chunk_tokens: int | None = None,
                 log=lambda *_: None):
        self.llm = llm
        self.log = log
        self.checkpoint = Path(checkpoint) if checkpoint else None

        if tpm is not None:
            eff = 0.9 * tpm
            max_out = min(max_output, eff // 5)
            cap_in = min(max_input, eff - max_out)
            max_out = min(max_out, cap_in // 4)
            self.max_out = int(max_out)
            self.cap_in = int(cap_in)
            self.chunk_tokens = int(0.54 * self.cap_in) if chunk_tokens is None else chunk_tokens
            self.reduce_batch = int(0.64 * self.cap_in)
            self.notes_budget = int(0.6 * self.cap_in)
            self.max_topics = max(4, self.max_out // 120)
            self.max_points = max(3, self.max_out // 250)
            self.pacer = pacer if pacer is not None else TokenPacer(tpm)
        else:
            self.cap_in = max_input
            self.max_out = max_output
            self.chunk_tokens = CHUNK_TOKENS if chunk_tokens is None else chunk_tokens
            self.reduce_batch = REDUCE_BATCH_TOKENS
            self.notes_budget = NOTES_BUDGET
            self.max_topics = max(4, self.max_out // 120)
            self.max_points = max(3, self.max_out // 250)
            self.pacer = pacer

        if hasattr(self.llm, "set_caps"):
            self.llm.set_caps(self.cap_in, self.max_out)
        elif hasattr(self.llm, "cap_in") and hasattr(self.llm, "max_out"):
            self.llm.cap_in = self.cap_in
            self.llm.max_out = self.max_out

        self.buffer = []            # turns not yet mapped
        self.notes = []             # mapped chunk notes, in order
        self.cache = {}             # sha1(chunk text) -> notes (makes re-runs and crashes cheap)
        self.degraded = 0           # chunks/merges that fell back to code-only handling
        self.calls = 0
        if self.checkpoint and self.checkpoint.exists():
            self.cache = json.loads(self.checkpoint.read_text(encoding="utf-8"))

    # -- single guarded model call
    def _call(self, system, prompt, json_mode):
        inp = est_tokens(system) + est_tokens(prompt)
        if inp > self.cap_in:
            raise PromptTooLarge(f"Input estimate {inp} exceeds cap {self.cap_in}")
        if self.pacer:
            self.pacer.acquire(inp + self.max_out)
        self.calls += 1
        return self.llm(system, prompt, json_mode, self.max_out)

    def _save(self):
        if self.checkpoint:
            self.checkpoint.write_text(json.dumps(self.cache), encoding="utf-8")

    # -- MAP
    def _map_chunk(self, chunk, depth: int = 0) -> dict:
        text = "\n".join(render(t) for t in chunk)
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        try:
            notes = parse_notes(self._call(MAP_SYSTEM, "TRANSCRIPT:\n" + text, True))
        except (PromptTooLarge, ValueError) as err:
            self.log("map failed, splitting:", type(err).__name__)
            if len(chunk) > 1 and depth < 6:
                mid = len(chunk) // 2
                notes = merge_plain(self._map_chunk(chunk[:mid], depth + 1), self._map_chunk(chunk[mid:], depth + 1))
            else:
                notes = self._degraded_notes(chunk)
        except Exception as err:  # network/quota after retries: keep going, never lose the meeting
            self.log("map error:", err)
            notes = self._degraded_notes(chunk)
        self.cache[key] = notes
        self._save()
        return notes

    def _degraded_notes(self, chunk) -> dict:
        self.degraded += 1
        first, last = chunk[0], chunk[-1]
        return {"topics": [{"at": clock(first["t"]), "topic": "Segment not summarized",
                            "summary": f"AI summary unavailable for {clock(first['t'])}-{clock(last['t'])}; see transcript."}],
                "speaker_points": [], "decisions": [], "action_items": [], "open_questions": []}

    def feed(self, turns):
        """Call as the meeting runs. Full chunks are mapped immediately; the tail waits for more speech."""
        chunks = chunk_turns(self.buffer + list(turns), self.chunk_tokens)
        if not chunks:
            return
        for c in chunks[:-1]:
            self.notes.append(self._map_chunk(c))
        self.buffer = chunks[-1]

    # -- REDUCE
    def _narrative(self, notes_list) -> dict:
        parts = [{"topics": n["topics"], "speaker_points": n["speaker_points"]} for n in notes_list]
        if not parts:
            return {"topics": [], "speaker_points": []}
        for rnd in range(MAX_REDUCE_ROUNDS + 1):
            if est_tokens(json.dumps(parts, ensure_ascii=False)) <= self.notes_budget:
                break
            if rnd == MAX_REDUCE_ROUNDS:  # did not converge: finish deterministically
                acc = {"topics": [], "speaker_points": []}
                for p in parts:
                    acc = merge_plain(acc, p)
                self.degraded += 1
                return hard_trim(acc, max_topics=self.max_topics, max_points=self.max_points)
            batches, cur, cur_tok = [], [], 0
            for p in parts:
                n = est_tokens(json.dumps(p, ensure_ascii=False))
                if cur and cur_tok + n > self.reduce_batch:
                    batches.append(cur)
                    cur, cur_tok = [], 0
                cur.append(p)
                cur_tok += n
            batches.append(cur)
            merged = []
            for b in batches:
                try:
                    system = REDUCE_SYSTEM.replace("{max_topics}", str(self.max_topics)).replace("{max_points}", str(self.max_points))
                    merged.append(parse_notes(self._call(system, "PARTS:\n" + json.dumps(b, ensure_ascii=False), True)))
                except Exception as err:
                    self.log("reduce fallback:", type(err).__name__)
                    self.degraded += 1
                    acc = {"topics": [], "speaker_points": []}
                    for p in b:
                        acc = merge_plain(acc, p)
                    merged.append(hard_trim(acc, max_topics=self.max_topics, max_points=self.max_points))
            new_parts = [{"topics": m["topics"], "speaker_points": m["speaker_points"]} for m in merged]
            if len(new_parts) >= len(parts):
                acc = {"topics": [], "speaker_points": []}
                for p in new_parts:
                    acc = merge_plain(acc, p)
                self.degraded += 1
                return hard_trim(acc, max_topics=self.max_topics, max_points=self.max_points)
            parts = new_parts
        acc = {"topics": [], "speaker_points": []}
        for p in parts:
            acc = merge_plain(acc, p)
        return acc

    def summary_so_far(self) -> dict:
        """Cheap mid-meeting catch-up: notes so far plus the unmapped tail."""
        notes = list(self.notes)
        if self.buffer:
            notes.append(self._map_chunk(self.buffer))
        return self._collect(notes)

    def _collect(self, notes) -> dict:
        nar = self._narrative(notes)
        allm = {k: [x for n in notes for x in n[k]] for k in ("decisions", "action_items", "open_questions")}
        return {
            "topics": nar["topics"],
            "speaker_points": nar["speaker_points"],
            "decisions": dedupe(allm["decisions"]),
            "action_items": dedupe(allm["action_items"], key=lambda a: f"{a.get('owner')} {a.get('task')}"),
            "open_questions": dedupe(allm["open_questions"]),
        }

    # -- FINAL
    def finalize(self, meta: dict, participation_md: str = "") -> str:
        if self.buffer:
            self.notes.append(self._map_chunk(self.buffer))
            self.buffer = []
        c = self._collect(self.notes)
        context = json.dumps({"topics": c["topics"], "decisions": c["decisions"][:15],
                              "action_items": c["action_items"][:15]}, ensure_ascii=False)
        speakers = json.dumps(c["speaker_points"], ensure_ascii=False)

        try:
            summary = self._call(SUMMARY_SYSTEM, "NOTES:\n" + context, False).strip()
        except Exception as err:
            self.log("summary fallback:", type(err).__name__)
            self.degraded += 1
            summary = "AI summary unavailable. The sections below were built directly from extracted notes."
        try:
            speaker_md = self._call(SPEAKERS_SYSTEM, "NOTES:\n" + speakers, False).strip()
        except Exception as err:
            self.log("speakers fallback:", type(err).__name__)
            self.degraded += 1
            speaker_md = "\n".join(f"- **{s['speaker']}**: " + "; ".join(s.get("points", [])[:4])
                                   for s in c["speaker_points"]) or "None recorded."

        lines = ["# Meeting report", ""]
        lines += [f"- {k}: {v}" for k, v in meta.items()]
        lines += ["", "## Executive summary", summary, "", "## Topic timeline"]
        lines += [f"- [{t.get('at', '--:--')}] **{t.get('topic', '')}**: {t.get('summary', '')}" for t in c["topics"]] \
            or ["None recorded."]
        lines += ["", "## Speaker contributions", speaker_md, "", "## Decisions"]
        lines += [f"- {d}" for d in c["decisions"]] or ["None recorded."]
        lines += ["", "## Action items"]
        if c["action_items"]:
            lines += ["| Owner | Task | Due |", "| --- | --- | --- |"]
            lines += [f"| {a.get('owner') or '-'} | {a['task']} | {a.get('due') or '-'} |" for a in c["action_items"]]
        else:
            lines.append("None recorded.")
        lines += ["", "## Open questions"] + ([f"- {q}" for q in c["open_questions"]] or ["None recorded."])
        if participation_md:
            lines += ["", participation_md]
        if self.degraded:
            lines += ["", f"> {self.degraded} part(s) of this report could not be summarized by the AI and were "
                          "handled without it. The full transcript is unaffected."]
        return "\n".join(lines) + "\n"


def build_report(turns, llm, meta, participation_md="", checkpoint=None, log=lambda *_: None,
                 max_input=MAX_INPUT_TOKENS, max_output=MAX_OUTPUT_TOKENS, tpm=None, pacer=None) -> str:
    rb = ReportBuilder(llm, checkpoint=checkpoint, log=log,
                       max_input=max_input, max_output=max_output, tpm=tpm, pacer=pacer)
    rb.feed(turns)
    return rb.finalize(meta, participation_md)


if __name__ == "__main__":
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Build a report from a bot session.jsonl")
    ap.add_argument("jsonl")
    ap.add_argument("--provider", choices=["groq", "gemini"], default="groq")
    ap.add_argument("--model", required=True)
    ap.add_argument("--tpm", type=int, default=None)
    ap.add_argument("--out", default="report.md")
    ap.add_argument("--checkpoint", default="report.ckpt.json")
    a = ap.parse_args()

    tpm = a.tpm
    if a.provider == "groq" and tpm is None:
        tpm = 6000

    if a.provider == "groq":
        key = os.environ["GROQ_API_KEY"]
        llm = groq_llm(key, a.model)
    else:
        key = os.environ["GEMINI_API_KEY"]
        llm = gemini_llm(key, a.model)

    turns = turns_from_jsonl(a.jsonl)
    md = build_report(turns, llm, {"Source": a.jsonl, "Turns": len(turns)},
                      checkpoint=a.checkpoint, tpm=tpm, log=print)
    Path(a.out).write_text(md, encoding="utf-8")
    print("wrote", a.out)
