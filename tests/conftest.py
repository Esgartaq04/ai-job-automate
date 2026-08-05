from __future__ import annotations

import types
from dataclasses import dataclass, field

import pytest


@dataclass
class FakeFact:
    """Duck-typed stand-in for models.Fact so validator tests need no database."""

    id: str
    text: str
    org: str | None = None
    role: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    type: str = "experience"
    skills: list[str] = field(default_factory=list)


@pytest.fixture
def facts() -> list[FakeFact]:
    return [
        FakeFact(
            id="exp_001",
            org="Morningstar",
            role="Software Engineering Intern",
            start_date="2026-06",
            text="Built an internal pricing service in Python that replaced a nightly batch job.",
            skills=["python", "fastapi", "postgres"],
        ),
        FakeFact(
            id="exp_002",
            org="Morningstar",
            role="Software Engineering Intern",
            start_date="2026-06",
            text="Owned the CI pipeline for 4 repositories, including test parallelization.",
            skills=["ci/cd", "docker"],
        ),
    ]


class StubLLM:
    """Returns queued responses; records the prompts it was given."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_json(self, *, model, system, prompt, schema, max_tokens=8000, cache_system=True):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        data = self.responses.pop(0) if self.responses else {}
        return types.SimpleNamespace(
            data=data, cost_cents=0.01, model=model, input_tokens=100, output_tokens=50
        )


@pytest.fixture
def stub_llm():
    return StubLLM
