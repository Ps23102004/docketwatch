"""AI digest tests: prompt-building and chain loading only.

Never calls a live model -- `_run_lens_core` (the part that actually shells
out to Ollama) is mocked wherever `summarize_new_filings` is exercised, same
pattern as grantradar's `tests/test_match.py`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from llm_ladder.config import load_chains
from llm_ladder.digest import LensResult, LensTake

from docketwatch.ai import (
    CHAINS_PATH,
    DIGEST_CHAIN_TAG,
    SummaryError,
    new_filings_judge_prompt,
    new_filings_lens_prompt,
    summarize_new_filings,
)
from docketwatch.models import Docket, DocketEntry, TrackedCase

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "src" / "docketwatch" / "fixtures"


@pytest.fixture
def case() -> TrackedCase:
    return TrackedCase(
        docket_id="fixture-alpha-1",
        case_name="Redwood Analytics, Inc. v. Harbor Systems LLC",
        docket_number="3:26-cv-00417",
        court="N.D. Cal.",
        source="fixture",
    )


@pytest.fixture
def new_entries() -> list:
    return [
        DocketEntry(
            id="fixture-alpha-e9",
            docket_id="fixture-alpha-1",
            date_filed="2026-05-02",
            entry_number=9,
            description="ORDER setting Case Management Conference for 2026-06-15.",
            source="fixture",
            note="Hand-authored sample entry, not a real filing.",
        )
    ]


# -- chains.yaml -------------------------------------------------------------


def test_chains_yaml_defines_the_digest_chain_with_3_samples_and_0_7_threshold():
    chains = load_chains(str(CHAINS_PATH))
    assert "digest" in chains
    tier = chains["digest"].tiers[0]
    assert tier.model
    assert tier.samples == 3
    assert tier.threshold == 0.7


# -- prompt building -----------------------------------------------------------


def test_lens_prompt_carries_case_and_entry_facts_verbatim(case, new_entries):
    prompt = new_filings_lens_prompt(case, new_entries)
    assert case.case_name in prompt
    assert case.docket_number in prompt
    assert "Case Management Conference" in prompt
    assert "fixture-alpha-e9" in prompt


def test_judge_prompt_labels_each_models_take_and_asks_for_verdict_shape():
    takes = [
        LensTake(model="model-a", take="Something procedural happened."),
        LensTake(model="model-b", take="A hearing was scheduled."),
        LensTake(model="model-c", error="connection refused"),  # unanswered, must be excluded
    ]
    prompt = new_filings_judge_prompt(takes)
    assert "[model-a]: Something procedural happened." in prompt
    assert "[model-b]: A hearing was scheduled." in prompt
    assert "model-c" not in prompt  # errored takes are not fed to the judge
    assert "VERDICT:" in prompt
    assert "CONSENSUS:" in prompt
    assert "DISAGREEMENTS:" in prompt


# -- summarize_new_filings: delegates to the shared lens engine ----------------


def test_summarize_new_filings_delegates_to_lens_core(case, new_entries):
    expected = LensResult(note="mocked, no live model call")
    with patch("docketwatch.ai._run_lens_core", return_value=expected) as core:
        result = summarize_new_filings(case, new_entries, models=["model-a", "model-b"])

    assert result is expected
    args = core.call_args.args
    assert args[0] == ["model-a", "model-b"]
    assert case.case_name in args[1]  # the lens prompt
    assert args[3] == DIGEST_CHAIN_TAG


def test_summarize_new_filings_empty_entries_raises_without_touching_the_chain(case):
    with patch("docketwatch.ai._run_lens_core") as core:
        with pytest.raises(SummaryError, match="at least one new entry"):
            summarize_new_filings(case, [])
    core.assert_not_called()


def test_summarize_new_filings_unknown_chain_raises(case, new_entries):
    with pytest.raises(SummaryError, match="chain 'not-a-real-chain' not found"):
        summarize_new_filings(case, new_entries, chain="not-a-real-chain")


def test_summarize_new_filings_malformed_chains_file_raises(case, new_entries, tmp_path, monkeypatch):
    bad = tmp_path / "chains.yaml"
    bad.write_text("chains:\n  digest:\n    - samples: 3\n")  # missing required 'model' key
    monkeypatch.setattr("docketwatch.ai.CHAINS_PATH", bad)
    with pytest.raises(SummaryError, match="chains.yaml is invalid"):
        summarize_new_filings(case, new_entries)
