"""
Proves the report pipeline survives long meetings under a hard 7000-token per-call cap.
Run:  python tests/report_test.py        (no network, no API key: the model is a strict mock)
The mock FAILS THE TEST if any single call exceeds MAX_INPUT_TOKENS or any answer exceeds MAX_OUTPUT_TOKENS.
"""
import io
import json
import random
import re
import sys
import tempfile
import urllib.error
from collections import Counter
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
import report as R  # noqa: E402

passed = 0


def ok(cond, msg):
    global passed
    assert cond, msg
    passed += 1
    print("  ok  " + msg)


class StrictMock:
    """Stands in for the model. Enforces the token caps and can inject realistic failures."""

    def __init__(self, garble_every=0, fail_final=False, cap_in=R.MAX_INPUT_TOKENS, max_out=R.MAX_OUTPUT_TOKENS):
        self.n = 0
        self.max_in = 0
        self.kinds = Counter()
        self.garble_every = garble_every
        self.fail_final = fail_final
        self.cap_in = cap_in
        self.max_out = max_out

    def set_caps(self, cap_in, max_out):
        self.cap_in = cap_in
        self.max_out = max_out

    def __call__(self, system, prompt, json_mode, max_out):
        self.n += 1
        tin = R.est_tokens(system) + R.est_tokens(prompt)
        self.max_in = max(self.max_in, tin)
        assert tin <= self.cap_in, f"prompt of {tin} tokens exceeds the {self.cap_in} cap"
        assert max_out == self.max_out, f"max_out {max_out} does not match expected {self.max_out}"
        if system.startswith("You extract"):
            self.kinds["map"] += 1
            if self.garble_every and self.n % self.garble_every == 0:
                return '{"topics": [{"at": "00:01", "top'  # truncated JSON, as a real model can do
            out = self._map(prompt)
        elif system.startswith("You merge"):
            self.kinds["reduce"] += 1
            out = self._reduce(prompt)
        elif system.startswith("Write the executive"):
            self.kinds["summary"] += 1
            if self.fail_final:
                raise RuntimeError("quota exhausted")
            out = "The team reviewed progress and agreed next steps."
        else:
            self.kinds["speakers"] += 1
            out = "- **Asha**: led planning."
        assert R.est_tokens(out) <= self.max_out, f"answer ({R.est_tokens(out)}) larger than output cap {self.max_out}"
        return out

    @staticmethod
    def _map(prompt):
        lines = [ln for ln in prompt.split("TRANSCRIPT:\n", 1)[1].split("\n") if ln.strip()]
        m = re.match(r"\[([\d:]+)\] ([^:]+): (.*)", lines[0])
        at, first_text = m.group(1), m.group(3)
        topics = [{"at": at, "topic": first_text[:40], "summary": first_text[:80]}]
        points, actions, decisions = {}, [], []
        for ln in lines:
            mm = re.match(r"\[([\d:]+)\] ([^:]+): (.*)", ln)
            if not mm:
                continue
            sp, text = mm.group(2), mm.group(3)
            points.setdefault(sp, [text[:50]])
            for a in re.findall(r"ACTION: ([^.]*)\.", text):
                actions.append({"owner": sp, "task": a, "due": None})
            for d in re.findall(r"DECIDED: ([^.]*)\.", text):
                decisions.append(d)
        return json.dumps({"topics": topics,
                           "speaker_points": [{"speaker": s, "points": p} for s, p in points.items()],
                           "decisions": decisions, "action_items": actions, "open_questions": []})

    def _reduce(self, prompt):
        parts = json.loads(prompt.split("PARTS:\n", 1)[1])
        acc = {"topics": [], "speaker_points": []}
        for p in parts:
            acc = R.merge_plain(acc, p)
        max_topics = max(4, self.max_out // 120)
        max_points = max(3, self.max_out // 250)
        return json.dumps(R.hard_trim(acc, max_topics=max_topics, max_points=max_points))


WORDS = "budget vendor release roadmap customer latency pipeline review hiring metrics rollout design security".split()
NAMES = ["Asha", "Rahul", "Meera", "Karan", "Divya", "Sameer"]


def synthetic_turns(n_turns, n_actions, n_decisions, seed=7):
    rnd = random.Random(seed)
    turns, t = [], 0.0
    action_slots = set(rnd.sample(range(n_turns), n_actions))
    decision_slots = set(rnd.sample(range(n_turns), n_decisions))
    a = d = 0
    tasks = []
    for i in range(n_turns):
        text = " ".join(rnd.choice(WORDS) for _ in range(30)) + "."
        if i in action_slots:
            task = f"prepare {WORDS[a % len(WORDS)]} document number {a} for team {NAMES[a % 6].lower()}"
            text += f" ACTION: {task}."
            tasks.append(task)
            a += 1
        if i in decision_slots:
            text += f" DECIDED: adopt option {d} for the {rnd.choice(WORDS)} work."
            d += 1
        turns.append({"t": t, "end": t + 10, "speaker": NAMES[i % 6], "text": text})
        t += 12
    return turns, tasks


def main():
    print("long meeting")
    turns, tasks = synthetic_turns(2000, 40, 10)   # roughly a 6.5 hour meeting
    total = sum(R.est_tokens(R.render(t)) for t in turns)
    ok(total > 100_000, f"transcript is ~{total:,} tokens, over 14x the 7000 cap (a single call would have failed)")

    mock = StrictMock()
    md = R.build_report(turns, mock, {"Meeting": "abc-defg-hij", "Duration": "6h 40m"}, participation_md="## Participation\n")
    ok(mock.max_in <= R.MAX_INPUT_TOKENS, f"largest prompt was {mock.max_in} tokens (cap {R.MAX_INPUT_TOKENS})")
    for section in ("## Executive summary", "## Topic timeline", "## Speaker contributions", "## Decisions", "## Action items", "## Open questions"):
        ok(section in md, f"report contains {section}")
    ok(all(task in md for task in tasks), "all 40 action items survive (merged in code, never paraphrased away)")
    ok(md.count("adopt option") == 10, "all 10 decisions survive")
    ok("could not be summarized" not in md, "clean run reports no degraded parts")
    print(f"  info  {mock.n} model calls: {dict(mock.kinds)}")

    print("live catch-up and checkpoint resume")
    with tempfile.TemporaryDirectory() as tmp:
        ckpt = Path(tmp) / "ckpt.json"
        rb = R.ReportBuilder(StrictMock(), checkpoint=ckpt)
        rb.feed(turns[:700])
        so_far = rb.summary_so_far()
        ok(len(so_far["topics"]) > 0 and len(so_far["action_items"]) > 0, "mid-meeting summary works on a partial transcript")
        rb.feed(turns[700:])
        rb.finalize({"Meeting": "x"})
        m2 = StrictMock()
        rb2 = R.ReportBuilder(m2, checkpoint=ckpt)
        rb2.feed(turns)
        rb2.finalize({"Meeting": "x"})
        ok(m2.kinds["map"] == 0, "restart after a crash re-uses saved chunk notes (0 extraction calls)")

    print("failure injection")
    bad = StrictMock(garble_every=3, fail_final=True)
    md2 = R.build_report(turns[:600], bad, {"Meeting": "x"})
    ok("## Executive summary" in md2 and "AI summary unavailable" in md2, "report still produced when the final call fails")
    ok("## Action items" in md2, "action items still present after garbled/truncated model answers")
    ok(bad.max_in <= R.MAX_INPUT_TOKENS, "caps respected even while retrying and splitting")

    print("edge cases")
    huge = [{"t": 0, "end": 5, "speaker": "Asha", "text": ("word " * 9000).strip() + "."}]  # one 45k-char monologue
    m3 = StrictMock()
    md3 = R.build_report(huge, m3, {"Meeting": "x"})
    ok(m3.max_in <= R.MAX_INPUT_TOKENS and "# Meeting report" in md3, "a single huge monologue is split and handled")
    md4 = R.build_report([], StrictMock(), {"Meeting": "x"})
    ok("None recorded." in md4, "empty meeting produces an empty-but-valid report")

    print("transcript loading")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.jsonl"
        ev = [
            {"type": "call_started", "ts": "2026-10-24T12:00:00.000Z"},
            {"type": "caption", "id": "r-1", "speaker": "Asha", "text": "Hello", "start_ts": "2026-10-24T12:00:05.000Z", "end_ts": "2026-10-24T12:00:06.000Z", "final": False},
            {"type": "caption", "id": "r-1", "speaker": "Asha", "text": "Hello everyone", "start_ts": "2026-10-24T12:00:05.000Z", "end_ts": "2026-10-24T12:00:08.000Z", "final": True},
            {"type": "caption", "id": "r-2", "speaker": "Asha", "text": "let us start", "start_ts": "2026-10-24T12:00:12.000Z", "end_ts": "2026-10-24T12:00:14.000Z", "final": True},
            {"type": "caption", "id": "r-3", "speaker": "Rahul", "text": "ok", "start_ts": "2026-10-24T12:00:20.000Z", "end_ts": "2026-10-24T12:00:21.000Z", "final": True},
        ]
        p.write_text("\n".join(json.dumps(e) for e in ev), encoding="utf-8")
        tt = R.turns_from_jsonl(p)
        ok(len(tt) == 2 and tt[0]["text"] == "Hello everyone let us start" and tt[0]["t"] == 5, "caption updates collapse to latest text and same-speaker segments merge")

    test_pacer_fake_clock()
    test_groq_llm()

    print(f"\n{passed} checks passed")


def test_pacer_fake_clock():
    print("sliding-window 6000 TPM pacer (fake clock)")
    sim_time = 0.0

    def fake_time():
        return sim_time

    def fake_sleep(dur):
        nonlocal sim_time
        sim_time += dur

    tpm = 6000
    pacer = R.TokenPacer(tpm, time_fn=fake_time, sleep_fn=fake_sleep)

    # 1. Burst acquire tests
    burst_events = []
    for req in [2500, 2500, 2000, 1500, 2000]:
        pacer.acquire(req)
        burst_events.append((sim_time, req))

    # Assert that at every event timestamp, the trailing 60s window did not exceed 6000 tokens
    for t_now, _ in burst_events:
        window_tokens = sum(tok for t, tok in burst_events if t_now - 60.0 < t <= t_now)
        ok(window_tokens <= tpm, f"burst window at t={t_now:.1f} has {window_tokens} <= {tpm} tokens")

    # 2. End-to-end report build with 6000 TPM pacing and builder caps
    sim_time = 100.0
    pacer = R.TokenPacer(tpm, time_fn=fake_time, sleep_fn=fake_sleep)
    class LoggingMock(StrictMock):
        def __call__(self, system, prompt, json_mode, max_out):
            tin = R.est_tokens(system) + R.est_tokens(prompt)
            call_log.append((fake_time(), tin + max_out))
            return super().__call__(system, prompt, json_mode, max_out)

    call_log = []
    mock = LoggingMock()

    turns, tasks = synthetic_turns(250, 5, 2, seed=42)
    rb = R.ReportBuilder(mock, tpm=tpm, pacer=pacer)
    ok(rb.cap_in == 4320 and rb.max_out == 1080, f"builder caps derived: cap_in={rb.cap_in}, max_out={rb.max_out}")
    ok(mock.cap_in == 4320 and mock.max_out == 1080, "mock uses builder caps")

    rb.feed(turns)
    md = rb.finalize({"Meeting": "pacer-fake-clock"})
    ok("# Meeting report" in md, "report generated successfully under 6000 TPM")
    ok(len(call_log) >= 5, f"pacer regulated {len(call_log)} model calls")
    ok(sim_time > 100.0, f"pacer advanced fake clock by {sim_time - 100.0:.1f}s")

    for t_now, _ in call_log:
        window_tokens = sum(cost for t, cost in call_log if t_now - 60.0 < t <= t_now)
        ok(window_tokens <= tpm, f"sliding window at t={t_now:.1f}s had {window_tokens} <= {tpm} tokens")

    # 3. Test PromptTooLarge when acquire exceeds TPM
    try:
        pacer.acquire(6001)
        ok(False, "should have raised PromptTooLarge for request > tpm")
    except R.PromptTooLarge:
        ok(True, "pacer raised PromptTooLarge when tokens > tpm")


def test_groq_llm():
    print("groq_llm error handling")

    def make_http_error(code, msg, headers_dict=None, body_bytes=b""):
        hdrs = HTTPMessage()
        if headers_dict:
            for k, v in headers_dict.items():
                hdrs.add_header(k, str(v))
        return urllib.error.HTTPError("https://api.groq.com/openai/v1/chat/completions", code, msg, hdrs, io.BytesIO(body_bytes))

    def make_http_response(body_dict):
        data = json.dumps(body_dict).encode("utf-8")
        resp = io.BytesIO(data)
        resp.headers = HTTPMessage()
        return resp

    # 1. 429 with Retry-After
    slept = []

    def fake_sleep(dur):
        slept.append(dur)

    success_resp = {
        "choices": [{
            "message": {"content": "Hello from Groq"},
            "finish_reason": "stop"
        }]
    }

    err_429 = make_http_error(429, "Too Many Requests", {"Retry-After": "1.5"}, b'{"error": "rate limit"}')
    with patch("urllib.request.urlopen", side_effect=[err_429, make_http_response(success_resp)]):
        client = R.groq_llm("test-key", "llama-3.3-70b-versatile", sleep_fn=fake_sleep)
        res = client("system prompt", "user prompt", False, 1000)
        ok(res == "Hello from Groq", "429 with Retry-After sleeps and retries successfully")
        ok(len(slept) == 1 and abs(slept[0] - 2.0) < 0.01, f"slept Retry-After + 0.5s: {slept}")

    # 2. 413 raises PromptTooLarge
    err_413 = make_http_error(413, "Payload Too Large", body_bytes=b'{"error": "too large"}')
    with patch("urllib.request.urlopen", side_effect=err_413):
        client = R.groq_llm("test-key", "llama-3.3-70b-versatile")
        try:
            client("sys", "prompt", False, 1000)
            ok(False, "should have raised PromptTooLarge on 413")
        except R.PromptTooLarge:
            ok(True, "413 raised PromptTooLarge")

    # 3. Daily limit (wait > 120s) raises RuntimeError
    err_daily = make_http_error(429, "Too Many Requests", {"Retry-After": "3600"}, b'{"error": "daily limit exceeded"}')
    with patch("urllib.request.urlopen", side_effect=err_daily):
        client = R.groq_llm("test-key", "llama-3.3-70b-versatile")
        try:
            client("sys", "prompt", False, 1000)
            ok(False, "should have raised RuntimeError on daily limit")
        except RuntimeError as e:
            ok("exceeds 120s limit" in str(e) or "limit" in str(e), f"daily limit raised RuntimeError: {e}")

    # 4. finish_reason == 'length' raises ValueError
    trunc_resp = {
        "choices": [{
            "message": {"content": "Incomplete response..."},
            "finish_reason": "length"
        }]
    }
    with patch("urllib.request.urlopen", return_value=make_http_response(trunc_resp)):
        client = R.groq_llm("test-key", "llama-3.3-70b-versatile")
        try:
            client("sys", "prompt", False, 1000)
            ok(False, "should have raised ValueError on finish_reason=length")
        except ValueError:
            ok(True, "finish_reason=length raised ValueError")


if __name__ == "__main__":
    main()
