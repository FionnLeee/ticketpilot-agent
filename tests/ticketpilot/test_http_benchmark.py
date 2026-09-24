import argparse
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_benchmark_keeps_transport_errors_and_non_200_in_report(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts/ticketpilot_http_benchmark.py"
    spec = importlib.util.spec_from_file_location("http_benchmark", path)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    monkeypatch.setenv("TICKETPILOT_BENCHMARK_TOKEN", "test-only")
    statuses = iter([200, 503, None, 200])

    async def handle(request):
        if request.url.path == "/v1/tickets":
            return httpx.Response(200, json={"items": [{"id": "test-ticket"}]})
        code = next(statuses)
        if code is None:
            raise httpx.ReadTimeout("injected", request=request)
        return httpx.Response(code, json={})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        benchmark.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    output = tmp_path / "report.json"
    report = await benchmark.run(
        argparse.Namespace(
            levels=(2,),
            base_url="http://test",
            warmup=0,
            timeout=1,
            requests=4,
            output=output,
        )
    )
    assert not report["passed"]
    phase = json.loads(output.read_text(encoding="utf-8"))["phases"][0]
    assert phase["status_counts"] == {"200": 2, "503": 1, "0": 1}
    assert phase["error_counts"] == {"ReadTimeout": 1}
    assert phase["success_rate"] == 0.5
    assert len(phase["rows"]) == 4
    assert phase["success_p95_ms"] is not None
