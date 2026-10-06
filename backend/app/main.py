from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .analysis import JobManager, validate_analysis
from .cache_manager import CACHE_SWEEP_SECONDS, CacheManager
from .control_classification import classify_payload
from .diagnostic_exports import (
    build_diagnostic_excel,
    build_diagnostic_word,
    figure_bytes,
)
from .diagnostics import DiagnosticError, DiagnosticJobManager
from .exports import build_excel, build_word
from .filter_parser import FilterSyntaxError
from .models import (
    ActiveCacheLease,
    AnalysisSpec,
    DerivedVariableSpec,
    DiagnosticSpec,
    FigureRenderSettings,
    FilterRequest,
    JobStatus,
)
from .session_store import SessionStore

sessions = SessionStore()
jobs = JobManager(sessions)
diagnostic_jobs = DiagnosticJobManager(sessions, jobs)
cache = CacheManager(sessions, jobs, diagnostic_jobs)


async def cache_reaper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=CACHE_SWEEP_SECONDS)
        except TimeoutError:
            await asyncio.to_thread(cache.cleanup)


@asynccontextmanager
async def lifespan(_: FastAPI):
    matplotlib_cache = os.environ.get("MPLCONFIGDIR")
    if matplotlib_cache:
        Path(matplotlib_cache).mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    reaper = asyncio.create_task(cache_reaper(stop))
    try:
        yield
    finally:
        stop.set()
        await reaper
        cache.shutdown()


app = FastAPI(title="Control Combination Explorer", version="2026.10.6", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def expected_token() -> str:
    return os.environ.get("LOCAL_APP_TOKEN", "")


def require_token(
    authorization: Annotated[str | None, Header()] = None,
    query_token: Annotated[str | None, Query(alias="token")] = None,
) -> None:
    expected = expected_token()
    if not expected:
        return
    if authorization != f"Bearer {expected}" and query_token != expected:
        raise HTTPException(status_code=401, detail="Invalid local session token")


Auth = Annotated[None, Depends(require_token)]


def missing_resource(kind: str, resource_id: str, message: str) -> HTTPException:
    if cache.is_expired(kind, resource_id):  # type: ignore[arg-type]
        return HTTPException(
            status_code=410,
            detail={
                "code": "CACHE_EXPIRED",
                "message": "This cached result was automatically cleaned up; run it again",
            },
        )
    return HTTPException(status_code=404, detail=message)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/datasets")
async def upload_dataset(
    _: Auth,
    file: Annotated[UploadFile, File()],
    replace_dataset_id: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".dta"):
        raise HTTPException(status_code=400, detail="Please select a .dta file")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The selected file is empty")
    if len(content) > 1_000_000_000:
        raise HTTPException(status_code=413, detail="The file exceeds the 1 GB limit")
    try:
        session = sessions.create(file.filename, content)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Unable to read the Stata file: {exc}"
        ) from exc
    payload = session.payload()
    if replace_dataset_id and replace_dataset_id != session.id:
        cache.retire_dataset(replace_dataset_id)
    return payload


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str, _: Auth) -> dict[str, Any]:
    try:
        return sessions.get(dataset_id).payload()
    except KeyError as exc:
        raise missing_resource("dataset", dataset_id, "Dataset session not found") from exc


@app.get("/api/datasets/{dataset_id}/control-classifications")
def get_control_classifications(dataset_id: str, _: Auth) -> dict[str, Any]:
    try:
        return classify_payload(sessions.get(dataset_id).variable_payload())
    except KeyError as exc:
        raise missing_resource("dataset", dataset_id, "Dataset session not found") from exc


@app.post("/api/datasets/{dataset_id}/derived")
def create_derived(dataset_id: str, spec: DerivedVariableSpec, _: Auth) -> dict[str, Any]:
    try:
        stats = sessions.add_derived(dataset_id, spec)
        return {"stats": stats, "dataset": sessions.get(dataset_id).payload()}
    except KeyError as exc:
        raise missing_resource("dataset", dataset_id, "Dataset session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/datasets/{dataset_id}/derived/{name}")
def remove_derived(dataset_id: str, name: str, _: Auth) -> dict[str, Any]:
    try:
        sessions.remove_derived(dataset_id, name)
        return sessions.get(dataset_id).payload()
    except KeyError as exc:
        raise missing_resource("dataset", dataset_id, "Dataset session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def filter_response(dataset_id: str, request: FilterRequest, apply: bool) -> dict[str, Any]:
    try:
        if apply:
            result = sessions.apply_filter(dataset_id, request.expression)
            result["dataset"] = sessions.get(dataset_id).payload()
            return result
        return sessions.validate_filter(dataset_id, request.expression)
    except KeyError as exc:
        raise missing_resource("dataset", dataset_id, "Dataset session not found") from exc
    except FilterSyntaxError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": exc.message, "position": exc.position},
        ) from exc


@app.post("/api/datasets/{dataset_id}/filter/validate")
def validate_filter(dataset_id: str, request: FilterRequest, _: Auth) -> dict[str, Any]:
    return filter_response(dataset_id, request, False)


@app.post("/api/datasets/{dataset_id}/filter/apply")
def apply_filter(dataset_id: str, request: FilterRequest, _: Auth) -> dict[str, Any]:
    return filter_response(dataset_id, request, True)


@app.post("/api/analysis/preview")
def preview_analysis(spec: AnalysisSpec, _: Auth) -> dict[str, Any]:
    try:
        return validate_analysis(sessions.get(spec.dataset_id), spec)
    except KeyError as exc:
        raise missing_resource("dataset", spec.dataset_id, "Dataset session not found") from exc


@app.post("/api/jobs")
def start_job(spec: AnalysisSpec, _: Auth) -> dict[str, Any]:
    try:
        job = jobs.start(spec)
        return job.progress().model_dump(mode="json")
    except KeyError as exc:
        raise missing_resource("dataset", spec.dataset_id, "Dataset session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, _: Auth) -> dict[str, Any]:
    try:
        return jobs.get(job_id).progress().model_dump(mode="json")
    except KeyError as exc:
        raise missing_resource("analysis", job_id, "Job not found") from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _: Auth) -> dict[str, bool]:
    try:
        jobs.cancel(job_id)
        return {"cancel_requested": True}
    except KeyError as exc:
        raise missing_resource("analysis", job_id, "Job not found") from exc


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str, _: Auth) -> dict[str, Any]:
    try:
        job = jobs.get(job_id)
    except KeyError as exc:
        raise missing_resource("analysis", job_id, "Job not found") from exc
    if job.status != JobStatus.completed or not job.result:
        raise HTTPException(status_code=409, detail="The job has not completed")
    return job.result.model_dump(mode="json")


@app.websocket("/api/jobs/{job_id}/ws")
async def job_socket(websocket: WebSocket, job_id: str) -> None:
    token = expected_token()
    if token and websocket.query_params.get("token") != token:
        await websocket.close(code=4401)
        return
    try:
        job = jobs.get(job_id)
    except KeyError:
        await websocket.close(code=4410 if cache.is_expired("analysis", job_id) else 4404)
        return
    await websocket.accept()
    try:
        while True:
            progress = job.progress()
            await websocket.send_json(progress.model_dump(mode="json"))
            if progress.status in {JobStatus.completed, JobStatus.cancelled, JobStatus.failed}:
                return
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        return


def export_response(job_id: str, kind: str) -> Response:
    try:
        job = jobs.get(job_id)
    except KeyError as exc:
        raise missing_resource("analysis", job_id, "Job not found") from exc
    if job.status != JobStatus.completed or not job.result:
        raise HTTPException(status_code=409, detail="The job has not completed")
    if not job.result.best_model:
        raise HTTPException(status_code=409, detail="There is no qualifying model to export")
    if kind == "xlsx":
        content = build_excel(job.result)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = build_word(job.result)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="control-search-{job.id[:8]}.{kind}"'
        },
    )


@app.get("/api/jobs/{job_id}/export.xlsx")
def export_excel(job_id: str, _: Auth) -> Response:
    return export_response(job_id, "xlsx")


@app.get("/api/jobs/{job_id}/export.docx")
def export_word(job_id: str, _: Auth) -> Response:
    return export_response(job_id, "docx")


def diagnostic_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Diagnostic job or source job not found")
    return HTTPException(status_code=400, detail=str(exc))


@app.post("/api/jobs/{analysis_job_id}/diagnostics/preview")
def preview_diagnostics(analysis_job_id: str, spec: DiagnosticSpec, _: Auth) -> dict[str, Any]:
    try:
        if spec.analysis_job_id != analysis_job_id:
            raise DiagnosticError("The analysis job in the path and request must match")
        return diagnostic_jobs.preview(spec)
    except (KeyError, ValueError, DiagnosticError) as exc:
        raise diagnostic_error(exc) from exc


def start_diagnostic(analysis_job_id: str, kind: str, spec: DiagnosticSpec) -> dict[str, Any]:
    try:
        if spec.analysis_job_id != analysis_job_id:
            raise DiagnosticError("The analysis job in the path and request must match")
        job = diagnostic_jobs.start(kind, spec)  # type: ignore[arg-type]
        return job.progress().model_dump(mode="json")
    except (KeyError, ValueError, DiagnosticError) as exc:
        raise diagnostic_error(exc) from exc


@app.post("/api/jobs/{analysis_job_id}/diagnostics/event-study")
def start_event_study(analysis_job_id: str, spec: DiagnosticSpec, _: Auth) -> dict[str, Any]:
    return start_diagnostic(analysis_job_id, "event_study", spec)


@app.post("/api/jobs/{analysis_job_id}/diagnostics/placebo")
def start_placebo(analysis_job_id: str, spec: DiagnosticSpec, _: Auth) -> dict[str, Any]:
    return start_diagnostic(analysis_job_id, "placebo", spec)


@app.get("/api/diagnostic-jobs/{job_id}")
def get_diagnostic_job(job_id: str, _: Auth) -> dict[str, Any]:
    try:
        return diagnostic_jobs.get(job_id).progress().model_dump(mode="json")
    except KeyError as exc:
        raise missing_resource("diagnostic", job_id, "Diagnostic job not found") from exc


@app.post("/api/diagnostic-jobs/{job_id}/cancel")
def cancel_diagnostic_job(job_id: str, _: Auth) -> dict[str, bool]:
    try:
        diagnostic_jobs.cancel(job_id)
        return {"cancel_requested": True}
    except KeyError as exc:
        raise missing_resource("diagnostic", job_id, "Diagnostic job not found") from exc


@app.get("/api/diagnostic-jobs/{job_id}/result")
def get_diagnostic_result(job_id: str, _: Auth) -> dict[str, Any]:
    try:
        job = diagnostic_jobs.get(job_id)
    except KeyError as exc:
        raise missing_resource("diagnostic", job_id, "Diagnostic job not found") from exc
    if job.status != JobStatus.completed or not job.result:
        raise HTTPException(status_code=409, detail="The diagnostic job has not completed")
    return job.result.model_dump(mode="json")


@app.patch("/api/diagnostic-jobs/{job_id}/figure-settings")
def update_diagnostic_figure_settings(
    job_id: str, settings: FigureRenderSettings, _: Auth
) -> dict[str, bool]:
    job = completed_diagnostic(job_id)
    with job.lock:
        job.result.spec.language = settings.language
        job.result.spec.labels = settings.labels
        job.result.spec.show_density = settings.show_density
    return {"updated": True}


@app.websocket("/api/diagnostic-jobs/{job_id}/ws")
async def diagnostic_socket(websocket: WebSocket, job_id: str) -> None:
    token = expected_token()
    if token and websocket.query_params.get("token") != token:
        await websocket.close(code=4401)
        return
    try:
        job = diagnostic_jobs.get(job_id)
    except KeyError:
        await websocket.close(code=4410 if cache.is_expired("diagnostic", job_id) else 4404)
        return
    await websocket.accept()
    try:
        while True:
            progress = job.progress()
            await websocket.send_json(progress.model_dump(mode="json"))
            if progress.status in {JobStatus.completed, JobStatus.cancelled, JobStatus.failed}:
                return
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        return


def completed_diagnostic(job_id: str):
    try:
        job = diagnostic_jobs.get(job_id)
    except KeyError as exc:
        raise missing_resource("diagnostic", job_id, "Diagnostic job not found") from exc
    if job.status != JobStatus.completed or not job.result:
        raise HTTPException(status_code=409, detail="The diagnostic job has not completed")
    return job


@app.get("/api/diagnostic-jobs/{job_id}/figures/{key}.{kind}")
def export_diagnostic_figure(job_id: str, key: str, kind: str, _: Auth) -> Response:
    if kind not in {"png", "pdf"}:
        raise HTTPException(status_code=404, detail="Unsupported figure format")
    job = completed_diagnostic(job_id)
    try:
        content = figure_bytes(job.result, key, kind)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content,
        media_type="image/png" if kind == "png" else "application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{key}-{job.id[:8]}.{kind}"'},
    )


@app.get("/api/diagnostic-jobs/{job_id}/export.{kind}")
def export_diagnostic_report(job_id: str, kind: str, _: Auth) -> Response:
    if kind not in {"xlsx", "docx"}:
        raise HTTPException(status_code=404, detail="Unsupported report format")
    job = completed_diagnostic(job_id)
    if kind == "xlsx":
        content = build_diagnostic_excel(job.result)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = build_diagnostic_word(job.result)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="diagnostics-{job.id[:8]}.{kind}"'},
    )


@app.get("/api/cache/status")
def cache_status(_: Auth) -> dict[str, Any]:
    return cache.status()


@app.put("/api/cache/active")
def renew_cache_lease(lease: ActiveCacheLease, _: Auth) -> dict[str, Any]:
    return cache.lease(
        lease.dataset_id,
        list(dict.fromkeys(lease.analysis_job_ids)),
        list(dict.fromkeys(lease.diagnostic_job_ids)),
    )


@app.post("/api/cache/cleanup/preview")
def preview_cache_cleanup(_: Auth) -> dict[str, Any]:
    return cache.preview_manual_cleanup()


@app.post("/api/cache/cleanup")
def clean_cache(_: Auth) -> dict[str, Any]:
    return cache.cleanup(manual=True)


@app.post("/api/shutdown")
async def shutdown(request: Request, _: Auth) -> dict[str, bool]:
    callback = getattr(request.app.state, "shutdown_callback", None)
    if callback:
        asyncio.get_running_loop().call_later(0.2, callback)
    return {"shutting_down": True}


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
