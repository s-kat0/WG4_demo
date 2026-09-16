from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_maintenance_fixture_is_natural_prose_not_prefabricated_cards() -> None:
    fixture = json.loads((PROJECT_ROOT / "data" / "maintenance1.json").read_text("utf-8"))

    assert fixture["segments"] == [
        {
            "key": "maintenance1-p1",
            "text": (
                "冷却器1では、温度計を交換してから出口温度が高めに表示されるように"
                "なったため、念のため別の計器とも突き合わせた。"
            ),
        },
        {
            "key": "maintenance1-p2",
            "text": "ただし、表示差の原因までは特定できていない。",
        },
    ]


def test_extraction_prompt_requires_concise_grounded_fact_text() -> None:
    prompt = (PROJECT_ROOT / "prompts" / "extraction.md").read_text("utf-8")

    assert "原文をそのまま分割・転記せず" in prompt
    assert "原則30文字以内" in prompt
    assert "未確定の内容を確定表現に変えたりしてはいけません" in prompt
    assert "kindがconditionのfactにはcondition_scopeを必ず設定" in prompt
    assert "kindがcondition以外のfactではcondition_scopeをnull" in prompt
    assert "parent_action_fact_idはすべてnull" in prompt
    assert "原文の該当引用" in prompt
