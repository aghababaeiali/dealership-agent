"""Eval for Step 8, Part D1: when a sales search returns real vehicles,
synthesis must actually name specific ones (year, make, model, price,
mileage), not summarize vaguely or pivot to an unrelated point - Step 7's
live conversation 1 found 4 real SUVs and named none of them.

Real Groq call, real prompt construction matching
agents/nodes.py::make_synthesis_node - NOT part of the pytest suite (see
evals/run_action_claim_eval.py for the same reasoning).

Run with: uv run python evals/run_synthesis_quality_eval.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dealership_agent.agents.prompts import SYNTHESIS_SYSTEM_PROMPT
from dealership_agent.config import get_settings
from dealership_agent.llm.base import Message
from dealership_agent.llm.factory import get_llm_provider

DATASET_PATH = Path(__file__).parent / "datasets" / "synthesis_quality.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    cases = []
    with open(DATASET_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _vehicles_named(response: str, vehicles: list[dict[str, Any]]) -> int:
    lowered = response.lower()
    named = 0
    for vehicle in vehicles:
        make_model = f"{vehicle['make']} {vehicle['model']}".lower()
        price_str = f"{vehicle['price']:,.0f}".replace(",", "")
        price_variants = (f"${vehicle['price']:,.2f}", f"${price_str}", f"${vehicle['price']:,.0f}")
        if make_model in lowered or any(p.lower() in lowered for p in price_variants):
            named += 1
    return named


def main() -> None:
    settings = get_settings()
    if settings.llm_provider != "groq":
        raise RuntimeError(
            f"evals/run_synthesis_quality_eval.py is meant to run against Groq "
            f"for local dev (CLAUDE.md), but LLM_PROVIDER={settings.llm_provider!r} "
            f"- check .env."
        )
    llm = get_llm_provider()
    cases = _load_cases()
    print(f"Loaded {len(cases)} cases from {DATASET_PATH}")

    passed = 0
    for case in cases:
        sales_result = case["sales_result"]
        vehicles = sales_result["tool_calls"][0]["result"]
        payload = {"sales": sales_result, "account": None, "escalation": None}
        messages = [
            Message(role="system", content=SYNTHESIS_SYSTEM_PROMPT),
            Message(role="user", content=case["user_message"]),
            Message(role="user", content=f"Agent results: {json.dumps(payload, default=str)}"),
        ]
        response = llm.complete(messages, model=settings.llm_model_synthesis)

        expected = min(case["min_vehicles_expected"], len(vehicles))
        named = _vehicles_named(response, vehicles)
        ok = named >= expected
        passed += int(ok)

        print("\n" + "-" * 88)
        print(
            f"[{case['id']}] {'PASS' if ok else 'FAIL'} - named {named}/{len(vehicles)} "
            f"vehicles (needed >= {expected})"
        )
        print(f"  rationale: {case['rationale']}")
        print(f"  response: {response}")

    print("\n" + "=" * 88)
    print(f"{passed}/{len(cases)} cases passed")


if __name__ == "__main__":
    main()
