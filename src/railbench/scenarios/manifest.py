"""The Fault Manifest (SPEC §6, §7.4, ADR 0011): every Fault a run injected, with its true
root cause, for the triage evaluation to score against.

The Simulator records each Fault as it injects it (`simulator.faults`); this module adds the
Scenario's names for the Payments and writes it as JSON. The Ledger never reads
it: the `ledger` and `payments` packages don't import this package (an import-boundary test
proves it) and the Ledger's database role can't read the `simulator` schema.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from railbench.scenarios.model import Scenario
from railbench.simulator.model import InjectedFault, PaymentPlan

DEFAULT_DIRECTORY = Path("reports") / "manifests"


class ManifestEntry(InjectedFault):
    """A Fault the Simulator injected, with the Scenario's name for its Payment."""

    payment: str


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario: str
    seed: int
    faults: tuple[ManifestEntry, ...]

    def write(self, path: Path | str | None = None) -> Path:
        """Write the manifest as JSON, by default to reports/manifests/<scenario>-<seed>.json."""
        target = Path(path) if path else DEFAULT_DIRECTORY / f"{self.scenario}-{self.seed}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2) + "\n")
        return target


def manifest_for(
    scenario: Scenario,
    seed: int,
    plans: dict[str, PaymentPlan],
    injected: list[InjectedFault],
) -> Manifest:
    names = {plan.idempotency_key: payment for payment, plan in plans.items()}
    return Manifest(
        scenario=scenario.name,
        seed=seed,
        faults=tuple(
            ManifestEntry(**fault.model_dump(), payment=names[fault.idempotency_key])
            for fault in injected
            if fault.idempotency_key in names
        ),
    )
