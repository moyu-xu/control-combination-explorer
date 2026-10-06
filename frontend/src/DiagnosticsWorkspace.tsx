import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { MessageKey } from "./i18n";
import type {
  AnalysisResult,
  DatasetInfo,
  DiagnosticPreview,
  DiagnosticProgress,
  DiagnosticResult,
  DiagnosticSpec,
  EventStudyMethod,
  Language,
  PlaceboMethod
} from "./types";

type T = (key: MessageKey) => string;
type StaleScope = "both" | "event" | "placebo" | "labels";

export interface DiagnosticController {
  spec: DiagnosticSpec | null;
  preview: DiagnosticPreview | null;
  progress: DiagnosticProgress | null;
  eventResult: DiagnosticResult | null;
  placeboResult: DiagnosticResult | null;
  eventStale: boolean;
  placeboStale: boolean;
  eventExpired: boolean;
  placeboExpired: boolean;
  update: (patch: Partial<DiagnosticSpec>, scope?: StaleScope) => void;
  validate: () => Promise<DiagnosticPreview | null>;
  run: (kind: "event-study" | "placebo") => Promise<void>;
  cancel: () => Promise<void>;
  download: (jobId: string, path: string, filename: string) => Promise<void>;
  expireResources: (jobIds: string[]) => void;
}

function defaultSpec(
  result: AnalysisResult,
  dataset: DatasetInfo,
  rank: number,
  language: Language
): DiagnosticSpec {
  const dependent = dataset.variables.find((item) => item.name === result.spec.dependent);
  const label = dependent?.label || result.spec.dependent;
  return {
    analysis_job_id: result.job_id,
    model_rank: rank,
    language,
    panel_id: "",
    time_variable: "",
    treatment: {
      mode: "cohort",
      variable: "",
      never_treated_mode: "zero",
      never_treated_value: null
    },
    window_start: -5,
    window_end: 5,
    extra_fixed_effects: result.spec.fixed_effects.filter((item) => item !== ""),
    event_methods: ["saturated", "did2s"],
    run_pretrend_test: true,
    placebo_methods: ["random_group"],
    placebo_estimator: "did2s",
    repetitions: 500,
    random_seed: 12345,
    show_density: true,
    labels: {
      title_zh: `${label}的事件研究估计结果`,
      title_en: `Event-study estimates for ${label}`,
      y_axis_zh: label,
      y_axis_en: label,
      unit_zh: "",
      unit_en: ""
    }
  };
}

export function useDiagnosticController(
  result: AnalysisResult | null,
  dataset: DatasetInfo | null,
  modelRank: number | null,
  language: Language,
  onError: (message: string) => void
): DiagnosticController {
  const [spec, setSpec] = useState<DiagnosticSpec | null>(null);
  const [preview, setPreview] = useState<DiagnosticPreview | null>(null);
  const [progress, setProgress] = useState<DiagnosticProgress | null>(null);
  const [eventResult, setEventResult] = useState<DiagnosticResult | null>(null);
  const [placeboResult, setPlaceboResult] = useState<DiagnosticResult | null>(null);
  const [eventStale, setEventStale] = useState(false);
  const [placeboStale, setPlaceboStale] = useState(false);
  const [eventExpired, setEventExpired] = useState(false);
  const [placeboExpired, setPlaceboExpired] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const sourceRef = useRef("");

  useEffect(() => {
    if (!result || !dataset || !modelRank) return;
    const source = `${result.job_id}:${modelRank}`;
    if (sourceRef.current === source) {
      setSpec((current) => current ? { ...current, language } : current);
      return;
    }
    if (sourceRef.current.startsWith(`${result.job_id}:`)) {
      sourceRef.current = source;
      setSpec((current) => current ? { ...current, model_rank: modelRank, language } : current);
      setPreview(null);
      if (eventResult) setEventStale(true);
      if (placeboResult) setPlaceboStale(true);
      return;
    }
    sourceRef.current = source;
    setSpec(defaultSpec(result, dataset, modelRank, language));
    setPreview(null);
    if (eventResult) setEventStale(true);
    if (placeboResult) setPlaceboStale(true);
  }, [result, dataset, modelRank, language]);

  useEffect(() => {
    if (!spec) return;
    const jobIds = [eventResult?.job_id, placeboResult?.job_id].filter(
      (value): value is string => Boolean(value)
    );
    if (!jobIds.length) return;
    const timer = window.setTimeout(() => {
      Promise.all(jobIds.map((jobId) => api.updateDiagnosticFigureSettings(jobId, spec)))
        .catch((reason) => onError((reason as Error).message));
    }, 350);
    return () => window.clearTimeout(timer);
  }, [spec?.language, spec?.labels, spec?.show_density, eventResult?.job_id, placeboResult?.job_id]);

  const update = (patch: Partial<DiagnosticSpec>, scope: StaleScope = "both") => {
    setSpec((current) => current ? { ...current, ...patch } : current);
    setPreview(null);
    if (scope === "both" || scope === "event") setEventStale(Boolean(eventResult));
    if (scope === "both" || scope === "placebo") setPlaceboStale(Boolean(placeboResult));
  };

  const validate = async () => {
    if (!spec) return null;
    try {
      const value = await api.diagnosticPreview(spec.analysis_job_id, spec);
      setPreview(value);
      return value;
    } catch (reason) {
      onError((reason as Error).message);
      setPreview(null);
      return null;
    }
  };

  const watch = (jobId: string) => {
    socketRef.current?.close();
    const socket = api.diagnosticSocket(jobId);
    socketRef.current = socket;
    socket.onmessage = async (event) => {
      const next = JSON.parse(event.data) as DiagnosticProgress;
      setProgress(next);
      if (next.status === "completed") {
        try {
          const completed = await api.diagnosticResult(jobId);
          if (completed.kind === "event_study") {
            setEventResult(completed);
            setEventStale(false);
            setEventExpired(false);
          } else {
            setPlaceboResult(completed);
            setPlaceboStale(false);
            setPlaceboExpired(false);
          }
        } catch (reason) {
          onError((reason as Error).message);
        }
      } else if (next.status === "failed") {
        onError(next.message);
      }
    };
    socket.onerror = () => onError("Diagnostic progress connection was interrupted");
  };

  const run = async (kind: "event-study" | "placebo") => {
    if (!spec) return;
    const issues = localIssues(spec, kind);
    if (issues.length) {
      onError(issues.join("; "));
      return;
    }
    const checked = preview ?? await validate();
    if (!checked?.valid) return;
    const work = kind === "placebo" ? spec.placebo_methods.length * spec.repetitions : spec.event_methods.length;
    if (work > 1000 && !window.confirm(language === "zh" ? "预计回归次数较多，运行可能耗时。是否继续？" : "This run requires many regressions and may take time. Continue?")) return;
    try {
      const started = await api.startDiagnostic(spec.analysis_job_id, kind, spec);
      setProgress(started);
      watch(started.job_id);
    } catch (reason) {
      onError((reason as Error).message);
    }
  };

  const cancel = async () => {
    if (!progress || !["queued", "running"].includes(progress.status)) return;
    await api.cancelDiagnostic(progress.job_id);
  };

  const download = (jobId: string, path: string, filename: string) =>
    api.downloadDiagnostic(jobId, path, filename).catch((reason) => onError((reason as Error).message));

  const expireResources = (jobIds: string[]) => {
    if (eventResult && jobIds.includes(eventResult.job_id)) {
      setEventResult(null);
      setEventStale(false);
      setEventExpired(true);
    }
    if (placeboResult && jobIds.includes(placeboResult.job_id)) {
      setPlaceboResult(null);
      setPlaceboStale(false);
      setPlaceboExpired(true);
    }
    if (progress && jobIds.includes(progress.job_id)) {
      socketRef.current?.close();
      setProgress(null);
    }
  };

  return { spec, preview, progress, eventResult, placeboResult, eventStale, placeboStale, eventExpired, placeboExpired, update, validate, run, cancel, download, expireResources };
}

function localIssues(spec: DiagnosticSpec, kind?: "event-study" | "placebo"): string[] {
  const issues: string[] = [];
  if (!spec.panel_id) issues.push("Panel ID is required");
  if (!spec.time_variable) issues.push("Time variable is required");
  if (!spec.treatment.variable) issues.push("Treatment timing variable is required");
  if (spec.panel_id && spec.panel_id === spec.time_variable) issues.push("Panel ID and time variable must differ");
  if (spec.treatment.never_treated_mode === "custom" && spec.treatment.never_treated_value === null) issues.push("Custom never-treated code is required");
  if (kind === "event-study" && spec.event_methods.length === 0) issues.push("Select an event-study method");
  if (kind === "placebo" && spec.placebo_methods.length === 0) issues.push("Select a placebo method");
  return issues;
}

function toggle<T extends string>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export function DiagnosticsWorkspace({
  dataset,
  result,
  controller,
  language,
  t,
  embedded = false
}: {
  dataset: DatasetInfo;
  result: AnalysisResult;
  controller: DiagnosticController;
  language: Language;
  t: T;
  embedded?: boolean;
}) {
  const { spec } = controller;
  const [tab, setTab] = useState<"event" | "placebo">("event");
  const [eventMethod, setEventMethod] = useState<EventStudyMethod>("saturated");
  const [placeboMethod, setPlaceboMethod] = useState<PlaceboMethod>("random_group");
  const [showData, setShowData] = useState(false);
  const [triptych, setTriptych] = useState(false);
  const [placeboPage, setPlaceboPage] = useState(0);
  const resultsRef = useRef<HTMLDivElement | null>(null);
  const completedJobsRef = useRef({
    event: controller.eventResult?.job_id ?? "",
    placebo: controller.placeboResult?.job_id ?? ""
  });
  const hasCompletedRef = useRef(Boolean(controller.eventResult || controller.placeboResult));
  const selected = result.top_models.find((item) => item.rank === spec?.model_rank) ?? result.best_model;
  const numeric = dataset.variables.filter((item) => item.numeric);
  const running = controller.progress?.status === "queued" || controller.progress?.status === "running";
  const issues = useMemo(() => spec ? localIssues(spec) : ["Diagnostic configuration is unavailable"], [spec]);
  useEffect(() => {
    const nextEvent = controller.eventResult?.job_id ?? "";
    const nextPlacebo = controller.placeboResult?.job_id ?? "";
    const completedKind = nextEvent && nextEvent !== completedJobsRef.current.event
      ? "event"
      : nextPlacebo && nextPlacebo !== completedJobsRef.current.placebo
        ? "placebo"
        : null;
    completedJobsRef.current = { event: nextEvent, placebo: nextPlacebo };
    if (!completedKind) return;
    setTab(completedKind);
    if (!hasCompletedRef.current) {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      hasCompletedRef.current = true;
    }
  }, [controller.eventResult?.job_id, controller.placeboResult?.job_id]);
  if (!spec || !selected) return <div className="notice warning">{t("diagnosticBlocked")}</div>;
  const event = controller.eventResult?.event_studies.find((item) => item.method === eventMethod) ?? controller.eventResult?.event_studies[0];
  const placebo = controller.placeboResult?.placebos.find((item) => item.method === placeboMethod) ?? controller.placeboResult?.placebos[0];
  const currentResult = tab === "event" ? controller.eventResult : controller.placeboResult;
  const stale = tab === "event" ? controller.eventStale : controller.placeboStale;
  const figureKey = tab === "event"
    ? event ? `event_${event.method}` : ""
    : triptych ? "placebo_all" : placebo ? `placebo_${placebo.method}` : "";
  const updateTreatment = (patch: Partial<DiagnosticSpec["treatment"]>) => controller.update({ treatment: { ...spec.treatment, ...patch } });
  const downloadTabFigures = () => {
    if (!currentResult || stale) return;
    const keys = tab === "event"
      ? currentResult.event_studies.map((item) => `event_${item.method}`)
      : currentResult.placebos.map((item) => `placebo_${item.method}`);
    keys.forEach((key) => void controller.download(currentResult.job_id, `figures/${key}.png`, `${key}.png`));
  };
  return <section className={embedded ? "embedded-section diagnostics-workspace" : "page-section diagnostics-workspace"}>
    {!embedded && <div className="page-heading"><span>06</span><div><h1>{t("diagnostics")}</h1><p>{language === "zh" ? "配置分期 DID 检验并生成论文级图表。" : "Configure staggered-DID diagnostics and publication-ready figures."}</p></div></div>}
    <div className="diagnostic-model-strip">
      <div><span>{t("currentDiagnosticModel")}</span><strong>#{selected.rank}</strong></div>
      <div><span>{t("dependent")}</span><strong>{result.spec.dependent}</strong></div>
      <div><span>{t("core")}</span><strong>{result.spec.core}</strong></div>
      <div><span>{t("controls")}</span><strong>{selected.controls.join(", ") || "—"}</strong></div>
      <div><span>{t("clusterVariable")}</span><strong>{result.spec.cluster_variable ?? "—"}</strong></div>
      <div><span>{t("observations")}</span><strong>{selected.observations.toLocaleString()}</strong></div>
      <div><span>{language === "zh" ? "数据与筛选" : "Data and filter"}</span><strong>{dataset.filename} · {dataset.filter_applied ? dataset.filter_expression : (language === "zh" ? "未筛选" : "No filter")}</strong></div>
    </div>

    <fieldset disabled={running} className="configuration-fieldset">
      <div className="card inner-card diagnostic-config-card">
        <div className="section-title"><div><h2>{t("diagnosticSetup")}</h2><p>{language === "zh" ? "基础设置始终可见；统计定义相关项会联动校验。" : "Core settings remain visible and are validated together."}</p></div></div>
        {issues.length > 0 && <div className="notice error"><strong>{t("errors")}</strong><ul>{issues.map((item) => <li key={item}>{item}</li>)}</ul></div>}
        <div className="form-grid two diagnostic-grid">
          <Field label={t("panelId")} error={!spec.panel_id ? (language === "zh" ? "请选择个体 ID。" : "Select a panel ID.") : ""}><Select variables={dataset.variables} value={spec.panel_id} onChange={(value) => controller.update({ panel_id: value })} /></Field>
          <Field label={t("timeVariable")} error={!spec.time_variable ? (language === "zh" ? "请选择时期变量。" : "Select a time variable.") : ""}><Select variables={numeric} value={spec.time_variable} onChange={(value) => controller.update({ time_variable: value })} /></Field>
          <Field label={t("treatmentInput")}><select value={spec.treatment.mode} onChange={(event) => updateTreatment({ mode: event.target.value as "cohort" | "indicator" })}><option value="cohort">{t("cohortVariable")}</option><option value="indicator">{t("indicatorVariable")}</option></select></Field>
          <Field label={spec.treatment.mode === "cohort" ? t("cohortVariable") : t("indicatorVariable")} error={!spec.treatment.variable ? (language === "zh" ? "请选择处理时点变量。" : "Select a treatment variable.") : ""}><Select variables={numeric} value={spec.treatment.variable} onChange={(value) => updateTreatment({ variable: value })} /></Field>
          {spec.treatment.mode === "cohort" ? <Field label={t("neverTreated")}><div className="inline-fields"><select value={spec.treatment.never_treated_mode} onChange={(event) => updateTreatment({ never_treated_mode: event.target.value as "zero" | "missing" | "custom" })}><option value="zero">{t("neverZero")}</option><option value="missing">{t("neverMissing")}</option><option value="custom">{t("neverCustom")}</option></select>{spec.treatment.never_treated_mode === "custom" && <input type="number" value={spec.treatment.never_treated_value ?? ""} onChange={(event) => updateTreatment({ never_treated_value: event.target.value === "" ? null : Number(event.target.value) })} />}</div></Field> : <div className="field-note">{t("monotoneTreatmentNote")}</div>}
          <div className="window-pair"><Field label={t("windowStart")}><select value={spec.window_start} onChange={(event) => controller.update({ window_start: Number(event.target.value) })}>{[-8,-7,-6,-5,-4,-3,-2].map((value) => <option key={value}>{value}</option>)}</select></Field><Field label={t("windowEnd")}><select value={spec.window_end} onChange={(event) => controller.update({ window_end: Number(event.target.value) })}>{[0,1,2,3,4,5,6,7,8].map((value) => <option key={value}>{value}</option>)}</select></Field></div>
        </div>
        <div className="diagnostic-method-grid">
          <ChoiceGroup title={t("eventMethods")} options={[["saturated", t("saturated")], ["did2s", t("did2s")]]} selected={spec.event_methods} onToggle={(value) => controller.update({ event_methods: toggle(spec.event_methods, value as EventStudyMethod) }, "event")} />
          <ChoiceGroup title={t("placeboMethods")} options={[["random_group", t("randomGroup")], ["random_timing", t("randomTiming")], ["permute_outcome", t("permuteOutcome")]]} selected={spec.placebo_methods} onToggle={(value) => controller.update({ placebo_methods: toggle(spec.placebo_methods, value as PlaceboMethod) }, "placebo")} />
        </div>
        {spec.placebo_methods.length > 0 && <div className="form-grid two"><Field label={t("placeboEstimator")}><select value={spec.placebo_estimator} onChange={(event) => controller.update({ placebo_estimator: event.target.value as EventStudyMethod }, "placebo")}><option value="did2s">{t("did2s")}</option><option value="saturated">{t("saturated")}</option></select></Field><Field label={t("repetitions")}><select value={spec.repetitions} onChange={(event) => controller.update({ repetitions: Number(event.target.value) as 100 | 500 | 1000 | 2000 }, "placebo")}>{[100,500,1000,2000].map((value) => <option key={value}>{value}</option>)}</select></Field></div>}
        <details className="advanced-diagnostic-settings"><summary>{t("advancedSettings")}</summary><div className="advanced-diagnostic-body">
          <div className="locked-fe-note">🔒 {t("baseFeLocked")}</div>
          <Field label={t("extraFixedEffects")}><CheckboxPicker variables={dataset.variables.filter((item) => ![spec.panel_id, spec.time_variable, spec.treatment.variable, result.spec.dependent, result.spec.core, ...result.spec.required_controls, ...selected.controls].includes(item.name))} selected={spec.extra_fixed_effects} onToggle={(value) => controller.update({ extra_fixed_effects: toggle(spec.extra_fixed_effects, value) })} /></Field>
          <div className="form-grid two"><label className="check-row"><input type="checkbox" checked={spec.run_pretrend_test} onChange={(event) => controller.update({ run_pretrend_test: event.target.checked }, "event")} />{t("pretrendTest")}</label><Field label={t("randomSeed")}><input type="number" value={spec.random_seed} onChange={(event) => controller.update({ random_seed: Number(event.target.value) }, "placebo")} /></Field></div>
          <h3>{t("figureText")}</h3><div className="form-grid two"><Field label={t("figureTitleZh")}><input value={spec.labels.title_zh} onChange={(event) => controller.update({ labels: { ...spec.labels, title_zh: event.target.value } }, "labels")} /></Field><Field label={t("figureTitleEn")}><input value={spec.labels.title_en} onChange={(event) => controller.update({ labels: { ...spec.labels, title_en: event.target.value } }, "labels")} /></Field><Field label={t("axisZh")}><input value={spec.labels.y_axis_zh} onChange={(event) => controller.update({ labels: { ...spec.labels, y_axis_zh: event.target.value } }, "labels")} /></Field><Field label={t("axisEn")}><input value={spec.labels.y_axis_en} onChange={(event) => controller.update({ labels: { ...spec.labels, y_axis_en: event.target.value } }, "labels")} /></Field><Field label={t("unitZh")}><input value={spec.labels.unit_zh} onChange={(event) => controller.update({ labels: { ...spec.labels, unit_zh: event.target.value } }, "labels")} /></Field><Field label={t("unitEn")}><input value={spec.labels.unit_en} onChange={(event) => controller.update({ labels: { ...spec.labels, unit_en: event.target.value } }, "labels")} /></Field></div>
          <label className="check-row"><input type="checkbox" checked={spec.show_density} onChange={(event) => controller.update({ show_density: event.target.checked }, "labels")} />{t("showDensity")}</label>
        </div></details>
      </div>
    </fieldset>

    <div className="card inner-card diagnostic-actions-card">
      {controller.preview?.warnings.length ? <div className="notice warning"><strong>{t("warnings")}</strong><ul>{controller.preview.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
      {controller.preview?.valid && <div className="notice success">✓ {t("configurationValid")} · {controller.preview.rows.toLocaleString()} {t("observations")} · {controller.preview.clusters} {t("clusters")}</div>}
      <div className="diagnostic-action-row"><button className="secondary-button" disabled={running || issues.length > 0} onClick={controller.validate}>{t("checkConfig")}</button><div><small>{t("estimatedWork")}: {spec.event_methods.length}</small><button className="primary-button" disabled={running || issues.length > 0 || spec.event_methods.length === 0} onClick={() => controller.run("event-study")}>{controller.eventStale ? t("rerun") : t("runEventStudy")}</button>{issues.length > 0 && <small className="button-reason">{language === "zh" ? "请先修正上方阻塞项" : "Resolve the blocking items above"}</small>}</div><div><small>{t("estimatedWork")}: {(spec.placebo_methods.length * spec.repetitions).toLocaleString()}</small><button className="primary-button" disabled={running || issues.length > 0 || spec.placebo_methods.length === 0} onClick={() => controller.run("placebo")}>{controller.placeboStale ? t("rerun") : t("runPlacebo")}</button>{issues.length > 0 && <small className="button-reason">{language === "zh" ? "请先修正上方阻塞项" : "Resolve the blocking items above"}</small>}</div></div>
      {running && controller.progress && <div className="diagnostic-progress"><div className="section-title"><div><span className="status-badge running">{controller.progress.method || controller.progress.stage}</span><h2>{controller.progress.message}</h2></div><strong>{controller.progress.total ? Math.min(100, controller.progress.successful / controller.progress.total * 100).toFixed(1) : "0"}%</strong></div><div className="progress-track"><span style={{ width: `${controller.progress.total ? Math.min(100, controller.progress.successful / controller.progress.total * 100) : 0}%` }} /></div><div className="stat-grid four compact-stats"><Stat label={t("completed")} value={String(controller.progress.completed)} /><Stat label={t("validRuns")} value={`${controller.progress.successful} / ${controller.progress.total}`} /><Stat label={t("failedRuns")} value={String(controller.progress.failed)} /><Stat label={language === "zh" ? "耗时" : "Elapsed"} value={`${controller.progress.elapsed_seconds.toFixed(1)} s`} /></div><button className="secondary-button danger" onClick={controller.cancel}>{t("cancel")}</button></div>}
    </div>

    <div className="diagnostic-results" ref={resultsRef}>
      <div className="diagnostic-tabs" role="tablist"><button role="tab" aria-selected={tab === "event"} className={tab === "event" ? "active" : ""} onClick={() => { setTab("event"); setShowData(false); }}>{t("eventTab")}</button><button role="tab" aria-selected={tab === "placebo"} className={tab === "placebo" ? "active" : ""} onClick={() => { setTab("placebo"); setShowData(false); }}>{t("placeboTab")}</button></div>
      <div className="paper-viewer card" role="tabpanel">
        {stale && <div className="notice warning stale-banner">⚠ {t("staleDiagnostic")}</div>}
        {!currentResult ? <div className="diagnostic-empty"><span>{(tab === "event" ? controller.eventExpired : controller.placeboExpired) ? "♻" : "⌁"}</span><h2>{tab === "event" ? t("eventTab") : t("placeboTab")}</h2><p>{(tab === "event" ? controller.eventExpired : controller.placeboExpired) ? t("cacheExpiredResult") : tab === "event" ? t("waitingEvent") : t("waitingPlacebo")}</p>{(tab === "event" ? controller.eventExpired : controller.placeboExpired) && <button className="secondary-button" onClick={() => document.querySelector(".diagnostic-config-card")?.scrollIntoView({ behavior: "smooth", block: "start" })}>{t("returnToConfig")}</button>}</div> : <>
          <div className="paper-toolbar">{((tab === "event" && currentResult.event_studies.length > 1) || (tab === "placebo" && currentResult.placebos.length > 1)) && <div className="segmented" role="group" aria-label={tab === "event" ? t("eventMethods") : t("placeboMethods")}>{tab === "event" ? currentResult.event_studies.map((item) => <button key={item.method} aria-pressed={event?.method === item.method} className={event?.method === item.method ? "active" : ""} onClick={() => setEventMethod(item.method)}>{item.method === "saturated" ? t("saturated") : t("did2s")}</button>) : currentResult.placebos.map((item) => <button key={item.method} aria-pressed={placebo?.method === item.method && !triptych} className={placebo?.method === item.method && !triptych ? "active" : ""} onClick={() => { setPlaceboMethod(item.method); setTriptych(false); setPlaceboPage(0); }}>{item.method === "random_group" ? t("randomGroup") : item.method === "random_timing" ? t("randomTiming") : t("permuteOutcome")}</button>)}</div>}<div className="paper-actions">{tab === "placebo" && currentResult.placebos.length === 3 && <button aria-pressed={triptych} className={`secondary-button ${triptych ? "active" : ""}`} onClick={() => setTriptych((value) => !value)}>{triptych ? t("singleFigure") : t("triptych")}</button>}<button className="secondary-button" disabled={stale} onClick={() => currentResult && controller.download(currentResult.job_id, `figures/${figureKey}.png`, `${figureKey}.png`)}>{t("downloadPng")}</button><button className="secondary-button" disabled={stale} onClick={() => currentResult && controller.download(currentResult.job_id, `figures/${figureKey}.pdf`, `${figureKey}.pdf`)}>{t("downloadPdf")}</button><details className="export-menu"><summary>{t("exportMenu")}</summary><div><button disabled={stale} onClick={downloadTabFigures}>{language === "zh" ? "导出当前标签页全部图" : "Export all figures in this tab"}</button><button disabled={stale} onClick={() => controller.download(currentResult.job_id, "export.xlsx", "diagnostics.xlsx")}>{t("exportAllExcel")}</button><button disabled={stale} onClick={() => controller.download(currentResult.job_id, "export.docx", "diagnostics.docx")}>{t("exportAllWord")}</button>{tab === "placebo" && currentResult.placebos.length === 3 && <button disabled={stale} onClick={() => controller.download(currentResult.job_id, "figures/placebo_all.png", "placebo_triptych.png")}>{language === "zh" ? "导出安慰剂三联图" : "Export placebo triptych"}</button>}</div></details></div></div>
          <div className="diagnostic-metrics">{tab === "event" && event ? <><Stat label={language === "zh" ? "方法" : "Method"} value={event.method === "saturated" ? t("saturated") : t("did2s")} /><Stat label={t("observations")} value={event.observations.toLocaleString()} /><Stat label={t("clusters")} value={String(event.clusters)} /><Stat label={t("eventWindow")} value={`[${spec.window_start}, ${spec.window_end}]`} /><Stat label={t("baseline")} value="-1" /><Stat label={t("pretrendP")} value={event.pretrend_p_value === null ? "—" : event.pretrend_p_value.toFixed(4)} /></> : placebo ? <><Stat label={t("actualAtt")} value={placebo.actual_att.toFixed(5)} /><Stat label={t("placeboMean")} value={placebo.mean.toFixed(5)} /><Stat label={language === "zh" ? "安慰剂标准差" : "Placebo SD"} value={placebo.std_dev.toFixed(5)} /><Stat label={language === "zh" ? "95% 随机分布区间" : "95% randomization interval"} value={`[${placebo.quantile_low.toFixed(4)}, ${placebo.quantile_high.toFixed(4)}]`} /><Stat label={t("empiricalP")} value={placebo.empirical_p_value.toFixed(4)} /><Stat label={t("validRepetitions")} value={placebo.draws.length.toLocaleString()} /></> : null}</div>
          <div className="figure-canvas">{figureKey && <img key={`${currentResult.job_id}-${figureKey}-${language}-${spec.labels.title_zh}-${spec.labels.title_en}`} src={api.diagnosticAssetUrl(currentResult.job_id, `figures/${figureKey}.png?v=${encodeURIComponent(`${language}-${spec.labels.title_zh}-${spec.labels.title_en}-${spec.show_density}`)}`)} alt={tab === "event" ? t("eventTab") : t("placeboTab")} />}</div>
          <button className="data-toggle" onClick={() => setShowData((value) => !value)}>{showData ? t("hideData") : t("viewData")}</button>
          {showData && <div className="table-wrap diagnostic-data-table">{tab === "event" && event ? <table><thead><tr><th>t</th><th>{t("estimate")}</th><th>{t("stdError")}</th><th>{t("confidence")}</th><th>{language === "zh" ? "有效样本权重" : "Effective-sample weight"}</th></tr></thead><tbody>{event.points.map((point) => <tr key={point.event_time}><td>{point.event_time}</td><td>{point.estimate === null ? (language === "zh" ? "不可识别" : "Not identified") : point.estimate.toFixed(6)}</td><td>{point.std_error === null ? "—" : point.std_error.toFixed(6)}</td><td>{point.conf_low === null ? "—" : `[${point.conf_low.toFixed(6)}, ${point.conf_high?.toFixed(6)}]`}</td><td>{point.weight ?? "—"}</td></tr>)}</tbody></table> : placebo ? <><table><thead><tr><th>{language === "zh" ? "模拟次序" : "Iteration"}</th><th>{t("estimate")}</th></tr></thead><tbody>{placebo.draws.slice(placeboPage * 50, (placeboPage + 1) * 50).map((draw) => <tr key={draw.iteration}><td>{draw.iteration}</td><td>{draw.estimate.toFixed(7)}</td></tr>)}</tbody></table><div className="table-pagination"><button disabled={placeboPage === 0} onClick={() => setPlaceboPage((value) => Math.max(0, value - 1))}>{language === "zh" ? "上一页" : "Previous"}</button><span>{placeboPage + 1} / {Math.max(1, Math.ceil(placebo.draws.length / 50))}</span><button disabled={(placeboPage + 1) * 50 >= placebo.draws.length} onClick={() => setPlaceboPage((value) => value + 1)}>{language === "zh" ? "下一页" : "Next"}</button></div></> : null}</div>}
        </>}
      </div>
    </div>
  </section>;
}

function Field({ label, error = "", children }: { label: string; error?: string; children: React.ReactNode }) {
  return <label className={`labeled ${error ? "field-invalid" : ""}`}><span>{label}</span>{children}{error && <small className="field-error">{error}</small>}</label>;
}

function Select({ variables, value, onChange }: { variables: DatasetInfo["variables"]; value: string; onChange: (value: string) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value)}><option value="">—</option>{variables.map((item) => <option key={item.name} value={item.name}>{item.name}{item.label ? ` — ${item.label}` : ""}</option>)}</select>;
}

function ChoiceGroup<T extends string>({ title, options, selected, onToggle }: { title: string; options: [T, string][]; selected: T[]; onToggle: (value: T) => void }) {
  return <div className="choice-group"><strong>{title}</strong>{options.map(([value, label]) => <label key={value}><input type="checkbox" checked={selected.includes(value)} onChange={() => onToggle(value)} /><span>{label}</span></label>)}</div>;
}

function CheckboxPicker({ variables, selected, onToggle }: { variables: DatasetInfo["variables"]; selected: string[]; onToggle: (value: string) => void }) {
  return <div className="compact-check-picker">{variables.map((item) => <label key={item.name}><input type="checkbox" checked={selected.includes(item.name)} onChange={() => onToggle(item.name)} /><span><code>{item.name}</code>{item.label && <small>{item.label}</small>}</span></label>)}</div>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>;
}
