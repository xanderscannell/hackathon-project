"""Write the weekly brief: facts from pipeline.py -> Granite (road_weekly_brief flow) -> number check.

    .venv/Scripts/python brief.py              # writes data/out/brief.json
    .venv/Scripts/python brief.py --selftest

The prose is rejected if it holds any number that isn't in the facts; after
three rejected tries the brief is just the facts.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

OUT = Path("data/out")
NUM = re.compile(r"\d+(?:\.\d+)?")
TRIES = 3


def unsupported_numbers(prose, facts):
    """Numbers in prose that appear nowhere in the facts."""
    # ponytail: checks numbers only, not which road a number is attached to; a pairing check if Granite starts swapping them
    allowed = set(NUM.findall(" ".join(facts)))
    return sorted(set(NUM.findall(prose)) - allowed, key=float)


def run_flow(facts):
    from ibm_watsonx_orchestrate_clients.common.utils import instantiate_client
    from ibm_watsonx_orchestrate_clients.tools.tool_client import ToolClient
    c = instantiate_client(ToolClient)
    flow_id = c.get_draft_by_name("road_weekly_brief")[0]["id"]
    res = c._post(f"/flows/{flow_id}/run", data={"facts": "\n".join(facts)})
    return res, flow_id


def main():
    facts = json.loads((OUT / "facts.json").read_text())
    attempts = []
    for _ in range(TRIES):
        res, _ = run_flow(facts)
        prose = res.get("brief") or res.get("output", {}).get("brief") if isinstance(res, dict) else None
        if not prose:
            sys.exit(f"unexpected flow response: {json.dumps(res)[:1000]}")
        bad = unsupported_numbers(prose, facts)
        attempts.append({"brief": prose, "rejected_numbers": bad})
        print(prose, "\n", "OK" if not bad else f"REJECTED, numbers not in facts: {bad}", "\n")
        if not bad:
            break
    ok = not attempts[-1]["rejected_numbers"]
    (OUT / "brief.json").write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model": "watsonx/ibm/granite-4-h-small via watsonx Orchestrate",
        "brief": attempts[-1]["brief"] if ok else None,
        "facts": facts,
        "attempts": attempts,
    }, indent=1))


def selftest():
    facts = ["1. Wayne Rd: severity 14.6, first seen Sep 28, 27 days before the 30-day mark."]
    assert unsupported_numbers("Wayne Rd has severity 14.6 and 27 days left.", facts) == []
    assert unsupported_numbers("Wayne Rd has severity 14.7, 3 weeks old.", facts) == ["3", "14.7"]
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
