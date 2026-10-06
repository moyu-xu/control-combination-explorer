from __future__ import annotations

import io

import pandas as pd
from fastapi.testclient import TestClient

from backend.app.main import app


def fixture_bytes() -> bytes:
    frame = pd.DataFrame({"id": [1, 1, 2, 2], "year": [1, 2, 1, 2], "y": [1, 2, 3, 4]})
    stream = io.BytesIO()
    frame.to_stata(stream, write_index=False, version=118)
    return stream.getvalue()


def upload(client: TestClient, name: str, replace: str | None = None) -> dict:
    data = {"replace_dataset_id": replace} if replace else None
    response = client.post(
        "/api/datasets",
        data=data,
        files={"file": (name, fixture_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_cache_lease_protects_current_dataset_from_manual_cleanup() -> None:
    with TestClient(app) as client:
        dataset = upload(client, "current.dta")
        lease = client.put(
            "/api/cache/active",
            json={
                "dataset_id": dataset["id"],
                "analysis_job_ids": [],
                "diagnostic_job_ids": [],
            },
        )
        assert lease.status_code == 200
        preview = client.post("/api/cache/cleanup/preview")
        assert preview.json() == {"resources": 0, "estimated_bytes": 0}
        cleaned = client.post("/api/cache/cleanup")
        assert cleaned.json()["reclaimed_resources"] == 0
        assert client.get(f"/api/datasets/{dataset['id']}").status_code == 200


def test_cleaned_resource_returns_cache_expired_instead_of_not_found() -> None:
    with TestClient(app) as client:
        dataset = upload(client, "history.dta")
        cleaned = client.post("/api/cache/cleanup")
        assert cleaned.json()["reclaimed_resources"] == 1
        expired = client.get(f"/api/datasets/{dataset['id']}")
        assert expired.status_code == 410
        assert expired.json()["detail"]["code"] == "CACHE_EXPIRED"


def test_successful_replacement_retires_previous_dataset() -> None:
    with TestClient(app) as client:
        first = upload(client, "first.dta")
        second = upload(client, "second.dta", replace=first["id"])
        assert client.get(f"/api/datasets/{second['id']}").status_code == 200
        expired = client.get(f"/api/datasets/{first['id']}")
        assert expired.status_code == 410
        status = client.get("/api/cache/status").json()
        assert status["counts"]["datasets"] == 1


def test_failed_replacement_keeps_previous_dataset() -> None:
    with TestClient(app) as client:
        first = upload(client, "first.dta")
        failed = client.post(
            "/api/datasets",
            data={"replace_dataset_id": first["id"]},
            files={"file": ("broken.dta", b"not a stata file", "application/octet-stream")},
        )
        assert failed.status_code == 400
        assert client.get(f"/api/datasets/{first['id']}").status_code == 200

