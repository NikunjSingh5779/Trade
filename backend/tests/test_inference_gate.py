import asyncio

import pytest

from app.inference.gate import InferenceGate


@pytest.mark.asyncio
async def test_gate_never_runs_operations_concurrently(tmp_path):
    gate = InferenceGate(str(tmp_path / "gate.lock"))
    active = 0
    maximum = 0

    async def operation():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    results = await asyncio.gather(*(gate.run(operation) for _ in range(20)))
    assert results == ["ok"] * 20
    assert maximum == 1
