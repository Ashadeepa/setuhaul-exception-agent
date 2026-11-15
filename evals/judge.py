"""Optional model judge for what code cannot grade: tone, clarity, asking only what is needed.

Pass/fail never depends on the judge. Its scores sit beside the deterministic result,
so a weak judge can mislead a reader but cannot hide a broken booking.
"""
from __future__ import annotations

import json
import re

JUDGE_SYSTEM = """You grade replies from a logistics dispatch assistant to truck drivers. Score each \
criterion from 1 (poor) to 5 (excellent) using the transcript only. Tool results are shown so you can \
check the assistant's claims against them.

Criteria:
- grounded: every dock, time and outcome the assistant states is backed by a tool result
- clear: short, plain, easy to act on from a phone
- asks_only_needed: asks only for facts the tools could not provide, one question at a time
- honest: never implies a booking, hold or escalation that did not happen; says plainly when something failed
- fits_rubric: matches the case's description of a good outcome

Reply with JSON only: {"grounded":n,"clear":n,"asks_only_needed":n,"honest":n,"fits_rubric":n,"comment":"one sentence"}"""


def judge(case: dict, transcript: list[dict], model) -> dict:
    lines = [f"Case: {case['title']}", f"A good outcome: {case['rubric']}", ""]
    for t in transcript:
        lines.append(f"DRIVER: {t['driver']}")
        for tool in t["tools"]:
            lines.append(f"  [tool {tool['tool']} -> {tool['outcome']}]")
        lines.append(f"ASSISTANT: {t['agent']}")
    try:
        resp = model.converse(system=JUDGE_SYSTEM, messages=[{"role": "user", "content": [{"text": "\n".join(lines)}]}],
                              tool_config=None)
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        scores = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        nums = [v for k, v in scores.items() if k != "comment" and isinstance(v, (int, float))]
        scores["overall"] = round(sum(nums) / len(nums), 1) if nums else None
        return scores
    except Exception as e:  # the judge is advisory; never fail the run because of it
        return {"error": f"judge failed: {e}"}
