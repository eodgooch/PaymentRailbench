"""The fault manifest (SPEC §6, §7.4, ADR 0011): every Fault a run injected, with its true
root cause, for the triage evaluation to score against.

The Simulator records each Fault as it injects it (`simulator.faults`); this module adds the
Scenario's names for the Payments and writes the manifest as JSON. The Ledger never reads
it: the `ledger` and `payments` packages don't import this package (an import-boundary test
proves it) and the Ledger's database role can't read the `simulator` schema.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from railbench.scenarios.model import Scenario
from railbench.simulator.model import InjectedFault, PaymentPlan

DEFAULT_DIRECTORY = Path("reports") / "manifests"


class ManifestEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    fault_id: str
    kind: str
    payment: str  # the Scenario's id for the Payment
    idempotency_key: str
    provider_payment_id: str
    event_id: str
    event_type: str
    params: dict[str, Any]
    injected_at: datetime
    cause_code: str
    root_cause: str


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
            ManifestEntry(
                fault_id=fault.fault_id,
                kind=fault.kind,
                payment=names[fault.idempotency_key],
                idempotency_key=fault.idempotency_key,
                provider_payment_id=fault.payment_id,
                event_id=fault.event_id,
                event_type=fault.event_type,
                params=fault.params,
                injected_at=fault.injected_at,
                cause_code=fault.cause_code,
                root_cause=fault.root_cause,
            )
            for fault in injected
            if fault.idempotency_key in names
        ),
    )
