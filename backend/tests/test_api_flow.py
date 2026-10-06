from __future__ import annotations

import io
import time

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from backend.app.main import app, frontend_dist


def dta_bytes() -> bytes:
    rng = np.random.default_rng(42)
    rows = 80
    frame = pd.DataFrame(
        {
            "x": rng.normal(size=rows),
            "c1": rng.normal(size=rows),
            "c2": rng.normal(size=rows),
            "c3": rng.normal(size=rows),
            "c4": rng.normal(size=rows),
            "firm": np.repeat(np.arange(8), 10),
            "year": np.tile(np.arange(2015, 2025), 8),
        }
    )
    frame["y"] = 1.1 * frame["x"] + 0.2 * frame["c1"] + rng.normal(scale=0.5, size=rows)
    stream = io.BytesIO()
    frame.to_stata(stream, write_index=False, version=118)
    return stream.getvalue()


def test_complete_api_workflow() -> None:
    with TestClient(app) as client:
        upload = client.post(
            "/api/datasets",
            files={"file": ("fixture.dta", dta_bytes(), "application/octet-stream")},
        )
        assert upload.status_code == 200, upload.text
        dataset = upload.json()
        dataset_id = dataset["id"]

        classifications = client.get(
            f"/api/datasets/{dataset_id}/control-classifications"
        )
        assert classifications.status_code == 200
        assert classifications.json()["rule_version"]
        assert len(classifications.json()["classifications"]) == dataset["columns"]

        derived = client.post(
            f"/api/datasets/{dataset_id}/derived",
            json={"name": "ratio", "kind": "ratio", "source": "c1", "denominator": "c2"},
        )
        assert derived.status_code == 200, derived.text

        checked = client.post(
            f"/api/datasets/{dataset_id}/filter/validate",
            json={"expression": "year >= 2017 & !missing(x)"},
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["after"] < checked.json()["before"]

        applied = client.post(
            f"/api/datasets/{dataset_id}/filter/apply",
            json={"expression": "year >= 2017 & !missing(x)"},
        )
        assert applied.status_code == 200, applied.text

        spec = {
            "dataset_id": dataset_id,
            "language": "zh",
            "dependent": "y",
            "core": "x",
            "required_controls": ["c1"],
            "firm_candidate_controls": ["c2", "c3", "c4", "firm"],
            "regional_candidate_controls": [],
            "fixed_other_controls": [],
            "firm_control_target": 3,
            "regional_control_target": 0,
            "control_classifications": [
                {
                    "variable": name,
                    "suggested_level": "firm",
                    "suggested_dimension": "unclassified",
                    "level": "firm",
                    "dimension": "unclassified",
                    "confidence": "unrecognized",
                    "reason": "fixture",
                }
                for name in ["c1", "c2", "c3", "c4", "firm"]
            ],
            "classification_rule_version": "fixture",
            "classification_confirmed": True,
            "dimension_conflict_overrides": [],
            "model_type": "ols",
            "fixed_effects": [],
            "standard_error": "robust",
            "cluster_variable": None,
            "expected_sign": "positive",
            "top_n": 20,
            "significance": 0.05,
        }
        preview = client.post("/api/analysis/preview", json=spec)
        assert preview.status_code == 200, preview.text
        preview_payload = preview.json()
        assert preview_payload["total_combinations"] == 6
        assert preview_payload["combination_examples"] == [
            ["c2", "c3"],
            ["c2", "c4"],
            ["c2", "firm"],
            ["c3", "c4"],
            ["c3", "firm"],
            ["c4", "firm"],
        ]
        assert not preview_payload["combination_examples_truncated"]

        started = client.post("/api/jobs", json=spec)
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        state = "running"
        for _ in range(1000):
            status = client.get(f"/api/jobs/{job_id}")
            assert status.status_code == 200
            state = status.json()["status"]
            if state in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert state == "completed", status.text
        assert status.json()["total"] == 6
        assert status.json()["completed"] == 6

        result = client.get(f"/api/jobs/{job_id}/result")
        assert result.status_code == 200, result.text
        assert result.json()["best_model"] is not None
        result_payload = result.json()
        assert len(result_payload["top_models"]) == 6
        p_values = [item["core_p_value"] for item in result_payload["top_models"]]
        assert p_values == sorted(p_values)
        for item in result_payload["top_models"]:
            assert len(item["controls"]) == 2
            assert "c1" in {coefficient["variable"] for coefficient in item["coefficients"]}

        excel = client.get(f"/api/jobs/{job_id}/export.xlsx")
        word = client.get(f"/api/jobs/{job_id}/export.docx")
        assert excel.status_code == 200 and excel.content.startswith(b"PK")
        assert word.status_code == 200 and word.content.startswith(b"PK")

        landing = client.get("/")
        if frontend_dist.exists():
            assert landing.status_code == 200
            assert "控制变量组合筛选器" in landing.text
        else:
            assert landing.status_code == 404


def staggered_dta_bytes() -> bytes:
    rng = np.random.default_rng(7)
    units = 24
    periods = np.arange(2015, 2023)
    frame = pd.DataFrame(
        {
            "firm": np.repeat(np.arange(units), len(periods)),
            "year": np.tile(periods, units),
        }
    )
    frame["g"] = np.select(
        [frame["firm"] < 8, frame["firm"] < 16],
        [2018, 2020],
        default=0,
    )
    frame["treat"] = ((frame["g"] > 0) & (frame["year"] >= frame["g"])).astype(int)
    frame["x"] = rng.normal(size=len(frame))
    frame["y"] = 0.7 * frame["x"] + frame["treat"] + rng.normal(scale=0.2, size=len(frame))
    stream = io.BytesIO()
    frame.to_stata(stream, write_index=False, version=118)
    return stream.getvalue()


def wait_for_job(client: TestClient, path: str) -> dict:
    payload: dict = {}
    for _ in range(500):
        response = client.get(path)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {path}: {payload}")


def test_event_study_api_and_exports() -> None:
    with TestClient(app) as client:
        upload = client.post(
            "/api/datasets",
            files={"file": ("staggered.dta", staggered_dta_bytes(), "application/octet-stream")},
        )
        dataset_id = upload.json()["id"]
        source_spec = {
            "dataset_id": dataset_id,
            "language": "zh",
            "dependent": "y",
            "core": "x",
            "required_controls": [],
            "candidate_controls": [],
            "candidate_count": 0,
            "model_type": "ols",
            "fixed_effects": [],
            "standard_error": "cluster",
            "cluster_variable": "firm",
            "expected_sign": "positive",
            "top_n": 20,
            "significance": 0.05,
        }
        started = client.post("/api/jobs", json=source_spec)
        source_job_id = started.json()["job_id"]
        assert wait_for_job(client, f"/api/jobs/{source_job_id}")["status"] == "completed"
        diagnostic_spec = {
            "analysis_job_id": source_job_id,
            "model_rank": 1,
            "language": "zh",
            "panel_id": "firm",
            "time_variable": "year",
            "treatment": {
                "mode": "cohort",
                "variable": "g",
                "never_treated_mode": "zero",
                "never_treated_value": None,
            },
            "window_start": -3,
            "window_end": 3,
            "extra_fixed_effects": [],
            "event_methods": ["saturated", "did2s"],
            "run_pretrend_test": True,
            "placebo_methods": ["random_group"],
            "placebo_estimator": "did2s",
            "repetitions": 100,
            "random_seed": 12345,
            "show_density": True,
            "labels": {
                "title_zh": "政策动态效应",
                "title_en": "Dynamic policy effects",
                "y_axis_zh": "政策效应",
                "y_axis_en": "Treatment effect",
                "unit_zh": "",
                "unit_en": "",
            },
        }
        preview = client.post(
            f"/api/jobs/{source_job_id}/diagnostics/preview", json=diagnostic_spec
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["treated_units"] == 16
        started_diagnostic = client.post(
            f"/api/jobs/{source_job_id}/diagnostics/event-study", json=diagnostic_spec
        )
        assert started_diagnostic.status_code == 200, started_diagnostic.text
        diagnostic_job_id = started_diagnostic.json()["job_id"]
        status = wait_for_job(client, f"/api/diagnostic-jobs/{diagnostic_job_id}")
        assert status["status"] == "completed", status
        result = client.get(f"/api/diagnostic-jobs/{diagnostic_job_id}/result")
        assert len(result.json()["event_studies"]) == 2
        png = client.get(f"/api/diagnostic-jobs/{diagnostic_job_id}/figures/event_did2s.png")
        pdf = client.get(f"/api/diagnostic-jobs/{diagnostic_job_id}/figures/event_saturated.pdf")
        excel = client.get(f"/api/diagnostic-jobs/{diagnostic_job_id}/export.xlsx")
        word = client.get(f"/api/diagnostic-jobs/{diagnostic_job_id}/export.docx")
        assert png.content.startswith(b"\x89PNG")
        assert pdf.content.startswith(b"%PDF")
        assert excel.content.startswith(b"PK")
        assert word.content.startswith(b"PK")
