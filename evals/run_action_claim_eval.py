"""Precision/recall/F1 evaluation harness for the action-claim verifier
(Step 8, Part A) - run against a real Groq classifier call, real dataset,
NOT part of the pytest suite (lives under evals/, same reasoning as
scripts/smoke_test.py: needs live API access, and its point is to measure
real model behavior, not to gate CI).

Establishes the baseline BEFORE any verifier changes (Step 8 Part A2),
then re-run unchanged after Part B/C to get a direct before/after
comparison - the harness calls `action_claims.check_draft`, whose return
shape is kept stable across the Part B rewrite specifically so this
script doesn't need to change between runs.

Run with: uv run python evals/run_action_claim_eval.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from dealership_agent.agents.action_claims import check_draft
from dealership_agent.config import get_settings
from dealership_agent.llm.factory import get_llm_provider

DATASET_PATH = Path(__file__).parent / "datasets" / "action_claims.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    cases = []
    with open(DATASET_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def main() -> None:
    settings = get_settings()
    if settings.llm_provider != "groq":
        raise RuntimeError(
            f"evals/run_action_claim_eval.py is meant to run against Groq for "
            f"local dev (CLAUDE.md), but LLM_PROVIDER={settings.llm_provider!r} - "
            f"check .env."
        )
    llm = get_llm_provider()
    model = settings.llm_model_classifier

    cases = _load_cases()
    print(f"Loaded {len(cases)} labelled cases from {DATASET_PATH}")

    # Confusion matrix: positive class = VIOLATION.
    tp = fp = tn = fn = 0
    unparseable_ids: list[str] = []
    unparseable_correct = 0  # unparseable case whose fail-closed guess happened to be right
    unparseable_incorrect = 0
    mistakes: list[dict[str, Any]] = []

    for case in cases:
        evidence = case["available_tool_results"]
        expected = case["expected_label"]
        outcome = check_draft(llm, model, case["draft_text"], evidence)
        predicted = outcome["label"]

        if outcome["unparseable"]:
            unparseable_ids.append(case["id"])
            if predicted == expected:
                unparseable_correct += 1
            else:
                unparseable_incorrect += 1

        if predicted == "VIOLATION" and expected == "VIOLATION":
            tp += 1
        elif predicted == "VIOLATION" and expected == "CLEAN":
            fp += 1
        elif predicted == "CLEAN" and expected == "CLEAN":
            tn += 1
        else:
            fn += 1

        if predicted != expected:
            mistakes.append(
                {
                    "id": case["id"],
                    "expected": expected,
                    "predicted": predicted,
                    "unparseable": outcome["unparseable"],
                    "stage1_detected": outcome["stage1_detected"],
                    "skipped_precheck": outcome["skipped_precheck"],
                    "draft_text": case["draft_text"],
                    "rationale": case["rationale"],
                }
            )

    violation_precision, violation_recall, violation_f1 = _precision_recall_f1(tp, fp, fn)
    # CLEAN as the positive class: swap tp/fp/fn accordingly.
    clean_precision, clean_recall, clean_f1 = _precision_recall_f1(tn, fn, fp)

    print("\n" + "=" * 88)
    print("CONFUSION MATRIX (positive class = VIOLATION)")
    print(f"  TP={tp}  FP={fp}")
    print(f"  FN={fn}  TN={tn}")
    print("-" * 88)
    print(
        f"VIOLATION class: precision={violation_precision:.3f} "
        f"recall={violation_recall:.3f} f1={violation_f1:.3f}"
    )
    print(
        f"CLEAN class:     precision={clean_precision:.3f} "
        f"recall={clean_recall:.3f} f1={clean_f1:.3f}"
    )
    print(f"Overall accuracy: {(tp + tn) / len(cases):.3f} ({tp + tn}/{len(cases)})")
    print("-" * 88)
    print(
        f"Unparseable verifier output: {len(unparseable_ids)}/{len(cases)} cases "
        f"({unparseable_correct} happened to land on the right label by luck of "
        f"fail-closed defaulting to VIOLATION, {unparseable_incorrect} did not) - "
        "reported separately per Part B4, not conflated with genuine "
        "substantiation judgments in the numbers above."
    )
    if unparseable_ids:
        print(f"  unparseable case ids: {unparseable_ids}")

    print("-" * 88)
    print(f"MISTAKES ({len(mistakes)}):")
    for m in mistakes:
        print(
            f"  [{m['id']}] expected={m['expected']} predicted={m['predicted']} "
            f"unparseable={m['unparseable']} stage1_detected={m['stage1_detected']} "
            f"skipped_precheck={m['skipped_precheck']}"
        )
        print(f"    draft: {m['draft_text'][:150]}")
        print(f"    rationale: {m['rationale']}")

    label_counts = Counter(c["expected_label"] for c in cases)
    print("-" * 88)
    print(f"Dataset composition: {dict(label_counts)}")
    print("=" * 88)


if __name__ == "__main__":
    main()
