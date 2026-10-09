"""Offline crash/resume and qualification contracts for the opt-in ledger.

Every ledger, evidence file, and child-process input lives below ``tmp_path``.
The fake clock measures local stage work independently from queue wait time.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from daily_agent import batch_execution
from daily_agent.batch_execution import (
    BudgetExhausted,
    Execution,
    ExecutionConflict,
    WriterCircuitOpen,
    candidate,
    digest,
    existing_review_validator,
)
from daily_agent.models import EditorialDraft, EditorialReview, MaterialRecord
from daily_agent.parent_writer import PendingResponse
from daily_agent.workflow_state import StateCorrupt, WorkflowBusy


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def frozen_material(key="arxiv:2609.00001", kind="paper"):
    value = {"key": key, "item_type": kind, "title": "Frozen evidence", "version": "v1"}
    return {
        "key": key, "version": "v1", "aliases": {"doi": "10.1000/example"},
        "kind": kind, "input_sha256": digest(value), "input": value,
        "chunks": ["c1", "c2", "c3"] if kind == "paper" else [],
        "pages": [1, 2] if kind == "paper" else [],
        "required_pages": [2] if kind == "paper" else [],
    }


@pytest.fixture
def ledger(tmp_path):
    config = SimpleNamespace(root=tmp_path, sources={
        "reading": {"run_budget_seconds": 30.0, "visual_budget_seconds": 20.0,
                    "fidelity_budget_seconds": 50.0, "max_chunks_per_paper": 2},
        "llm_writer": {"run_budget_seconds": 60.0, "model": "offline-test-model"},
    })
    return SimpleNamespace(
        root=tmp_path / "issue", config=config, clock=FakeClock(),
        candidates=[frozen_material(), frozen_material("github:example/repo", "repo")],
        batch_id="original-issue-batch", protocol="test-protocol-v1",
    )


def execution(ledger, **overrides):
    arguments = {
        "issue_dir": ledger.root, "batch_id": ledger.batch_id,
        "candidates": ledger.candidates, "config": ledger.config,
        "protocol": ledger.protocol, "clock": ledger.clock,
    }
    arguments.update(overrides)
    return Execution(**arguments)


def write_sealed(path, payload):
    path.write_text(json.dumps({"sha256": digest(payload), "payload": payload}), encoding="utf-8")


def result_bytes(ledger):
    return {
        "record": {**deepcopy(ledger.candidates[0]["input"]), "reading": {"chunks": ["full reading"]}},
        "draft": {"key": ledger.candidates[0]["key"], "text": "Full reviewed draft"},
        "review": {"verdict": "PASS", "checks": [{"supported": True}]},
        "science": {"status": "checked", "claims": ["retained science result"]},
        "evidence": {"claims": [{"quote": "Exact source bytes", "page": 2}]},
        "asset_hashes": {"page-2.png": hashlib.sha256(b"fixture pixels").hexdigest()},
    }


def test_three_concurrent_ten_second_stages_charge_one_union(ledger):
    entered = threading.Barrier(4)
    leave = threading.Event()
    with execution(ledger) as run:
        def worker():
            with run.stage("native") as reservation:
                entered.wait(timeout=10)
                assert leave.wait(timeout=10)
                return reservation

        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(worker) for _ in range(3)]
            try:
                entered.wait(timeout=10)
                held = run.snapshot()
                assert held["pools"]["native"]["remaining"] == 0
                assert len(held["reservations"]) == 1
                ledger.clock.advance(10)
            finally:
                leave.set()
            reservations = [future.result(timeout=10) for future in futures]

        assert len({r["token"] for r in reservations}) == 1
        assert {r["deadline"] for r in reservations} == {130.0}
        state = run.snapshot()
        assert state["pools"]["native"] == {
            "limit": 30.0, "remaining": 20.0, "measured": 10.0,
            "charged": 10.0, "forfeited": 0,
        }
        assert not state["reservations"]
        assert len(state["settlements"]) == 1


def test_staggered_overlap_charges_union_but_not_idle_gap(ledger):
    with execution(ledger) as run:
        first = run.stage("native")
        second = run.stage("native")
        first_info = first.__enter__()
        ledger.clock.advance(4)
        second_info = second.__enter__()
        ledger.clock.advance(6)
        first.__exit__(None, None, None)
        assert run.snapshot()["pools"]["native"]["charged"] == 0
        ledger.clock.advance(4)
        second.__exit__(None, None, None)
        assert first_info == second_info
        ledger.clock.advance(1000)  # Queue/parent waiting must be outside the stage.
        with run.stage("native"):
            ledger.clock.advance(2)
        assert run.snapshot()["pools"]["native"]["charged"] == 16
        assert run.snapshot()["pools"]["native"]["remaining"] == 14


def test_simultaneous_pools_account_independently(ledger):
    with execution(ledger) as run:
        with ExitStack() as stack:
            reservations = [stack.enter_context(run.stage(pool)) for pool in batch_execution.POOLS]
            assert len({r["token"] for r in reservations}) == len(batch_execution.POOLS)
            ledger.clock.advance(10)
        state = run.snapshot()
        for pool in batch_execution.POOLS:
            assert state["pools"][pool]["charged"] == 10
            assert state["pools"][pool]["remaining"] == state["pools"][pool]["limit"] - 10


def test_one_hundred_pending_resumes_never_reset_budget_or_admission(ledger):
    assert not issubclass(PendingResponse, Exception)
    slots = set()
    for resume in range(100):
        with pytest.raises(PendingResponse, match="stable-job"):
            with execution(ledger) as run:
                with run.stage("primary_writer"):
                    slots.add(run.admit(
                        ledger.candidates[0]["key"], "primary_writer", "draft", 0,
                        {"prompt": "same frozen prompt"}, queue_job_id="stable-job", queue_role="draft",
                    ))
                    ledger.clock.advance(0.125)
                    raise PendingResponse("stable-job")
        ledger.clock.advance(3600)  # A long human/queue wait contributes zero local work.
        with execution(ledger) as run:
            state = run.snapshot()
            assert state["pools"]["primary_writer"]["charged"] == (resume + 1) * 0.125
            assert state["pools"]["primary_writer"]["remaining"] == 60 - (resume + 1) * 0.125
            assert not state["reservations"]
            assert state["writer_circuit"] is None
            assert len(state["operations"]) == 1
    assert len(slots) == 1


def test_hard_kill_forfeits_entire_saved_reservation_without_refund(ledger):
    script = """
import json, os, sys
from types import SimpleNamespace
from daily_agent.batch_execution import Execution
root, batch, protocol, candidates, sources = sys.argv[1:]
config = SimpleNamespace(sources=json.loads(sources))
run = Execution(root, batch, json.loads(candidates), config, protocol=protocol, clock=lambda: 100.0)
run.__enter__()
stage = run.stage('native')
print(stage.__enter__()['token'], flush=True)
os._exit(73)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(batch_execution.__file__).resolve().parents[1])
    child = subprocess.run(
        [sys.executable, "-c", script, str(ledger.root), ledger.batch_id, ledger.protocol,
         json.dumps(ledger.candidates), json.dumps(ledger.config.sources)],
        env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert child.returncode == 73, child.stderr
    token = child.stdout.strip()
    assert token
    path = execution(ledger).path
    interrupted = json.loads(path.read_text())["payload"]
    assert interrupted["reservations"][token] == {"pool": "native", "amount": 30.0}
    assert interrupted["pools"]["native"]["remaining"] == 0
    for _ in range(3):
        with execution(ledger) as run:
            pool = run.snapshot()["pools"]["native"]
            assert pool == {"limit": 30.0, "remaining": 0, "measured": 0,
                            "charged": 0, "forfeited": 30.0}
            assert run.settlement(token) == {
                "pool": "native", "reserved": 30.0, "measured": None,
                "charged": 0, "forfeited": 30.0,
            }
            assert not run.snapshot()["reservations"]
            with pytest.raises(BudgetExhausted):
                with run.stage("native"):
                    pytest.fail("A forfeited pool cannot receive a fresh budget")
            with run.stage("repaired"):
                ledger.clock.advance(1)


def test_repeat_settlement_and_operation_are_idempotent(ledger):
    key = ledger.candidates[0]["key"]
    with execution(ledger) as run:
        with run.stage("native") as info:
            first = run.admit(key, "native", "chunk:c1", 0, {"text": "exact"}, retry_generation=0)
            for _ in range(10):
                assert run.admit(key, "native", "chunk:c1", 0, {"text": "exact"}, retry_generation=0) == first
            ledger.clock.advance(3)
        before = run.snapshot()
        saved_bytes = run.path.read_bytes()
        settled = run.settlement(info["token"])
        for _ in range(10):
            assert run._settle(info["token"], 999) == settled
            assert run.settlement(info["token"]) == settled
        settled["charged"] = 999
        assert run.snapshot() == before
        assert run.path.read_bytes() == saved_bytes
    with execution(ledger) as run:
        with run.stage("native"):
            assert run.admit(key, "native", "chunk:c1", 0, {"text": "exact"}, retry_generation=0) == first
        assert len(run.snapshot()["operations"]) == 1


@pytest.mark.parametrize("change", [
    "order", "candidate", "input", "version", "alias", "topology", "protocol",
    "reading_budget", "visual_budget", "writer_budget", "chunk_limit", "writer_model",
])
def test_frozen_contract_conflicts_preserve_original_journal(ledger, change):
    with execution(ledger) as run:
        with run.stage("native"):
            ledger.clock.advance(2)
        path = run.path
    original = path.read_bytes()
    candidates = deepcopy(ledger.candidates)
    config = deepcopy(ledger.config)
    protocol = ledger.protocol
    if change == "order":
        candidates.reverse()
    elif change == "candidate":
        candidates.append(frozen_material("arxiv:2609.00002"))
    elif change == "input":
        candidates[0]["input"]["title"] = "Changed actual evidence"
        candidates[0]["input_sha256"] = digest(candidates[0]["input"])
    elif change == "version":
        candidates[0]["version"] = "v2"
    elif change == "alias":
        candidates[0]["aliases"]["doi"] = "10.1000/other"
    elif change == "topology":
        candidates[0]["chunks"].reverse()
    elif change == "protocol":
        protocol = "test-protocol-v2"
    elif change == "reading_budget":
        config.sources["reading"]["run_budget_seconds"] += 1
    elif change == "visual_budget":
        config.sources["reading"]["visual_budget_seconds"] += 1
    elif change == "writer_budget":
        config.sources["llm_writer"]["run_budget_seconds"] += 1
    elif change == "chunk_limit":
        config.sources["reading"]["max_chunks_per_paper"] += 1
    elif change == "writer_model":
        config.sources["llm_writer"]["model"] = "different-model"
    with pytest.raises(ExecutionConflict):
        with execution(ledger, candidates=candidates, config=config, protocol=protocol):
            pytest.fail("Resume cannot replace a frozen batch contract")
    assert path.read_bytes() == original
    with execution(ledger) as run:
        assert run.snapshot()["pools"]["native"]["remaining"] == 28


@pytest.mark.parametrize("malformation", [
    "schema", "schema_type", "missing_pool", "missing_field", "negative", "bad_hash",
    "bad_payload_hash", "nan", "infinity", "null", "truncated", "duplicate_member",
    "invalid_operation_hash", "bad_completion_hash",
])
def test_malformed_journal_fails_closed_and_preserves_exact_bytes(ledger, malformation):
    with execution(ledger) as run:
        with run.stage("native"):
            run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0,
                      {"text": "source"}, retry_generation=0)
        path = run.path
        payload = run.snapshot()
    if malformation == "schema":
        payload["schema"] = 999
    elif malformation == "schema_type":
        payload["schema"] = True
    elif malformation == "missing_pool":
        del payload["pools"]["visual"]
    elif malformation == "missing_field":
        del payload["reservations"]
    elif malformation == "negative":
        payload["pools"]["native"]["remaining"] = -1
    elif malformation == "invalid_operation_hash":
        next(iter(payload["operations"].values()))["input_sha256"] = "not-a-hash"
    elif malformation == "bad_completion_hash":
        payload["completed"][ledger.candidates[0]["key"]] = "not-a-hash"
    write_sealed(path, payload)
    if malformation == "bad_hash":
        value = json.loads(path.read_text())
        value["sha256"] = "not-a-hash"
        path.write_text(json.dumps(value))
    elif malformation == "bad_payload_hash":
        value = json.loads(path.read_text())
        value["sha256"] = "0" * 64
        path.write_text(json.dumps(value))
    elif malformation in {"nan", "infinity"}:
        value = json.loads(path.read_text())
        value["payload"]["pools"]["native"]["remaining"] = float("nan" if malformation == "nan" else "inf")
        path.write_text(json.dumps(value, allow_nan=True))
    elif malformation == "null":
        path.write_text("null")
    elif malformation == "truncated":
        path.write_text('{"sha256":')
    elif malformation == "duplicate_member":
        path.write_text('{"payload": {}, "payload": {}, "sha256": "' + "0" * 64 + '"}')
    corrupt_bytes = path.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(ledger):
            pytest.fail("Malformed journal must never be reset")
    assert path.read_bytes() == corrupt_bytes


@pytest.mark.parametrize("bad_budget", [-1, float("nan"), float("inf"), True, "30"])
def test_invalid_configured_budget_creates_no_journal(ledger, bad_budget):
    ledger.config.sources["reading"]["run_budget_seconds"] = bad_budget
    with pytest.raises(ExecutionConflict):
        execution(ledger)
    assert not ledger.root.exists()


@pytest.mark.parametrize("issue_state", sorted(batch_execution.BLOCKED_ISSUE_STATES))
def test_publication_owned_issue_cannot_rerun(ledger, issue_state):
    with pytest.raises(ExecutionConflict):
        execution(ledger, issue_state=issue_state)
    assert not ledger.root.exists()


def test_process_lease_rejects_second_owner_without_mutation(ledger):
    with execution(ledger) as run:
        before = run.path.read_bytes()
        with pytest.raises(WorkflowBusy):
            with execution(ledger):
                pytest.fail("Only one process lease may mutate an issue batch")
        assert run.path.read_bytes() == before


def test_missing_initialized_journal_never_recreates_budget(ledger):
    with execution(ledger) as run:
        with run.stage("native"):
            ledger.clock.advance(5)
        journal = run.path
        marker = run.folder / "initialized.json"
    assert marker.is_file()
    marker_bytes = marker.read_bytes()
    journal.unlink()
    for _ in range(2):
        with pytest.raises(StateCorrupt):
            with execution(ledger):
                pytest.fail("An initialized issue cannot replace its missing budget journal")
        assert not journal.exists()
        assert marker.read_bytes() == marker_bytes


def test_corrupt_initialization_marker_is_preserved(ledger):
    with execution(ledger) as run:
        journal = run.path
        marker = run.folder / "initialized.json"
    before = journal.read_bytes()
    marker.write_bytes(b'{"sha256": "bad", "payload": null}')
    damaged = marker.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(ledger):
            pytest.fail("A corrupt initialization marker cannot be silently replaced")
    assert journal.read_bytes() == before
    assert marker.read_bytes() == damaged


@pytest.mark.parametrize("write_reached_disk", [False, True])
def test_uncertain_settlement_write_requires_reopen_and_uses_durable_truth(ledger, monkeypatch, write_reached_disk):
    real_atomic_json = batch_execution.atomic_json
    with execution(ledger) as run:
        def failed_write(path, value):
            if write_reached_disk:
                real_atomic_json(path, value)
            raise OSError("Simulated uncertain journal write")

        with pytest.raises(OSError, match="uncertain journal write"):
            with run.stage("native"):
                ledger.clock.advance(2)
                monkeypatch.setattr(batch_execution, "atomic_json", failed_write)
        with pytest.raises(ExecutionConflict):
            run.snapshot()
        with pytest.raises(ExecutionConflict):
            with run.stage("visual"):
                pytest.fail("A poisoned execution cannot admit new work")
    monkeypatch.setattr(batch_execution, "atomic_json", real_atomic_json)
    with execution(ledger) as run:
        pool = run.snapshot()["pools"]["native"]
        assert pool["remaining"] == (28 if write_reached_disk else 0)
        assert pool["charged"] == (2 if write_reached_disk else 0)
        assert pool["forfeited"] == (0 if write_reached_disk else 30)
        assert not run.snapshot()["reservations"]


@pytest.mark.parametrize("write_reached_disk", [False, True])
def test_uncertain_admission_write_retains_reservation_and_original_error(ledger, monkeypatch, write_reached_disk):
    real_atomic_json = batch_execution.atomic_json
    run = execution(ledger)
    with run:
        def failed_write(path, value):
            if write_reached_disk:
                real_atomic_json(path, value)
            raise OSError("Original admission write error")

        with pytest.raises(OSError, match="Original admission write error"):
            with run.stage("native"):
                ledger.clock.advance(1)
                monkeypatch.setattr(batch_execution, "atomic_json", failed_write)
                run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0,
                          {"text": "source"}, queue_job_id="job")
        # Stage cleanup must not refund based on poisoned in-memory accounting.
        durable = json.loads(run.path.read_text())["payload"]
        assert durable["pools"]["native"]["remaining"] == 0
        assert len(durable["reservations"]) == 1
        assert not durable["settlements"]
        assert len(durable["operations"]) == int(write_reached_disk)
        with pytest.raises(ExecutionConflict):
            run.snapshot()
    monkeypatch.setattr(batch_execution, "atomic_json", real_atomic_json)
    # The same object may resume only after it has reacquired and read the disk.
    with run:
        assert run.snapshot()["pools"]["native"]["forfeited"] == 30
        assert run.snapshot()["pools"]["native"]["remaining"] == 0
        assert not run.snapshot()["reservations"]


@pytest.mark.parametrize("corruption", ["totals", "semantics", "operation_schema"])
def test_resealed_accounting_corruption_is_rejected(ledger, corruption):
    with execution(ledger) as run:
        with run.stage("native"):
            run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0,
                      {"text": "frozen"}, retry_generation=0)
            ledger.clock.advance(3)
        payload = run.snapshot()
        path = run.path
    if corruption == "totals":
        payload["pools"]["native"].update(charged=1, remaining=29)
    elif corruption == "semantics":
        next(iter(payload["settlements"].values()))["reserved"] = 1
    else:
        next(iter(payload["operations"].values()))["unexpected"] = "field"
    write_sealed(path, payload)
    damaged = path.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(ledger):
            pytest.fail("Recomputing a checksum cannot legitimize invalid accounting")
    assert path.read_bytes() == damaged


def test_elapsed_overrun_is_measured_but_cannot_charge_beyond_pool(ledger):
    with execution(ledger) as run:
        with run.stage("native"):
            ledger.clock.advance(45)
        pool = run.snapshot()["pools"]["native"]
        assert pool["measured"] == 45
        assert pool["charged"] == 30
        assert pool["remaining"] == 0
        assert pool["forfeited"] == 0
        with pytest.raises(BudgetExhausted):
            with run.stage("native"):
                pytest.fail("Overrun cannot restore or create any budget")


@pytest.mark.parametrize("kind", ["backend", "json", "schema"])
def test_writer_circuit_persists_first_actual_failure(ledger, kind):
    with execution(ledger) as run:
        with run.stage("primary_writer"):
            ledger.clock.advance(1)
            run.writer_failure(kind, "actual failure")
            run.writer_failure("backend", "must not overwrite the first reason")
            with pytest.raises(WriterCircuitOpen):
                run.admit(ledger.candidates[0]["key"], "primary_writer", "draft", 0,
                          {"prompt": "draft"}, queue_job_id="job")
        assert run.snapshot()["writer_circuit"] == {"kind": kind, "detail": "actual failure"}
    for _ in range(3):
        with execution(ledger) as run:
            with pytest.raises(WriterCircuitOpen):
                with run.stage("primary_writer"):
                    pytest.fail("Writer failure circuit must survive resume")
            with run.stage("native"):
                ledger.clock.advance(1)
            assert run.snapshot()["writer_circuit"] == {"kind": kind, "detail": "actual failure"}


def test_pending_is_not_a_writer_circuit_failure(ledger):
    with execution(ledger) as run:
        with pytest.raises(ExecutionConflict):
            run.writer_failure("pending", "ordinary queue suspension")
        assert run.snapshot()["writer_circuit"] is None
        with pytest.raises(PendingResponse):
            with run.stage("primary_writer"):
                ledger.clock.advance(2)
                raise PendingResponse("pending-job")
        with run.stage("primary_writer"):
            ledger.clock.advance(1)
        assert run.snapshot()["pools"]["primary_writer"]["charged"] == 3
        assert run.snapshot()["writer_circuit"] is None


def test_finite_operation_slots_and_queue_roles_survive_resume(ledger):
    paper, repo = [c["key"] for c in ledger.candidates]
    expected = []
    for phase in ("native", "repaired"):
        expected += [(paper, phase, "chunk:" + chunk, attempt, "reading")
                     for chunk in ("c1", "c2") for attempt in range(2)]
    expected += [(paper, "visual", "page:2", 0, "review")]
    expected += [(paper, "fidelity", role + ":" + str(page), attempt, role)
                 for page in (1, 2) for role in ("transcribe", "review") for attempt in range(2)]
    writer_counts = {"draft": 1, "rewrite": 1, "semantic": 2, "presentation": 1,
                     "scientific_writer": 1, "scientific_review": 1,
                     "author_research": 1, "author_review": 1}
    expected += [(paper, "primary_writer", step, attempt, "review" if "review" in step else "draft")
                 for step, count in writer_counts.items() for attempt in range(count)]
    expected += [(repo, "primary_writer", "draft", 0, "draft")]
    with execution(ledger) as run:
        with ExitStack() as stack:
            for phase in batch_execution.POOLS:
                stack.enter_context(run.stage(phase))
            for index, (material, phase, substep, ordinal, role) in enumerate(expected):
                run.admit(material, phase, substep, ordinal, {"input": index},
                          queue_job_id="job-" + str(index), queue_role=role)
            forbidden = [
                (paper, "native", "chunk:c1", 2), (paper, "native", "chunk:c3", 0),
                (paper, "visual", "page:1", 0), (paper, "visual", "page:2", 1),
                (paper, "fidelity", "review:2", 2), (paper, "primary_writer", "semantic", 2),
                (paper, "primary_writer", "draft", 1), (repo, "primary_writer", "rewrite", 0),
                ("unknown-material", "primary_writer", "draft", 0),
            ]
            for identity in forbidden:
                with pytest.raises(BudgetExhausted):
                    run.admit(*identity, {"new": "input"}, retry_generation=999)
            assert len(run.snapshot()["operations"]) == len(expected)
    with execution(ledger) as run:
        operations = run.snapshot()["operations"]
        for material, phase, substep, ordinal, role in expected:
            operation = operations[digest([material, phase, substep, ordinal])]
            assert operation["queue_role"] == role
            assert operation["protocol"] == ledger.protocol


@pytest.mark.parametrize("changed", ["input", "job", "generation", "role"])
def test_existing_operation_cannot_be_replaced_with_new_identity_details(ledger, changed):
    with execution(ledger) as run:
        with run.stage("native"):
            kwargs = {"queue_job_id": "job-one", "retry_generation": 0, "queue_role": "reading"}
            prompt = {"source": "frozen"}
            run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0, prompt, **kwargs)
            before = run.path.read_bytes()
            if changed == "input":
                prompt = {"source": "changed"}
            else:
                kwargs[{"job": "queue_job_id", "generation": "retry_generation", "role": "queue_role"}[changed]] = {
                    "job": "job-two", "generation": 1, "role": "draft",
                }[changed]
            with pytest.raises(ExecutionConflict):
                run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0, prompt, **kwargs)
            assert run.path.read_bytes() == before


@pytest.mark.parametrize("identity", [
    {}, {"queue_job_id": ""}, {"queue_job_id": 123},
    {"retry_generation": -1}, {"retry_generation": True}, {"retry_generation": 1.0},
    {"queue_job_id": "job", "retry_generation": -1},
    {"queue_job_id": "job", "retry_generation": True},
    {"queue_job_id": 123, "retry_generation": 0},
    {"queue_job_id": "", "retry_generation": 0},
])
def test_every_provided_operation_generation_field_is_validated(ledger, identity):
    with execution(ledger) as run:
        with run.stage("native"):
            before = run.path.read_bytes()
            with pytest.raises(ExecutionConflict):
                run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0, {}, **identity)
            assert run.path.read_bytes() == before


def test_active_reservation_and_local_deadline_are_required_for_admission(ledger):
    with execution(ledger) as run:
        with pytest.raises(ExecutionConflict):
            run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0, {}, retry_generation=0)
        with run.stage("native"):
            ledger.clock.advance(30)
            with pytest.raises(BudgetExhausted):
                run.admit(ledger.candidates[0]["key"], "native", "chunk:c1", 0, {}, retry_generation=0)
        assert run.snapshot()["pools"]["native"]["remaining"] == 0


def test_orphan_envelope_stays_incomplete_until_validated_commit_and_reload(ledger):
    key = ledger.candidates[0]["key"]
    result = result_bytes(ledger)
    with execution(ledger) as run:
        sha = run.prepare_completion(key, **result)
        assert sha == run.prepare_completion(key, **result)
        assert run.completed(key, validator=lambda _: pytest.fail("Orphans cannot be qualified")) is None
        envelope_path = run.folder / "envelopes" / (sha + ".json")
        original = envelope_path.read_bytes()
        assert key not in run.snapshot()["completed"]
    seen = []
    def verify_full_envelope(envelope):
        seen.append(deepcopy(envelope))
        for name, value in result.items():
            assert envelope[name] == value
        assert envelope["batch_id"] == ledger.batch_id
        assert envelope["protocol"] == ledger.protocol
        assert envelope["input_sha256"] == ledger.candidates[0]["input_sha256"]
        envelope["draft"]["text"] = "Validator cannot mutate durable result bytes"
        return True
    with execution(ledger) as run:
        assert run.completed(key, validator=verify_full_envelope) is None
        run.commit_completion(key, sha, validator=verify_full_envelope)
        run.commit_completion(key, sha, validator=verify_full_envelope)
        assert run.snapshot()["completed"] == {key: sha}
    with execution(ledger) as run:
        loaded = run.completed(key, validator=verify_full_envelope)
        assert loaded["draft"] == result["draft"]
        loaded["draft"]["text"] = "Caller cannot mutate durable result bytes"
        assert run.completed(key, validator=verify_full_envelope)["draft"] == result["draft"]
        with pytest.raises(StateCorrupt):
            run.completed(key, validator=lambda _: False)
        assert envelope_path.read_bytes() == original
    assert len(seen) == 4


@pytest.mark.parametrize("verdict", [False, None, "PASS", 1, {"verdict": "PASS"}])
def test_completion_requires_exact_true_from_validator_not_raw_pass(ledger, verdict):
    key = ledger.candidates[0]["key"]
    with execution(ledger) as run:
        sha = run.prepare_completion(key, **result_bytes(ledger))
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.commit_completion(key, sha, validator=lambda _: verdict)
        assert run.path.read_bytes() == before
        assert not run.snapshot()["completed"]


def test_committed_completion_is_immutable(ledger):
    key = ledger.candidates[0]["key"]
    with execution(ledger) as run:
        result = result_bytes(ledger)
        first = run.prepare_completion(key, **result)
        run.commit_completion(key, first, validator=lambda _: True)
        result["draft"]["text"] = "A different draft"
        second = run.prepare_completion(key, **result)
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.commit_completion(key, second, validator=lambda _: True)
        assert run.path.read_bytes() == before
        assert run.snapshot()["completed"][key] == first


@pytest.mark.parametrize("changed", ["key", "version", "title", "document", "raw_evidence", "bad_asset_hash"])
def test_completion_cannot_substitute_frozen_input_lineage(ledger, changed):
    key = ledger.candidates[0]["key"]
    result = result_bytes(ledger)
    if changed == "key":
        result["record"]["key"] = "arxiv:other"
    elif changed == "version":
        result["record"]["version"] = "v2"
    elif changed == "title":
        result["record"]["title"] = "Different source"
    elif changed == "document":
        result["record"]["paper_document"] = {"chunks": [{"id": "c1", "text": "substituted source"}]}
    elif changed == "raw_evidence":
        result["record"]["raw"] = {"citation_context": "substituted evidence"}
    else:
        result["asset_hashes"] = {"page.png": "not-a-hash"}
    with execution(ledger) as run:
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.prepare_completion(key, **result)
        assert run.path.read_bytes() == before
        assert not list((run.folder / "envelopes").glob("*.json"))


def test_completion_accepts_existing_repaired_document_with_exact_native_lineage(ledger):
    native = {"chunks": [{"id": "c1", "text": "Original source evidence"}], "pages": [{"page": 1}]}
    ledger.candidates[0]["input"]["paper_document"] = deepcopy(native)
    ledger.candidates[0]["input_sha256"] = digest(ledger.candidates[0]["input"])
    result = result_bytes(ledger)
    result["record"]["paper_document"] = {
        "evidence_basis": "image_transcription_reviewed", "native_document": deepcopy(native),
        "chunks": [{"id": "repaired-c1", "text": "Reviewed visual transcription"}],
    }
    result["record"]["raw"] = {"research_context": {"author": "source-backed context"}}
    with execution(ledger) as run:
        assert run.prepare_completion(ledger.candidates[0]["key"], **result)
        result["record"]["paper_document"]["native_document"]["chunks"][0]["text"] = "Changed original"
        with pytest.raises(ExecutionConflict):
            run.prepare_completion(ledger.candidates[0]["key"], **result)


@pytest.mark.parametrize("corruption", ["missing", "tampered", "resealed"])
def test_referenced_completion_corruption_blocks_open_without_journal_reset(ledger, corruption):
    key = ledger.candidates[0]["key"]
    with execution(ledger) as run:
        sha = run.prepare_completion(key, **result_bytes(ledger))
        run.commit_completion(key, sha, validator=lambda _: True)
        journal = run.path
        envelope = run.folder / "envelopes" / (sha + ".json")
    journal_bytes = journal.read_bytes()
    if corruption == "missing":
        envelope.unlink()
        damaged = None
    else:
        value = json.loads(envelope.read_text())
        value["payload"]["evidence"]["claims"][0]["quote"] = "Tampered evidence"
        if corruption == "resealed":
            value["sha256"] = digest(value["payload"])
        envelope.write_text(json.dumps(value))
        damaged = envelope.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(ledger):
            pytest.fail("Missing/corrupt referenced bytes cannot be replayed")
    assert journal.read_bytes() == journal_bytes
    assert (envelope.read_bytes() if envelope.exists() else None) == damaged


def test_candidate_freezes_real_record_aliases_version_and_topology(ledger):
    record = MaterialRecord(
        key="arxiv:2609.00003", source="arxiv", item_type="paper", title="Actual record",
        url="https://arxiv.org/abs/2609.00003v2", source_aliases={"doi": "10.1000/frozen"},
        paper_document={"chunks": [{"id": "first"}, {"id": "second"}],
                        "pages": [{"page": 1}, {"page": 2, "visual_required": True}]},
    )
    frozen = candidate(record)
    original = deepcopy(frozen)
    assert frozen["aliases"] == record.source_aliases
    assert frozen["chunks"] == ["first", "second"]
    assert frozen["pages"] == [1, 2]
    assert frozen["required_pages"] == [2]
    assert frozen["input_sha256"] == digest(record.to_dict())
    record.title = "Later mutation"
    record.source_aliases["doi"] = "10.1000/changed"
    record.paper_document["chunks"][0]["id"] = "changed"
    assert frozen == original
    with execution(ledger, candidates=[frozen]):
        pass


def repo_envelope(ledger):
    from daily_agent.editorial import REPO_FIELDS
    record = MaterialRecord(key="github:fixture/tool", source="github", item_type="repo",
                            title="Fixture tool", url="https://github.com/fixture/tool")
    draft = EditorialDraft(record.key, "repo", record.title,
                           {field: "Concrete capability from the fixture README" for field in REPO_FIELDS})
    review = EditorialReview(record.key, "PASS")
    return {"material": record.key, "record": record.to_dict(), "draft": draft.to_dict(),
            "review": review.to_dict(), "science": {}, "evidence": {}, "asset_hashes": {}}


def test_existing_validator_rechecks_structure_instead_of_accepting_pass(ledger):
    envelope = repo_envelope(ledger)
    validate = existing_review_validator(ledger.config)
    assert validate(envelope) is True
    envelope["draft"]["draft_fields"] = {}
    assert envelope["review"]["verdict"] == "PASS"
    assert validate(envelope) is False
    assert validate({"review": "PASS"}) is False


def test_existing_validator_rejects_unsupported_paper_despite_pass(ledger):
    envelope = repo_envelope(ledger)
    envelope["record"]["item_type"] = "paper"
    envelope["draft"]["item_type"] = "paper"
    envelope["draft"]["verification"] = {"status": "located", "semantic_support": "model_checked"}
    assert existing_review_validator(ledger.config)(envelope) is False


@pytest.mark.parametrize("change", ["tampered", "missing", "wrong_manifest"])
def test_existing_validator_rechecks_asset_bytes_without_repair(ledger, change):
    envelope = repo_envelope(ledger)
    asset = ledger.config.root / "fixture-page.png"
    asset.write_bytes(b"original offline pixels")
    sha = hashlib.sha256(asset.read_bytes()).hexdigest()
    envelope["record"]["paper_document"] = {
        "pages": [{"page": 1, "image_path": str(asset), "image_hash": sha}],
    }
    envelope["asset_hashes"] = {str(asset): sha}
    validate = existing_review_validator(ledger.config)
    assert validate(envelope) is True
    if change == "tampered":
        asset.write_bytes(b"wrong pixels")
    elif change == "missing":
        asset.unlink()
    else:
        envelope["asset_hashes"] = {str(asset): "0" * 64}
    before = asset.read_bytes() if asset.exists() else None
    assert validate(envelope) is False
    assert (asset.read_bytes() if asset.exists() else None) == before
    assert not (ledger.config.root / "data" / "deferred-reviews").exists()
