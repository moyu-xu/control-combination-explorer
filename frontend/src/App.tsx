import { useEffect, useId, useMemo, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from "react";
import { api } from "./api";
import { DiagnosticsWorkspace, useDiagnosticController } from "./DiagnosticsWorkspace";
import { controlDimensionLabels, translate, type MessageKey } from "./i18n";
import type {
  AnalysisResult,
  AnalysisSpec,
  CacheCleanupPreview,
  CacheStatus,
  ControlLevel,
  DatasetInfo,
  DerivedSpec,
  JobProgress,
  Language,
  Mode,
  ModelResult,
  Preview,
  VariableClassification,
  VariableInfo
} from "./types";

interface FilterSummary {
  valid?: boolean;
  before: number;
  after: number;
  removed: number;
  removed_ratio: number;
}

const steps: MessageKey[] = ["upload", "process", "configure", "review", "results", "diagnostics"];

const blankConfig = (datasetId = "", language: Language = "zh"): AnalysisSpec => ({
  dataset_id: datasetId,
  language,
  dependent: "",
  core: "",
  required_controls: [],
  firm_candidate_controls: [],
  regional_candidate_controls: [],
  fixed_other_controls: [],
  firm_control_target: 0,
  regional_control_target: 0,
  control_classifications: [],
  classification_rule_version: "",
  classification_confirmed: false,
  dimension_conflict_overrides: [],
  model_type: "ols",
  fixed_effects: [],
  standard_error: "robust",
  cluster_variable: null,
  expected_sign: "positive",
  top_n: 20,
  significance: 0.05
});

const blankDerived: DerivedSpec = { name: "", kind: "log", source: "", denominator: "" };

function combinations(n: number, k: number): number {
  if (k < 0 || k > n) return 0;
  let value = 1;
  for (let i = 1; i <= Math.min(k, n - k); i += 1) value = (value * (n - i + 1)) / i;
  return Math.round(value);
}

const firmDimensions = ["size", "age", "ownership", "profitability", "capital_structure", "growth", "liquidity_cashflow", "governance", "innovation", "financing_constraints", "factor_intensity", "risk", "unclassified"];
const regionalDimensions = ["economic_development", "population_urbanization", "industrial_structure", "fiscal_government", "financial_development", "infrastructure", "human_capital", "openness", "environment", "digitalization", "unclassified"];
const otherDimensions = ["unclassified"];

function dimensionsFor(level: ControlLevel): string[] {
  if (level === "firm") return firmDimensions;
  if (level === "regional") return regionalDimensions;
  return otherDimensions;
}

function mergeClassifications(current: VariableClassification[], suggested: VariableClassification[]): VariableClassification[] {
  const existing = new Map(current.map((item) => [item.variable, item]));
  return suggested.map((item) => existing.get(item.variable) ?? item);
}

function classificationMap(config: AnalysisSpec): Map<string, VariableClassification> {
  return new Map(config.control_classifications.map((item) => [item.variable, item]));
}

function controlsAtLevel(config: AnalysisSpec, names: string[], level: ControlLevel): string[] {
  const lookup = classificationMap(config);
  return names.filter((name) => lookup.get(name)?.level === level);
}

function requiredDimensionConflicts(config: AnalysisSpec): Map<string, string[]> {
  const lookup = classificationMap(config);
  const groups = new Map<string, string[]>();
  config.required_controls.forEach((name) => {
    const item = lookup.get(name);
    if (!item || item.dimension === "unclassified") return;
    const key = `${item.level}:${item.dimension}`;
    groups.set(key, [...(groups.get(key) ?? []), name]);
  });
  return new Map([...groups].filter(([, names]) => names.length > 1));
}

function fmt(value: number | null | undefined, digits = 5): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value !== 0 && Math.abs(value) < 0.0001) return value.toExponential(3);
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

export default function App() {
  const [language, setLanguage] = useState<Language>("zh");
  const [mode, setMode] = useState<Mode>("wizard");
  const [step, setStep] = useState(0);
  const [dataset, setDataset] = useState<DatasetInfo | null>(null);
  const [config, setConfig] = useState<AnalysisSpec>(() => blankConfig());
  const [derivedDraft, setDerivedDraft] = useState<DerivedSpec>({ ...blankDerived });
  const [filterExpression, setFilterExpression] = useState("");
  const [filterChecked, setFilterChecked] = useState<FilterSummary | null>(null);
  const [filterPending, setFilterPending] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [resultStale, setResultStale] = useState(false);
  const [resultExpired, setResultExpired] = useState(false);
  const [selectedModel, setSelectedModel] = useState<ModelResult | null>(null);
  const [diagnosticModelRank, setDiagnosticModelRank] = useState<number | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cacheStatus, setCacheStatus] = useState<CacheStatus | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);
  const [cacheError, setCacheError] = useState("");
  const [cleanupPreview, setCleanupPreview] = useState<CacheCleanupPreview | null>(null);
  const [cleanupConfirmOpen, setCleanupConfirmOpen] = useState(false);
  const [cacheNotice, setCacheNotice] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  const cleanupButtonRef = useRef<HTMLButtonElement | null>(null);
  const overBudgetNotifiedRef = useRef(false);
  const diagnosticController = useDiagnosticController(
    result,
    dataset,
    diagnosticModelRank,
    language,
    setError
  );

  const t = (key: MessageKey) => translate(language, key);
  const running = progress?.status === "queued" || progress?.status === "running";
  const localErrors = useMemo(() => validateConfig(config, dataset), [config, dataset]);

  const applyCacheStatus = (next: CacheStatus, notify = true) => {
    setCacheStatus((current) => {
      if (notify && current && next.total_reclaimed_bytes > current.total_reclaimed_bytes) {
        setCacheNotice(
          language === "zh"
            ? `已自动清理 ${bytes(next.total_reclaimed_bytes - current.total_reclaimed_bytes)} 历史缓存。`
            : `Automatically reclaimed ${bytes(next.total_reclaimed_bytes - current.total_reclaimed_bytes)} of history.`
        );
      }
      return next;
    });
    const missing = next.missing_resources ?? [];
    const missingDiagnostics = missing.filter((item) => item.kind === "diagnostic").map((item) => item.id);
    if (missingDiagnostics.length) diagnosticController.expireResources(missingDiagnostics);
    if (dataset && missing.some((item) => item.kind === "dataset" && item.id === dataset.id)) {
      setDataset(null);
      setResult(null);
      setResultExpired(false);
      setProgress(null);
      setDiagnosticModelRank(null);
      setStep(0);
      setCacheNotice(t("cacheExpiredDataset"));
    } else if (result && missing.some((item) => item.kind === "analysis" && item.id === result.job_id)) {
      setResult(null);
      setProgress(null);
      setDiagnosticModelRank(null);
      setResultExpired(true);
      setCacheNotice(t("cacheExpiredResult"));
    }
    if (next.state === "protected_over_budget" && !overBudgetNotifiedRef.current) {
      overBudgetNotifiedRef.current = true;
      setCacheNotice(t("cacheProtectedWarning"));
    } else if (next.state !== "protected_over_budget") {
      overBudgetNotifiedRef.current = false;
    }
  };

  useEffect(() => {
    if (!cacheNotice) return;
    const timer = window.setTimeout(() => setCacheNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [cacheNotice]);

  const activeAnalysisIds = [...new Set([result?.job_id, progress?.job_id].filter((value): value is string => Boolean(value)))];
  const activeDiagnosticIds = [...new Set([
    diagnosticController.eventResult?.job_id,
    diagnosticController.placeboResult?.job_id,
    diagnosticController.progress?.job_id
  ].filter((value): value is string => Boolean(value)))];
  const activeLeaseKey = `${dataset?.id ?? ""}|${activeAnalysisIds.join(",")}|${activeDiagnosticIds.join(",")}`;

  useEffect(() => {
    let active = true;
    const renew = () => {
      api.renewCacheLease({
        dataset_id: dataset?.id ?? null,
        analysis_job_ids: activeAnalysisIds,
        diagnostic_job_ids: activeDiagnosticIds
      }).then((next) => {
        if (active) applyCacheStatus(next);
      }).catch(() => undefined);
    };
    renew();
    const timer = window.setInterval(renew, 30_000);
    const handleVisibility = () => {
      if (document.visibilityState === "visible") renew();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [activeLeaseKey]);

  useEffect(() => {
    if (!helpOpen) return;
    setCacheError("");
    api.cacheStatus().then((next) => applyCacheStatus(next, false)).catch((reason) => setCacheError((reason as Error).message));
  }, [helpOpen]);

  const updateConfig = (patch: Partial<AnalysisSpec>) => {
    if (running) return;
    setConfig((current) => ({ ...current, ...patch }));
    setPreview(null);
    if (result) setResultStale(true);
  };

  useEffect(() => {
    if (!dataset || running || localErrors.length > 0) {
      if (localErrors.length > 0) setPreview(null);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      api.preview({ ...config, language }).then((value) => {
        if (active) setPreview(value);
      }).catch(() => {
        if (active) setPreview(null);
      });
    }, 350);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [config, dataset?.revision, language, localErrors.length, running]);

  const changeLanguage = (next: Language) => {
    setLanguage(next);
    setConfig((current) => ({ ...current, language: next }));
  };

  const acceptFile = async (file: File) => {
    if (dataset && !window.confirm(t("replaceConfirm"))) return;
    setBusy(true);
    setError("");
    try {
      const loaded = await api.upload(file, dataset?.id);
      setDataset(loaded);
      const classified = await api.classifyControls(loaded.id);
      setConfig({
        ...blankConfig(loaded.id, language),
        control_classifications: classified.classifications,
        classification_rule_version: classified.rule_version
      });
      setFilterExpression("");
      setFilterChecked(null);
      setFilterPending(false);
      setDerivedDraft({ ...blankDerived });
      setPreview(null);
      setProgress(null);
      setResult(null);
      setResultExpired(false);
      setDiagnosticModelRank(null);
      setResultStale(false);
      setStep(0);
    } catch (reason) {
      setError(`${t("uploadError")}: ${(reason as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const refreshDataset = (next: DatasetInfo) => {
    setDataset(next);
    setPreview(null);
    if (result) setResultStale(true);
  };

  const createDerived = async () => {
    if (!dataset) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.createDerived(dataset.id, derivedDraft);
      refreshDataset(response.dataset);
      const classified = await api.classifyControls(dataset.id);
      setConfig((current) => ({
        ...current,
        control_classifications: mergeClassifications(current.control_classifications, classified.classifications),
        classification_rule_version: classified.rule_version,
        classification_confirmed: false
      }));
      setDerivedDraft({ ...blankDerived });
    } catch (reason) {
      setError(`${t("requestError")}: ${(reason as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const removeDerived = async (name: string) => {
    if (!dataset || !window.confirm(t("deleteConfirm"))) return;
    setBusy(true);
    try {
      const next = await api.removeDerived(dataset.id, name);
      const clean = (values: string[]) => values.filter((value) => value !== name);
      updateConfig({
        dependent: config.dependent === name ? "" : config.dependent,
        core: config.core === name ? "" : config.core,
        required_controls: clean(config.required_controls),
        firm_candidate_controls: clean(config.firm_candidate_controls),
        regional_candidate_controls: clean(config.regional_candidate_controls),
        fixed_other_controls: clean(config.fixed_other_controls),
        control_classifications: config.control_classifications.filter((item) => item.variable !== name),
        classification_confirmed: false,
        fixed_effects: clean(config.fixed_effects),
        cluster_variable: config.cluster_variable === name ? null : config.cluster_variable
      });
      setDataset(next);
      setFilterExpression("");
      setFilterChecked(null);
      setFilterPending(false);
    } catch (reason) {
      setError(`${t("requestError")}: ${(reason as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const checkFilter = async (apply: boolean) => {
    if (!dataset) return;
    setBusy(true);
    setError("");
    try {
      if (apply) {
        const response = await api.applyFilter(dataset.id, filterExpression);
        setDataset(response.dataset);
        setFilterChecked(response);
        setFilterPending(false);
      } else {
        setFilterChecked(await api.validateFilter(dataset.id, filterExpression));
      }
    } catch (reason) {
      const problem = reason as Error & { position?: number };
      const marker = problem.position !== undefined ? ` (${problem.position + 1})` : "";
      setError(`${problem.message}${marker}`);
      setFilterChecked(null);
    } finally {
      setBusy(false);
    }
  };

  const loadPreview = async (): Promise<Preview | null> => {
    if (localErrors.length) {
      setError(localErrors.join("; "));
      return null;
    }
    setBusy(true);
    setError("");
    try {
      const value = await api.preview({ ...config, language });
      setPreview(value);
      return value;
    } catch (reason) {
      setError((reason as Error).message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const goReview = async () => {
    const value = await loadPreview();
    if (value?.valid) setStep(3);
  };

  const watchJob = (jobId: string) => {
    socketRef.current?.close();
    const socket = api.socket(jobId);
    socketRef.current = socket;
    socket.onmessage = async (event) => {
      const next = JSON.parse(event.data) as JobProgress;
      setProgress(next);
      if (next.status === "completed") {
        try {
          const completed = await api.result(jobId);
          setResult(completed);
          setResultStale(false);
          setSelectedModel(completed.best_model);
          setDiagnosticModelRank(completed.best_model?.rank ?? null);
        } catch (reason) {
          setError((reason as Error).message);
        }
      } else if (next.status === "failed") {
        setError(next.message);
      }
    };
    socket.onerror = () => setError("Progress connection was interrupted");
  };

  const runAnalysis = async () => {
    const check = preview ?? (await loadPreview());
    if (!check?.valid) return;
    if (config.dimension_conflict_overrides.length > 0 && !window.confirm(t("conflictRunConfirm"))) return;
    if (check.resource_level === "high" && !window.confirm(t("highConfirm"))) return;
    setBusy(true);
    setError("");
    try {
      setResultExpired(false);
      const started = await api.start({ ...config, language });
      setProgress(started);
      setStep(4);
      watchJob(started.job_id);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!progress || !window.confirm(t("cancelConfirm"))) return;
    await api.cancel(progress.job_id);
  };

  const exit = async () => {
    if (!window.confirm(t("exitConfirm"))) return;
    await api.shutdown().catch(() => undefined);
    document.body.innerHTML = `<main class="closed-message"><h1>${t("appName")}</h1><p>${language === "zh" ? "程序已退出，可以关闭此页面。" : "The application has exited. You can close this page."}</p></main>`;
  };

  const refreshCacheStatus = async () => {
    setCacheBusy(true);
    setCacheError("");
    try {
      applyCacheStatus(await api.cacheStatus(), false);
    } catch (reason) {
      setCacheError((reason as Error).message);
    } finally {
      setCacheBusy(false);
    }
  };

  const requestCacheCleanup = async () => {
    setCacheBusy(true);
    setCacheError("");
    try {
      const previewValue = await api.previewCacheCleanup();
      setCleanupPreview(previewValue);
      if (previewValue.resources > 0) setCleanupConfirmOpen(true);
      else await refreshCacheStatus();
    } catch (reason) {
      setCacheError((reason as Error).message);
    } finally {
      setCacheBusy(false);
    }
  };

  const closeCleanupConfirmation = () => {
    setCleanupConfirmOpen(false);
    window.setTimeout(() => cleanupButtonRef.current?.focus(), 0);
  };

  const performCacheCleanup = async () => {
    setCacheBusy(true);
    setCacheError("");
    try {
      const cleaned = await api.cleanupCache();
      applyCacheStatus(cleaned.status, false);
      closeCleanupConfirmation();
      if (cleaned.reclaimed_resources > 0) {
        setCacheNotice(
          language === "zh"
            ? `已清理 ${cleaned.reclaimed_resources} 项历史缓存，释放约 ${bytes(cleaned.reclaimed_bytes)}。`
            : `Cleaned ${cleaned.reclaimed_resources} historical items and reclaimed about ${bytes(cleaned.reclaimed_bytes)}.`
        );
      }
    } catch (reason) {
      setCacheError((reason as Error).message);
    } finally {
      setCacheBusy(false);
    }
  };

  const exportResult = async (kind: "xlsx" | "docx") => {
    if (!result) return;
    setBusy(true);
    try {
      await api.download(result.job_id, kind);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const renderWizardPage = () => {
    if (step === 0) return <DataImport dataset={dataset} busy={busy} onFile={acceptFile} t={t} />;
    if (step === 1)
      return (
        <DataProcessing
          dataset={dataset!}
          draft={derivedDraft}
          setDraft={setDerivedDraft}
          filterExpression={filterExpression}
          setFilterExpression={(value) => {
            setFilterExpression(value);
            setFilterPending(value.trim() !== (dataset?.filter_expression ?? "").trim());
          }}
          filterChecked={filterChecked}
          filterPending={filterPending}
          busy={busy}
          createDerived={createDerived}
          removeDerived={removeDerived}
          checkFilter={checkFilter}
          t={t}
        />
      );
    if (step === 2)
      return <ModelConfiguration dataset={dataset!} config={config} preview={preview} update={updateConfig} locked={running} t={t} />;
    if (step === 3)
      return <Review dataset={dataset!} config={config} preview={preview} errors={localErrors} run={runAnalysis} busy={busy} t={t} />;
    if (step === 4 && resultExpired && !result) return <ExpiredResultState t={t} onReturn={() => setStep(2)} />;
    if (step === 4) return (
      <RunResults
        progress={progress}
        result={result}
        stale={resultStale}
        cancel={cancel}
        selectModel={(model) => {
          setSelectedModel(model);
          setDrawerOpen(true);
        }}
        exportResult={exportResult}
        diagnosticModelRank={diagnosticModelRank}
        setDiagnosticModelRank={setDiagnosticModelRank}
        t={t}
      />
    );
    if (resultExpired && !result) return <ExpiredResultState t={t} onReturn={() => setStep(2)} />;
    return diagnosticReady && result && diagnosticModelRank ? (
      <DiagnosticsWorkspace
        dataset={dataset!}
        result={result}
        controller={diagnosticController}
        language={language}
        t={t}
      />
    ) : <div className="notice warning">{t("diagnosticBlocked")}</div>;
  };

  const diagnosticReady = Boolean(
    result?.best_model && diagnosticModelRank && !resultStale && result.spec.standard_error === "cluster" && result.spec.cluster_variable
  );

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark">β</div>
          <div>
            <strong>{t("appName")}</strong>
            <span>{t("offline")}</span>
          </div>
        </div>
        <div className="file-chip" title={dataset?.filename}>{dataset?.filename ?? t("noFile")}</div>
        <nav className="top-actions">
          <div className="segmented" aria-label="View mode">
            <button className={mode === "wizard" ? "active" : ""} onClick={() => setMode("wizard")}>{t("wizard")}</button>
            <button className={mode === "advanced" ? "active" : ""} onClick={() => setMode("advanced")} disabled={!dataset}>{t("advanced")}</button>
          </div>
          <div className="segmented compact" aria-label="Language">
            <button className={language === "zh" ? "active" : ""} onClick={() => changeLanguage("zh")}>中</button>
            <button className={language === "en" ? "active" : ""} onClick={() => changeLanguage("en")}>EN</button>
          </div>
          <button className="ghost-button" onClick={() => setHelpOpen(true)}>{t("help")}</button>
          <button className="ghost-button danger-text" onClick={exit}>{t("exit")}</button>
        </nav>
      </header>

      {error && (
        <div className="global-error" role="alert">
          <span>{error}</span><button onClick={() => setError("")}>×</button>
        </div>
      )}
      {cacheNotice && <div className="cache-toast" role="status"><span>♻</span>{cacheNotice}</div>}

      {mode === "wizard" ? (
        <div className="workspace wizard-layout">
          <aside className="step-sidebar">
            <ol>
              {steps.map((item, index) => {
                const complete = stepComplete(index, dataset, config, preview, progress);
                const locked = (index > 0 && !dataset) || (index === 5 && !diagnosticReady);
                return (
                  <li key={item} className={`${index === step ? "current" : ""} ${complete ? "complete" : ""}`}>
                    <button disabled={locked || (running && index < 4)} onClick={() => setStep(index)}>
                      <span className="step-number">{complete ? "✓" : index + 1}</span>
                      <span>{t(item)}</span>
                    </button>
                  </li>
                );
              })}
            </ol>
            <div className="privacy-note"><span>●</span>{language === "zh" ? "仅监听本机地址" : "Localhost only"}</div>
          </aside>
          <main className="main-panel">
            {renderWizardPage()}
            <div className="wizard-footer">
              <button className="secondary-button" disabled={step === 0 || running} onClick={() => setStep((value) => Math.max(0, value - 1))}>{t("back")}</button>
              {step < 2 && <button className="primary-button" disabled={!dataset} onClick={() => setStep((value) => value + 1)}>{t("next")}</button>}
              {step === 2 && <button className="primary-button" disabled={localErrors.length > 0 || busy} onClick={goReview}>{t("next")}</button>}
              {step === 3 && <button className="primary-button" disabled={!preview?.valid || busy} onClick={runAnalysis}>{t("run")}</button>}
              {step === 4 && <button className="primary-button" disabled={!diagnosticReady} onClick={() => setStep(5)}>{t("diagnostics")}</button>}
            </div>
          </main>
        </div>
      ) : dataset ? (
        <main className="workspace advanced-layout">
          <div className="advanced-main">
            <details className="card" open><summary>{t("process")}</summary>
              <DataProcessing
                dataset={dataset}
                draft={derivedDraft}
                setDraft={setDerivedDraft}
                filterExpression={filterExpression}
                setFilterExpression={(value) => {
                  setFilterExpression(value);
                  setFilterPending(value.trim() !== dataset.filter_expression.trim());
                }}
                filterChecked={filterChecked}
                filterPending={filterPending}
                busy={busy}
                createDerived={createDerived}
                removeDerived={removeDerived}
                checkFilter={checkFilter}
                t={t}
                embedded
              />
            </details>
            <details className="card" open><summary>{t("configure")}</summary>
              <ModelConfiguration dataset={dataset} config={config} preview={preview} update={updateConfig} locked={running} t={t} embedded />
            </details>
            {(progress || result) && <details className="card" open><summary>{t("results")}</summary>
              <RunResults progress={progress} result={result} stale={resultStale} cancel={cancel} selectModel={(model) => { setSelectedModel(model); setDrawerOpen(true); }} exportResult={exportResult} diagnosticModelRank={diagnosticModelRank} setDiagnosticModelRank={setDiagnosticModelRank} t={t} embedded />
            </details>}
            {resultExpired && !result && <ExpiredResultState t={t} onReturn={() => { setMode("wizard"); setStep(2); }} />}
            {diagnosticReady && result && diagnosticModelRank && <details className="card" open><summary>{t("diagnostics")}</summary>
              <DiagnosticsWorkspace dataset={dataset} result={result} controller={diagnosticController} language={language} t={t} embedded />
            </details>}
            {result && !diagnosticReady && <div className="notice warning">{t("diagnosticBlocked")}</div>}
          </div>
          <aside className="advanced-summary card">
            <h2>{t("summary")}</h2>
            <SummaryRows dataset={dataset} config={config} preview={preview} t={t} />
            {preview && <CombinationPreview preview={preview} t={t} compact />}
            {localErrors.length > 0 && <IssueList title={t("errors")} items={localErrors} tone="error" />}
            <button className="secondary-button full" onClick={loadPreview} disabled={busy || running}>{t("validate")}</button>
            <button className="primary-button full" onClick={runAnalysis} disabled={busy || running || localErrors.length > 0}>{t("run")}</button>
            {running && <p className="muted centered">{t("configLocked")}</p>}
          </aside>
        </main>
      ) : null}

      {helpOpen && <Modal title={t("help")} close={() => setHelpOpen(false)} active={!cleanupConfirmOpen} className="help-modal"><p>{t("helpBody")}</p><p className="help-version">{t("appVersion")} <strong>{__APP_VERSION__}</strong></p><code>year &gt;= 2015 &amp; !missing(size)</code><CacheHelpPanel status={cacheStatus} busy={cacheBusy} error={cacheError} language={language} t={t} cleanupButtonRef={cleanupButtonRef} refresh={refreshCacheStatus} requestCleanup={requestCacheCleanup} /></Modal>}
      {cleanupConfirmOpen && cleanupPreview && <Modal title={t("cacheConfirmTitle")} close={closeCleanupConfirmation}><div className="cache-confirm"><div className="cache-confirm-icon">♻</div><p>{language === "zh" ? `将删除 ${cleanupPreview.resources} 项历史缓存，预计释放 ${bytes(cleanupPreview.estimated_bytes)}。当前数据、当前结果和运行中任务不会受到影响。` : `${cleanupPreview.resources} historical items will be removed, reclaiming about ${bytes(cleanupPreview.estimated_bytes)}. Current data, current results, and running jobs are protected.`}</p><div className="modal-actions"><button className="secondary-button" onClick={closeCleanupConfirmation}>{t("cancel")}</button><button data-autofocus className="primary-button" disabled={cacheBusy} onClick={performCacheCleanup}>{cacheBusy ? t("cacheCleaning") : t("cacheConfirmAction")}</button></div></div></Modal>}
      {drawerOpen && selectedModel && <ModelDrawer model={selectedModel} close={() => setDrawerOpen(false)} t={t} />}
      {busy && <div className="busy-indicator"><span className="spinner" /></div>}
    </div>
  );
}

function validateConfig(config: AnalysisSpec, dataset: DatasetInfo | null): string[] {
  const zh = config.language === "zh";
  if (!dataset) return [zh ? "需要先导入数据" : "Dataset required"];
  const errors: string[] = [];
  if (!config.dependent) errors.push(zh ? "请选择被解释变量" : "Dependent variable is required");
  if (!config.core) errors.push(zh ? "请选择核心解释变量" : "Focal explanatory variable is required");
  if (!config.classification_confirmed) errors.push(zh ? "请确认控制变量层级与经济维度" : "Control classifications must be confirmed");
  const requiredFirm = controlsAtLevel(config, config.required_controls, "firm").length;
  const requiredRegional = controlsAtLevel(config, config.required_controls, "regional").length;
  const firmNeeded = config.firm_control_target - requiredFirm;
  const regionalNeeded = config.regional_control_target - requiredRegional;
  if (firmNeeded < 0 || firmNeeded > config.firm_candidate_controls.length) errors.push(zh ? "企业层面目标数超出可用范围" : "Firm-level target is outside the available range");
  if (regionalNeeded < 0 || regionalNeeded > config.regional_candidate_controls.length) errors.push(zh ? "地区层面目标数超出可用范围" : "Regional-level target is outside the available range");
  const unresolved = [...requiredDimensionConflicts(config).keys()].filter((key) => !config.dimension_conflict_overrides.includes(key));
  if (unresolved.length) errors.push(zh ? `必选控制变量维度冲突尚未处理：${unresolved.join("、")}` : `Required-control dimension conflicts are unresolved: ${unresolved.join(", ")}`);
  if (config.model_type === "fixed_effects" && !config.fixed_effects.length) errors.push(zh ? "固定效应模型至少需要一个固定效应变量" : "At least one fixed effect is required");
  if (config.standard_error === "cluster" && !config.cluster_variable) errors.push(zh ? "聚类标准误需要聚类变量" : "Cluster variable is required");
  return errors;
}

function stepComplete(index: number, dataset: DatasetInfo | null, config: AnalysisSpec, preview: Preview | null, progress: JobProgress | null): boolean {
  if (index === 0) return Boolean(dataset);
  if (index === 1) return Boolean(dataset);
  if (index === 2) return Boolean(dataset) && validateConfig(config, dataset).length === 0;
  if (index === 3) return Boolean(preview?.valid);
  if (index === 4) return progress?.status === "completed";
  return false;
}

type T = (key: MessageKey) => string;

function DataImport({ dataset, busy, onFile, t }: { dataset: DatasetInfo | null; busy: boolean; onFile: (file: File) => void; t: T }) {
  const [dragging, setDragging] = useState(false);
  const [search, setSearch] = useState("");
  const variables = dataset?.variables.filter((item) => `${item.name} ${item.label}`.toLowerCase().includes(search.toLowerCase())) ?? [];
  const handle = (files: FileList | null) => files?.[0] && onFile(files[0]);
  return (
    <section className="page-section">
      <PageHeading index="01" title={t("upload")} subtitle={t("localOnly")} />
      <label
        className={`drop-zone ${dragging ? "dragging" : ""}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => { event.preventDefault(); setDragging(false); handle(event.dataTransfer.files); }}
      >
        <input type="file" accept=".dta" disabled={busy} onChange={(event) => handle(event.target.files)} />
        <span className="upload-icon">⇧</span>
        <strong>{t("dropHint")}</strong>
        <span>{t("chooseFile")}</span>
      </label>
      {dataset && <>
        <div className="stat-grid four">
          <Stat label={t("rows")} value={dataset.rows.toLocaleString()} />
          <Stat label={t("columns")} value={dataset.columns.toLocaleString()} />
          <Stat label={t("size")} value={bytes(dataset.size_bytes)} />
          <Stat label={t("encoding")} value={dataset.encoding || "—"} />
        </div>
        <div className="section-toolbar">
          <div><h2>{t("variables")}</h2><span className="muted">{dataset.filename} · {dataset.file_format}</span></div>
          <input className="search-input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("search")} />
        </div>
        <div className="table-wrap variable-table-wrap"><table><thead><tr><th>{t("variable")}</th><th>{t("label")}</th><th>{t("type")}</th><th>{t("nonMissing")}</th><th>{t("missing")}</th><th>{t("examples")}</th></tr></thead>
          <tbody>{variables.map((item) => <tr key={item.name}><td><code>{item.name}</code>{item.derived && <span className="tag">fx</span>}</td><td>{item.label || "—"}</td><td>{item.dtype}</td><td>{item.non_missing.toLocaleString()}</td><td>{item.missing.toLocaleString()}</td><td className="examples">{item.examples.map(String).join(", ") || "—"}</td></tr>)}</tbody>
        </table></div>
      </>}
    </section>
  );
}

function DataProcessing(props: {
  dataset: DatasetInfo;
  draft: DerivedSpec;
  setDraft: (value: DerivedSpec) => void;
  filterExpression: string;
  setFilterExpression: (value: string) => void;
  filterChecked: FilterSummary | null;
  filterPending: boolean;
  busy: boolean;
  createDerived: () => void;
  removeDerived: (name: string) => void;
  checkFilter: (apply: boolean) => void;
  t: T;
  embedded?: boolean;
}) {
  const { dataset, draft, setDraft, filterExpression, setFilterExpression, filterChecked, filterPending, busy, createDerived, removeDerived, checkFilter, t, embedded } = props;
  const numeric = dataset.variables.filter((item) => item.numeric);
  const draftValid = draft.name && draft.source && (draft.kind === "log" || draft.denominator);
  return <section className={embedded ? "embedded-section" : "page-section"}>
    {!embedded && <PageHeading index="02" title={t("process")} subtitle={t("derivedHelp")} />}
    <div className="card inner-card">
      <div className="section-title"><div><h2>{t("derivedTitle")}</h2><p>{t("derivedHelp")}</p></div></div>
      <div className="derived-editor">
        <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as "log" | "ratio" })}><option value="log">ln(x)</option><option value="ratio">x / y</option></select>
        <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder={t("newName")} />
        <select value={draft.source} onChange={(e) => setDraft({ ...draft, source: e.target.value })}><option value="">{t("source")}</option>{numeric.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select>
        {draft.kind === "ratio" && <select value={draft.denominator ?? ""} onChange={(e) => setDraft({ ...draft, denominator: e.target.value })}><option value="">{t("denominator")}</option>{numeric.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select>}
        <button className="secondary-button" disabled={!draftValid || busy} onClick={createDerived}>{t("create")}</button>
      </div>
      {dataset.derived.length > 0 && <div className="chip-row">{dataset.derived.map((item) => <span className="variable-chip" key={item.name}><code>{item.name}</code><small>{item.kind === "log" ? `ln(${item.source})` : `${item.source}/${item.denominator}`}</small><button onClick={() => removeDerived(item.name)} aria-label={t("remove")}>×</button></span>)}</div>}
    </div>
    <div className="card inner-card">
      <div className="section-title"><div><h2>{t("filterTitle")}</h2><p>{t("filterHelp")}</p></div>{dataset.filter_applied && <span className="status-badge success">✓ {t("applySuccess")}</span>}</div>
      <textarea className="filter-editor" rows={4} value={filterExpression} onChange={(e) => setFilterExpression(e.target.value)} placeholder={t("filterPlaceholder")} spellCheck={false} />
      {filterPending && <p className="inline-warning">● {t("pendingApply")}</p>}
      <div className="button-row"><button className="secondary-button" disabled={busy} onClick={() => checkFilter(false)}>{t("validate")}</button><button className="primary-button" disabled={busy} onClick={() => checkFilter(true)}>{t("apply")}</button></div>
      {filterChecked && <div className="stat-grid three compact-stats"><Stat label={t("before")} value={Number(filterChecked.before).toLocaleString()} /><Stat label={t("after")} value={Number(filterChecked.after).toLocaleString()} /><Stat label={t("removed")} value={Number(filterChecked.removed).toLocaleString()} /></div>}
    </div>
  </section>;
}

function ModelConfiguration({ dataset, config, preview, update, locked, t, embedded }: { dataset: DatasetInfo; config: AnalysisSpec; preview: Preview | null; update: (patch: Partial<AnalysisSpec>) => void; locked: boolean; t: T; embedded?: boolean }) {
  const numeric = dataset.variables.filter((item) => item.numeric);
  const order = new Map(numeric.map((item, index) => [item.name, index]));
  const sortVariables = (values: string[]) => [...new Set(values)].sort((left, right) => (order.get(left) ?? 0) - (order.get(right) ?? 0));
  const lookup = classificationMap(config);
  const used = new Set([config.dependent, config.core, ...config.required_controls, ...config.firm_candidate_controls, ...config.regional_candidate_controls, ...config.fixed_other_controls].filter(Boolean));
  const forRole = (current: string) => numeric.filter((item) => (!used.has(item.name) || item.name === current) && !config.fixed_effects.includes(item.name));
  const rolesReady = Boolean(config.dependent && config.core);
  const requiredFirm = controlsAtLevel(config, config.required_controls, "firm").length;
  const requiredRegional = controlsAtLevel(config, config.required_controls, "regional").length;
  const requiredOther = controlsAtLevel(config, config.required_controls, "other").length;
  const firmNeeded = Math.max(0, config.firm_control_target - requiredFirm);
  const regionalNeeded = Math.max(0, config.regional_control_target - requiredRegional);
  const rawTotal = combinations(config.firm_candidate_controls.length, firmNeeded) * combinations(config.regional_candidate_controls.length, regionalNeeded);
  const totalK = config.firm_control_target + config.regional_control_target + requiredOther + config.fixed_other_controls.length;
  const conflicts = requiredDimensionConflicts(config);
  const [adjustment, setAdjustment] = useState("");

  const toggleList = (key: "required_controls" | "fixed_effects", name: string) => {
    const current = config[key];
    const next = sortVariables(current.includes(name) ? current.filter((item) => item !== name) : [...current, name]);
    if (key === "fixed_effects") {
      update({ fixed_effects: next });
      return;
    }
    const nextFirm = next.filter((item) => lookup.get(item)?.level === "firm").length;
    const nextRegional = next.filter((item) => lookup.get(item)?.level === "regional").length;
    const patch: Partial<AnalysisSpec> = { required_controls: next };
    if (nextFirm > config.firm_control_target) {
      patch.firm_control_target = nextFirm;
      setAdjustment(`${t("autoAdjusted")}: ${t("firmTarget")} = ${nextFirm}`);
    }
    if (nextRegional > config.regional_control_target) {
      patch.regional_control_target = nextRegional;
      setAdjustment(`${t("autoAdjusted")}: ${t("regionalTarget")} = ${nextRegional}`);
    }
    const nextConfig = { ...config, ...patch };
    patch.dimension_conflict_overrides = config.dimension_conflict_overrides.filter((item) => requiredDimensionConflicts(nextConfig).has(item));
    update(patch);
  };

  const updateClassifications = (values: VariableClassification[]) => {
    const nextLookup = new Map(values.map((item) => [item.variable, item]));
    const firmPool = config.firm_candidate_controls.filter((name) => nextLookup.get(name)?.level === "firm");
    const regionalPool = config.regional_candidate_controls.filter((name) => nextLookup.get(name)?.level === "regional");
    const otherFixed = config.fixed_other_controls.filter((name) => nextLookup.get(name)?.level === "other");
    const nextFirmRequired = config.required_controls.filter((name) => nextLookup.get(name)?.level === "firm").length;
    const nextRegionalRequired = config.required_controls.filter((name) => nextLookup.get(name)?.level === "regional").length;
    update({
      control_classifications: values,
      classification_confirmed: false,
      firm_candidate_controls: firmPool,
      regional_candidate_controls: regionalPool,
      fixed_other_controls: otherFixed,
      firm_control_target: Math.max(nextFirmRequired, Math.min(config.firm_control_target, nextFirmRequired + firmPool.length)),
      regional_control_target: Math.max(nextRegionalRequired, Math.min(config.regional_control_target, nextRegionalRequired + regionalPool.length)),
      dimension_conflict_overrides: []
    });
  };

  const updateCandidates = (level: "firm" | "regional", values: string[]) => {
    const next = sortVariables(values);
    const poolKey = level === "firm" ? "firm_candidate_controls" : "regional_candidate_controls";
    const targetKey = level === "firm" ? "firm_control_target" : "regional_control_target";
    const oldPool = config[poolKey];
    const requiredCount = level === "firm" ? requiredFirm : requiredRegional;
    let target = config[targetKey];
    if (oldPool.length === 0 && next.length > 0 && target === requiredCount) {
      target = requiredCount + 1;
      setAdjustment(`${t("autoAdjusted")}: ${level === "firm" ? t("firmTarget") : t("regionalTarget")} = ${target}`);
    }
    if (target - requiredCount > next.length) {
      target = requiredCount + next.length;
      setAdjustment(`${t("autoAdjusted")}: ${level === "firm" ? t("firmTarget") : t("regionalTarget")} = ${target}`);
    }
    update({ [poolKey]: next, [targetKey]: target });
  };

  const setTarget = (level: "firm" | "regional", requested: number) => {
    const requiredCount = level === "firm" ? requiredFirm : requiredRegional;
    const poolSize = level === "firm" ? config.firm_candidate_controls.length : config.regional_candidate_controls.length;
    const value = Math.max(requiredCount, Math.min(requiredCount + poolSize, Number.isFinite(requested) ? requested : requiredCount));
    update(level === "firm" ? { firm_control_target: value } : { regional_control_target: value });
  };

  const classifiedVariables = (level: ControlLevel) => numeric.filter((item) => lookup.get(item.name)?.level === level);
  const candidateDisabled = (otherPool: string[]) => new Set([config.dependent, config.core, ...config.required_controls, ...config.fixed_effects, ...config.fixed_other_controls, ...otherPool].filter(Boolean));
  return <section className={embedded ? "embedded-section" : "page-section"}>
    {!embedded && <PageHeading index="03" title={t("configure")} subtitle={locked ? t("configLocked") : t("rawPNote")} />}
    <fieldset disabled={locked} className="configuration-fieldset">
      <details className="card config-stage" open><summary><span className="stage-number">1</span><span>{t("roles")}</span>{rolesReady && <span className="stage-status">✓</span>}</summary><div className="stage-body"><div className="form-grid two">
        <Labeled label={t("dependent")}><VariableSelect variables={forRole(config.dependent)} value={config.dependent} onChange={(value) => update({ dependent: value })} placeholder={t("selectPlaceholder")} /></Labeled>
        <Labeled label={t("core")}><VariableSelect variables={forRole(config.core)} value={config.core} onChange={(value) => update({ core: value })} placeholder={t("selectPlaceholder")} /></Labeled>
      </div></div></details>

      <details className="card config-stage" open={!config.classification_confirmed}><summary><span className="stage-number">2</span><span>{t("classificationTitle")}</span>{config.classification_confirmed && <span className="stage-status success">✓ {t("classificationConfirmed")}</span>}</summary><div className="stage-body"><fieldset disabled={!rolesReady} className="nested-fieldset">
        <p className="stage-help">{t("classificationHelp")}</p>
        <ClassificationEditor variables={numeric} classifications={config.control_classifications} language={config.language} onChange={updateClassifications} t={t} />
        <div className="button-row"><button type="button" className="primary-button" disabled={!rolesReady || config.control_classifications.length === 0} onClick={() => update({ classification_confirmed: true })}>{t("confirmClassification")}</button></div>
      </fieldset></div></details>

      <details className="card config-stage" open><summary><span className="stage-number">3</span><span>{t("required")}</span><span className="stage-status">{config.required_controls.length}</span></summary><div className="stage-body"><fieldset disabled={!config.classification_confirmed} className="nested-fieldset">
        <MultiPicker variables={numeric} selected={config.required_controls} disabled={new Set([config.dependent, config.core, ...config.firm_candidate_controls, ...config.regional_candidate_controls, ...config.fixed_other_controls, ...config.fixed_effects])} onToggle={(name) => toggleList("required_controls", name)} t={t} annotations={lookup} warningNames={new Set([...conflicts.values()].flat())} language={config.language} />
        {[...conflicts].map(([key, names]) => <div className="dimension-conflict" key={key}><div><strong>{t("requiredConflict")}</strong><p><code>{key}</code> · {names.join(", ")}</p></div>{config.dimension_conflict_overrides.includes(key) ? <span className="status-badge warning">{t("keepAnyway")}</span> : <button type="button" className="secondary-button danger" onClick={() => update({ dimension_conflict_overrides: [...config.dimension_conflict_overrides, key] })}>{t("keepAnyway")}</button>}</div>)}
        {adjustment && <p className="auto-adjustment">↻ {adjustment}</p>}
      </fieldset></div></details>

      <details className="card config-stage candidate-card" open><summary><span className="stage-number">4</span><span>{t("firmCandidates")}</span><span className="stage-status">C({config.firm_candidate_controls.length},{firmNeeded})</span></summary><div className="stage-body"><fieldset disabled={!config.classification_confirmed} className="nested-fieldset">
        <p className="pool-scope-notice">{t("poolScopeNotice")}</p>
        <CandidatePoolPicker variables={classifiedVariables("firm")} selected={config.firm_candidate_controls} disabled={candidateDisabled(config.regional_candidate_controls)} onChange={(values) => updateCandidates("firm", values)} classifications={lookup} language={config.language} t={t} />
        <div className="pool-metrics"><Stat label={t("requiredCount")} value={requiredFirm.toString()} /><Labeled label={t("firmTarget")}><input type="number" min={requiredFirm} max={requiredFirm + config.firm_candidate_controls.length} value={config.firm_control_target} onChange={(event) => setTarget("firm", Number(event.target.value))} /></Labeled><Stat label={t("stillNeeded")} value={firmNeeded.toString()} /><Stat label={t("rawCombinations")} value={combinations(config.firm_candidate_controls.length, firmNeeded).toLocaleString()} /></div>
      </fieldset></div></details>

      <details className="card config-stage candidate-card" open><summary><span className="stage-number">5</span><span>{t("regionalCandidates")}</span><span className="stage-status">C({config.regional_candidate_controls.length},{regionalNeeded})</span></summary><div className="stage-body"><fieldset disabled={!config.classification_confirmed} className="nested-fieldset">
        <p className="pool-scope-notice">{t("poolScopeNotice")}</p>
        <CandidatePoolPicker variables={classifiedVariables("regional")} selected={config.regional_candidate_controls} disabled={candidateDisabled(config.firm_candidate_controls)} onChange={(values) => updateCandidates("regional", values)} classifications={lookup} language={config.language} t={t} />
        <div className="pool-metrics"><Stat label={t("requiredCount")} value={requiredRegional.toString()} /><Labeled label={t("regionalTarget")}><input type="number" min={requiredRegional} max={requiredRegional + config.regional_candidate_controls.length} value={config.regional_control_target} onChange={(event) => setTarget("regional", Number(event.target.value))} /></Labeled><Stat label={t("stillNeeded")} value={regionalNeeded.toString()} /><Stat label={t("rawCombinations")} value={combinations(config.regional_candidate_controls.length, regionalNeeded).toLocaleString()} /></div>
      </fieldset></div></details>

      <details className="card config-stage" open><summary><span className="stage-number">6</span><span>{t("otherFixedControls")}</span><span className="stage-status">{config.fixed_other_controls.length}</span></summary><div className="stage-body"><fieldset disabled={!config.classification_confirmed} className="nested-fieldset"><p className="stage-help">{t("otherFixedHelp")}</p><MultiPicker variables={classifiedVariables("other")} selected={config.fixed_other_controls} disabled={new Set([config.dependent, config.core, ...config.required_controls, ...config.firm_candidate_controls, ...config.regional_candidate_controls, ...config.fixed_effects])} onToggle={(name) => update({ fixed_other_controls: sortVariables(config.fixed_other_controls.includes(name) ? config.fixed_other_controls.filter((item) => item !== name) : [...config.fixed_other_controls, name]) })} t={t} annotations={lookup} language={config.language} /></fieldset></div></details>

      <details className="card config-stage" open><summary><span className="stage-number">7</span><span>{t("combinationPreview")}</span><span className="stage-status">{previewCountLabel(rawTotal, config.language)}</span></summary><div className="stage-body"><div className="combination-summary-grid"><Stat label={t("rawCombinations")} value={rawTotal.toLocaleString()} /><Stat label={t("dimensionExcluded")} value={preview?.dimension_excluded_count.toLocaleString() ?? "—"} /><Stat label={t("feasibleCombinations")} value={preview?.feasible_combination_count.toLocaleString() ?? "—"} /><Stat label={t("totalControls")} value={totalK.toString()} /></div>{preview && <CombinationPreview preview={preview} t={t} />}{preview?.feasible_combination_count === 0 && <div className="notice error">{t("noFeasibleCombination")}</div>}<p className="notice warning compact-notice">{t("badControlNote")}</p></div></details>

      <details className="card config-stage" open><summary><span className="stage-number">8</span><span>{t("model")}</span></summary><div className="stage-body"><fieldset disabled={!config.classification_confirmed} className="nested-fieldset"><div className="form-grid two">
        <Labeled label={t("model")}><select value={config.model_type} onChange={(e) => update({ model_type: e.target.value as AnalysisSpec["model_type"], fixed_effects: e.target.value === "ols" ? [] : config.fixed_effects })}><option value="ols">{t("ols")}</option><option value="fixed_effects">{t("fixedEffects")}</option></select></Labeled>
        <Labeled label={t("type")}><select value={config.standard_error} onChange={(e) => update({ standard_error: e.target.value as AnalysisSpec["standard_error"], cluster_variable: e.target.value === "cluster" ? config.cluster_variable : null })}><option value="iid">{t("iid")}</option><option value="robust">{t("robust")}</option><option value="cluster">{t("cluster")}</option></select></Labeled>
      </div>
      {config.model_type === "fixed_effects" && <><h3>{t("feVariables")}</h3><MultiPicker variables={dataset.variables} selected={config.fixed_effects} disabled={used} onToggle={(name) => toggleList("fixed_effects", name)} t={t} /></>}
      {config.standard_error === "cluster" && <Labeled label={t("clusterVariable")}><VariableSelect variables={dataset.variables.filter((item) => !used.has(item.name) || config.fixed_effects.includes(item.name))} value={config.cluster_variable ?? ""} onChange={(value) => update({ cluster_variable: value || null })} placeholder={t("selectPlaceholder")} /></Labeled>}
      <div className="form-grid two"><Labeled label={t("direction")}><select value={config.expected_sign} onChange={(e) => update({ expected_sign: e.target.value as AnalysisSpec["expected_sign"] })}><option value="positive">{t("positive")}</option><option value="negative">{t("negative")}</option></select></Labeled><Labeled label={t("topN")}><select value={config.top_n} onChange={(e) => update({ top_n: Number(e.target.value) as AnalysisSpec["top_n"] })}>{[5, 10, 20, 50, 100].map((value) => <option value={value} key={value}>{value}</option>)}</select></Labeled></div></fieldset></div></details>
    </fieldset>
  </section>;
}

function previewCountLabel(rawTotal: number, language: Language): string {
  return language === "zh" ? `${rawTotal.toLocaleString()} 个原始组合` : `${rawTotal.toLocaleString()} raw combinations`;
}

function Review({ dataset, config, preview, errors, run, busy, t }: { dataset: DatasetInfo; config: AnalysisSpec; preview: Preview | null; errors: string[]; run: () => void; busy: boolean; t: T }) {
  return <section className="page-section"><PageHeading index="04" title={t("review")} subtitle={t("rawPNote")} />
    <div className="review-grid"><div><div className="card inner-card"><h2>{t("summary")}</h2><SummaryRows dataset={dataset} config={config} preview={preview} t={t} /></div>{preview && <CombinationPreview preview={preview} t={t} />}</div>
    <div>{errors.length > 0 && <IssueList title={t("errors")} items={errors} tone="error" />}{preview?.warnings.length ? <IssueList title={t("warnings")} items={preview.warnings} tone="warning" /> : null}<button className="primary-button large full" disabled={busy || errors.length > 0 || !preview?.valid} onClick={run}>{t("run")} · {preview?.total_combinations.toLocaleString() ?? "—"}</button></div></div>
  </section>;
}

function RunResults({ progress, result, stale, cancel, selectModel, exportResult, diagnosticModelRank, setDiagnosticModelRank, t, embedded }: { progress: JobProgress | null; result: AnalysisResult | null; stale: boolean; cancel: () => void; selectModel: (model: ModelResult) => void; exportResult: (kind: "xlsx" | "docx") => void; diagnosticModelRank: number | null; setDiagnosticModelRank: (rank: number) => void; t: T; embedded?: boolean }) {
  const percent = progress?.total ? Math.min(100, (progress.completed / progress.total) * 100) : 0;
  const active = progress?.status === "queued" || progress?.status === "running";
  return <section className={embedded ? "embedded-section" : "page-section"}>
    {!embedded && <PageHeading index="05" title={t("results")} subtitle={t("rawPNote")} />}
    {progress && <div className="card progress-card"><div className="section-title"><div><span className={`status-badge ${progress.status}`}>{t("status")}: {progress.status}</span><h2>{active ? t("running") : progress.message}</h2></div><strong>{percent.toFixed(1)}%</strong></div><div className="progress-track"><span style={{ width: `${percent}%` }} /></div><div className="stat-grid four compact-stats"><Stat label={t("completed")} value={`${progress.completed.toLocaleString()} / ${progress.total.toLocaleString()}`} /><Stat label={t("successful")} value={progress.successful.toLocaleString()} /><Stat label={t("excluded")} value={progress.excluded.toLocaleString()} /><Stat label={t("elapsed")} value={`${progress.elapsed_seconds.toFixed(1)}s`} /></div>{active && <button className="secondary-button danger" onClick={cancel}>{t("cancel")}</button>}</div>}
    {stale && <div className="notice warning">{t("previousResult")}</div>}
    {result && !result.best_model && <div className="empty-result"><span>∅</span><h2>{t("noResult")}</h2><p>{t("rawPNote")}</p></div>}
    {result?.best_model && <>
      {result.spec.standard_error !== "cluster" && <div className="notice warning">{t("diagnosticBlocked")}</div>}
      <div className="result-actions"><div><h2>{t("topModels")}</h2><p>{t("rawPNote")}</p></div><div className="button-row"><button className="secondary-button" onClick={() => exportResult("xlsx")}>{t("exportExcel")}</button><button className="secondary-button" onClick={() => exportResult("docx")}>{t("exportWord")}</button></div></div>
      <div className="table-wrap"><table className="results-table"><thead><tr><th>{t("rank")}</th><th>{t("controls")}</th><th>{t("estimate")}</th><th>{t("stdError")}</th><th>{t("tValue")}</th><th>{t("pValue")}</th><th>{t("observations")}</th><th>{t("diagnosticModel")}</th></tr></thead><tbody>{result.top_models.map((model) => <tr key={model.rank} onClick={() => selectModel(model)}><td><span className="rank-badge">{model.rank}</span></td><td>{model.controls.map((item) => <code className="control-code" key={item}>{item}</code>)}</td><td>{fmt(model.core_estimate)}</td><td>{fmt(model.core_std_error)}</td><td>{fmt(model.core_statistic)}</td><td className="p-value">{fmt(model.core_p_value, 7)}</td><td>{model.observations.toLocaleString()}</td><td><button className={`model-select-button ${diagnosticModelRank === model.rank ? "selected" : ""}`} onClick={(event) => { event.stopPropagation(); if (model.rank) setDiagnosticModelRank(model.rank); }}>{diagnosticModelRank === model.rank ? `✓ ${t("currentDiagnosticModel")}` : t("setDiagnosticModel")}</button></td></tr>)}</tbody></table></div>
      <div className="card inner-card command-card"><div><h3>{t("stataCommand")}</h3><code>{result.stata_command}</code></div><button className="secondary-button" onClick={() => navigator.clipboard.writeText(result.stata_command ?? "")}>{t("copy")}</button></div>
      {Object.keys(result.failure_counts).length > 0 && <div className="card inner-card"><h3>{t("failureReasons")}</h3><div className="failure-grid">{Object.entries(result.failure_counts).map(([key, value]) => <div key={key}><code>{key}</code><strong>{value.toLocaleString()}</strong></div>)}</div></div>}
    </>}
  </section>;
}

function SummaryRows({ dataset, config, preview, t }: { dataset: DatasetInfo; config: AnalysisSpec; preview: Preview | null; t: T }) {
  const firmRequired = controlsAtLevel(config, config.required_controls, "firm").length;
  const regionalRequired = controlsAtLevel(config, config.required_controls, "regional").length;
  const otherRequired = controlsAtLevel(config, config.required_controls, "other").length;
  const rows = [
    [t("rows"), dataset.filtered_rows.toLocaleString()],
    [t("dependent"), config.dependent || "—"],
    [t("core"), config.core || "—"],
    [t("required"), config.required_controls.join(", ") || "—"],
    [t("firmCandidates"), `${config.firm_candidate_controls.join(", ") || "—"} · Kf=${config.firm_control_target}, kf=${Math.max(0, config.firm_control_target - firmRequired)}`],
    [t("regionalCandidates"), `${config.regional_candidate_controls.join(", ") || "—"} · Kr=${config.regional_control_target}, kr=${Math.max(0, config.regional_control_target - regionalRequired)}`],
    [t("otherFixedControls"), config.fixed_other_controls.join(", ") || "—"],
    [t("totalControls"), (config.firm_control_target + config.regional_control_target + otherRequired + config.fixed_other_controls.length).toString()],
    [t("rawCombinations"), (preview?.raw_combination_count ?? 0).toLocaleString()],
    [t("dimensionExcluded"), (preview?.dimension_excluded_count ?? 0).toLocaleString()],
    [t("feasibleCombinations"), (preview?.feasible_combination_count ?? 0).toLocaleString()],
    [t("commonSample"), preview?.common_rows.toLocaleString() ?? "—"],
    [t("model"), config.model_type === "ols" ? t("ols") : t("fixedEffects")],
    [t("resource"), preview ? t(preview.resource_level) : "—"]
  ];
  return <dl className="summary-list">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

function CombinationPreview({ preview, t, compact = false }: { preview: Preview; t: T; compact?: boolean }) {
  return <div className={`card inner-card combination-preview-card ${compact ? "compact" : ""}`}><div className="section-title"><div><h2>{t("combinationPreview")}</h2>{!compact && <p>{t("candidatePoolHelp")}</p>}</div><span className="combination-total">{preview.total_combinations.toLocaleString()}</span></div>
    <ol className="combination-preview-list">{preview.combination_example_groups.map((combination, index) => <li key={`${index}-${[...combination.firm, ...combination.regional, ...combination.other].join("-")}`}><span>{index + 1}</span><div className="preview-groups">
      {combination.required.length > 0 && <PreviewGroup label={t("required")} names={combination.required} />}
      {combination.firm.length > 0 && <PreviewGroup label={t("firmLevel")} names={combination.firm} />}
      {combination.regional.length > 0 && <PreviewGroup label={t("regionalLevel")} names={combination.regional} />}
      {combination.other.length > 0 && <PreviewGroup label={t("otherLevel")} names={combination.other} />}
      {[...combination.required, ...combination.firm, ...combination.regional, ...combination.other].length === 0 && <em>{t("baselineCombination")}</em>}
    </div></li>)}</ol>
    {preview.combination_examples_truncated && <p className="preview-truncated">{t("combinationPreviewMore")}</p>}
  </div>;
}

function PreviewGroup({ label, names }: { label: string; names: string[] }) {
  return <span className="preview-group"><small>{label}</small>{names.map((name) => <code key={name}>{name}</code>)}</span>;
}

function ClassificationEditor({ variables, classifications, language, onChange, t }: { variables: VariableInfo[]; classifications: VariableClassification[]; language: Language; onChange: (values: VariableClassification[]) => void; t: T }) {
  const [search, setSearch] = useState("");
  const [levelFilter, setLevelFilter] = useState<ControlLevel | "">("");
  const [confidenceFilter, setConfidenceFilter] = useState<VariableClassification["confidence"] | "">("");
  const [pendingOnly, setPendingOnly] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [bulkLevel, setBulkLevel] = useState<ControlLevel>("firm");
  const [bulkDimension, setBulkDimension] = useState("unclassified");
  const info = new Map(variables.map((item) => [item.name, item]));
  const filtered = classifications.filter((item) => {
    const variable = info.get(item.variable);
    const matchesSearch = `${item.variable} ${variable?.label ?? ""}`.toLowerCase().includes(search.toLowerCase());
    return matchesSearch && (!levelFilter || item.level === levelFilter) && (!confidenceFilter || item.confidence === confidenceFilter) && (!pendingOnly || item.confidence === "low" || item.confidence === "unrecognized");
  });
  const replace = (variable: string, patch: Partial<VariableClassification>) => onChange(classifications.map((item) => item.variable === variable ? { ...item, ...patch, manually_modified: true } : item));
  const toggleSelected = (variable: string) => setSelected((current) => {
    const next = new Set(current);
    if (next.has(variable)) next.delete(variable); else next.add(variable);
    return next;
  });
  const applyBulk = () => {
    if (!selected.size) return;
    onChange(classifications.map((item) => selected.has(item.variable) ? { ...item, level: bulkLevel, dimension: bulkDimension, manually_modified: true } : item));
    setSelected(new Set());
  };
  const restore = () => {
    const targets = selected.size ? selected : new Set(filtered.map((item) => item.variable));
    onChange(classifications.map((item) => targets.has(item.variable) ? { ...item, level: item.suggested_level, dimension: item.suggested_dimension, manually_modified: false } : item));
    setSelected(new Set());
  };
  return <div className="classification-editor">
    <div className="classification-toolbar">
      <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("search")} />
      <select value={levelFilter} onChange={(event) => setLevelFilter(event.target.value as ControlLevel | "")}><option value="">{t("controlLevel")}</option><option value="firm">{t("firmLevel")}</option><option value="regional">{t("regionalLevel")}</option><option value="other">{t("otherLevel")}</option></select>
      <select value={confidenceFilter} onChange={(event) => setConfidenceFilter(event.target.value as VariableClassification["confidence"] | "")}><option value="">{t("confidenceLevel")}</option><option value="high">{t("highConfidence")}</option><option value="medium">{t("mediumConfidence")}</option><option value="low">{t("lowConfidence")}</option><option value="unrecognized">{t("unrecognizedConfidence")}</option></select>
      <label className="check-label"><input type="checkbox" checked={pendingOnly} onChange={(event) => setPendingOnly(event.target.checked)} />{t("pendingOnly")}</label>
    </div>
    <div className="bulk-toolbar"><span>{t("selectedCount")}: {selected.size}</span><select value={bulkLevel} onChange={(event) => { const level = event.target.value as ControlLevel; setBulkLevel(level); setBulkDimension("unclassified"); }}><option value="firm">{t("firmLevel")}</option><option value="regional">{t("regionalLevel")}</option><option value="other">{t("otherLevel")}</option></select><select value={bulkDimension} onChange={(event) => setBulkDimension(event.target.value)}>{dimensionsFor(bulkLevel).map((dimension) => <option value={dimension} key={dimension}>{controlDimensionLabels[language][dimension] ?? dimension}</option>)}</select><button type="button" className="secondary-button" disabled={!selected.size} onClick={applyBulk}>{t("bulkApply")}</button><button type="button" className="secondary-button" disabled={!filtered.length} onClick={restore}>{t("restoreSuggestion")}</button></div>
    <div className="table-wrap classification-table-wrap"><table><thead><tr><th><input type="checkbox" checked={filtered.length > 0 && filtered.every((item) => selected.has(item.variable))} onChange={(event) => setSelected(event.target.checked ? new Set(filtered.map((item) => item.variable)) : new Set())} /></th><th>{t("variable")}</th><th>{t("controlLevel")}</th><th>{t("economicDimension")}</th><th>{t("confidenceLevel")}</th><th>{t("matchReason")}</th></tr></thead><tbody>{filtered.map((item) => <tr key={item.variable} className={item.manually_modified ? "manually-edited" : ""}><td><input type="checkbox" checked={selected.has(item.variable)} onChange={() => toggleSelected(item.variable)} /></td><td><code>{item.variable}</code><small>{info.get(item.variable)?.label || "—"}</small></td><td><select value={item.level} onChange={(event) => replace(item.variable, { level: event.target.value as ControlLevel, dimension: "unclassified" })}><option value="firm">{t("firmLevel")}</option><option value="regional">{t("regionalLevel")}</option><option value="other">{t("otherLevel")}</option></select></td><td><select value={item.dimension} onChange={(event) => replace(item.variable, { dimension: event.target.value })}>{dimensionsFor(item.level).map((dimension) => <option key={dimension} value={dimension}>{controlDimensionLabels[language][dimension] ?? dimension}</option>)}</select></td><td><span className={`confidence-badge ${item.confidence}`}>{confidenceText(item.confidence, t)}</span>{item.manually_modified && <small>{t("manualEdit")}</small>}</td><td className="classification-reason">{item.reason}</td></tr>)}</tbody></table></div>
  </div>;
}

function confidenceText(value: VariableClassification["confidence"], t: T): string {
  if (value === "high") return t("highConfidence");
  if (value === "medium") return t("mediumConfidence");
  if (value === "low") return t("lowConfidence");
  return t("unrecognizedConfidence");
}

function CandidatePoolPicker({ variables, selected, disabled, onChange, classifications, language, t }: { variables: VariableInfo[]; selected: string[]; disabled: Set<string>; onChange: (names: string[]) => void; classifications: Map<string, VariableClassification>; language: Language; t: T }) {
  const [search, setSearch] = useState("");
  const [dimensionFilter, setDimensionFilter] = useState("");
  const [availableMarked, setAvailableMarked] = useState<Set<string>>(() => new Set());
  const [poolMarked, setPoolMarked] = useState<Set<string>>(() => new Set());
  const normalizedSearch = search.trim().toLowerCase();
  const available = variables.filter((item) => !selected.includes(item.name) && !disabled.has(item.name));
  const dimensions = [...new Set(variables.map((item) => classifications.get(item.name)?.dimension ?? "unclassified"))];
  const filteredAvailable = available.filter((item) => `${item.name} ${item.label}`.toLowerCase().includes(normalizedSearch) && (!dimensionFilter || classifications.get(item.name)?.dimension === dimensionFilter));
  const pool = variables.filter((item) => selected.includes(item.name));
  const toggleMark = (setter: Dispatch<SetStateAction<Set<string>>>, name: string) => setter((current) => {
    const next = new Set(current);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    return next;
  });
  const add = (names: string[]) => {
    const allowed = new Set(available.map((item) => item.name));
    const additions = names.filter((name) => allowed.has(name));
    if (additions.length === 0) return;
    onChange([...selected, ...additions]);
    setAvailableMarked(new Set());
  };
  const remove = (names: string[]) => {
    const removals = new Set(names);
    if (removals.size === 0) return;
    onChange(selected.filter((name) => !removals.has(name)));
    setPoolMarked(new Set());
  };
  const markedAvailable = filteredAvailable.filter((item) => availableMarked.has(item.name));
  const markedPool = pool.filter((item) => poolMarked.has(item.name));
  return <div className="candidate-pool-editor">
    <div className="candidate-column">
      <div className="candidate-column-header"><div><strong>{t("availableVariables")}</strong><span>{available.length}</span></div><small>{t("selectedCount")}: {markedAvailable.length}</small></div>
      <div className="candidate-filters"><input className="picker-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t("search")} /><select value={dimensionFilter} onChange={(event) => setDimensionFilter(event.target.value)}><option value="">{t("allDimensions")}</option>{dimensions.map((dimension) => <option value={dimension} key={dimension}>{controlDimensionLabels[language][dimension] ?? dimension}</option>)}</select></div>
      <div className="candidate-list">
        {filteredAvailable.map((item) => <label className={availableMarked.has(item.name) ? "marked" : ""} key={item.name}><input type="checkbox" checked={availableMarked.has(item.name)} onChange={() => toggleMark(setAvailableMarked, item.name)} /><span><code>{item.name}</code><small>{item.label || item.dtype}</small><small className="dimension-caption">{controlDimensionLabels[language][classifications.get(item.name)?.dimension ?? "unclassified"]}</small></span></label>)}
        {filteredAvailable.length === 0 && <p className="candidate-empty">—</p>}
      </div>
      <div className="candidate-actions"><button type="button" className="secondary-button" disabled={markedAvailable.length === 0} onClick={() => add(markedAvailable.map((item) => item.name))}>{t("addSelected")} →</button><button type="button" className="secondary-button" disabled={filteredAvailable.length === 0} onClick={() => add(filteredAvailable.map((item) => item.name))}>{t("addFiltered")}</button></div>
    </div>
    <div className="candidate-transfer-mark" aria-hidden="true">C(n,k)</div>
    <div className="candidate-column pool-column">
      <div className="candidate-column-header"><div><strong>{t("candidatePoolTitle")}</strong><span>{pool.length}</span></div><small>{t("selectedCount")}: {markedPool.length}</small></div>
      <div className="candidate-list">
        {pool.map((item) => <label className={poolMarked.has(item.name) ? "marked" : ""} key={item.name}><input type="checkbox" checked={poolMarked.has(item.name)} onChange={() => toggleMark(setPoolMarked, item.name)} /><span><code>{item.name}</code><small>{item.label || item.dtype}</small><small className="dimension-caption">{controlDimensionLabels[language][classifications.get(item.name)?.dimension ?? "unclassified"]}</small></span></label>)}
        {pool.length === 0 && <p className="candidate-empty">{t("poolEmpty")}</p>}
      </div>
      <div className="candidate-actions"><button type="button" className="secondary-button" disabled={markedPool.length === 0} onClick={() => remove(markedPool.map((item) => item.name))}>← {t("removeSelected")}</button><button type="button" className="secondary-button" disabled={pool.length === 0} onClick={() => { onChange([]); setPoolMarked(new Set()); }}>{t("clearPool")}</button></div>
    </div>
  </div>;
}

function MultiPicker({ variables, selected, disabled, onToggle, t, annotations, warningNames = new Set<string>(), language = "zh" }: { variables: VariableInfo[]; selected: string[]; disabled: Set<string>; onToggle: (name: string) => void; t: T; annotations?: Map<string, VariableClassification>; warningNames?: Set<string>; language?: Language }) {
  const [search, setSearch] = useState("");
  const filtered = variables.filter((item) => `${item.name} ${item.label}`.toLowerCase().includes(search.toLowerCase()));
  return <div className="multi-picker"><input className="picker-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("search")} /><div className="picker-options">{filtered.map((item) => <label className={`${selected.includes(item.name) ? "selected" : ""} ${disabled.has(item.name) && !selected.includes(item.name) ? "disabled" : ""} ${warningNames.has(item.name) ? "warning" : ""}`} key={item.name}><input type="checkbox" checked={selected.includes(item.name)} disabled={disabled.has(item.name) && !selected.includes(item.name)} onChange={() => onToggle(item.name)} /><span><code>{item.name}</code><small>{item.label || item.dtype}</small>{annotations?.get(item.name) && <small className="dimension-caption">{annotations.get(item.name)?.level === "firm" ? t("firmLevel") : annotations.get(item.name)?.level === "regional" ? t("regionalLevel") : t("otherLevel")} · {controlDimensionLabels[language][annotations.get(item.name)?.dimension ?? "unclassified"]}</small>}</span></label>)}</div>{selected.length > 0 && <div className="selected-summary">{selected.map((name) => <button type="button" key={name} onClick={() => onToggle(name)}><code>{name}</code> ×</button>)}</div>}</div>;
}

function VariableSelect({ variables, value, onChange, placeholder }: { variables: VariableInfo[]; value: string; onChange: (value: string) => void; placeholder: string }) {
  return <select value={value} onChange={(e) => onChange(e.target.value)}><option value="">{placeholder}</option>{variables.map((item) => <option key={item.name} value={item.name}>{item.name}{item.label ? ` — ${item.label}` : ""}</option>)}</select>;
}

function ModelDrawer({ model, close, t }: { model: ModelResult; close: () => void; t: T }) {
  return <div className="drawer-backdrop" onMouseDown={close}><aside className="model-drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-header"><div><span className="eyebrow">#{model.rank}</span><h2>{t("details")}</h2></div><button onClick={close}>×</button></div><div className="drawer-stats"><Stat label={t("observations")} value={model.observations.toLocaleString()} /><Stat label={t("dfResidual")} value={fmt(model.df_residual)} /><Stat label={t("rSquared")} value={fmt(model.r_squared)} /><Stat label={t("adjustedR2")} value={fmt(model.adjusted_r_squared)} /></div><h3>{t("controls")}</h3><div className="chip-row">{model.controls.map((item) => <code className="control-code" key={item}>{item}</code>)}</div><div className="table-wrap"><table><thead><tr><th>{t("variable")}</th><th>{t("estimate")}</th><th>{t("stdError")}</th><th>{t("tValue")}</th><th>{t("pValue")}</th><th>{t("confidence")}</th></tr></thead><tbody>{model.coefficients.map((item) => <tr key={item.variable}><td><code>{item.variable}</code></td><td>{fmt(item.estimate)}</td><td>{fmt(item.std_error)}</td><td>{fmt(item.statistic)}</td><td>{fmt(item.p_value, 7)}</td><td>[{fmt(item.conf_low)}, {fmt(item.conf_high)}]</td></tr>)}</tbody></table></div></aside></div>;
}

export function CacheHelpPanel({ status, busy, error, language, t, cleanupButtonRef, refresh, requestCleanup }: { status: CacheStatus | null; busy: boolean; error: string; language: Language; t: T; cleanupButtonRef: RefObject<HTMLButtonElement | null>; refresh: () => void; requestCleanup: () => void }) {
  const state = busy ? "cleaning" : status?.state ?? "normal";
  const stateText = state === "cleaning" ? t("cacheCleaning") : state === "near_limit" ? t("cacheNearLimit") : state === "protected_over_budget" ? t("cacheProtectedOver") : t("cacheNormal");
  const stateIcon = state === "cleaning" ? "↻" : state === "near_limit" || state === "protected_over_budget" ? "⚠" : "✓";
  return <details className="cache-help-section">
    <summary><span>{t("cacheStorage")}</span><span className={`cache-state ${state}`}>{stateIcon} {stateText}</span></summary>
    <div className="cache-help-body">
      {!status ? <div className="cache-loading"><span className="spinner dark" />{t("cacheLoading")}</div> : <>
        <div className="cache-metric-grid">
          <Stat label={t("cacheTotal")} value={bytes(status.estimated_total_bytes)} />
          <Stat label={t("cacheProtected")} value={bytes(status.protected_bytes)} />
          <Stat label={t("cacheHistorical")} value={bytes(status.historical_bytes)} />
          <Stat label={t("cacheReclaimable")} value={bytes(status.reclaimable_bytes)} />
        </div>
        <div className="cache-usage" aria-label={t("cacheUsage")}><span style={{ width: `${Math.min(100, status.historical_bytes / status.history_budget_bytes * 100)}%` }} /></div>
        <dl className="cache-details">
          <div><dt>{t("cacheResources")}</dt><dd>{status.counts.datasets} / {status.counts.analysis_jobs} / {status.counts.diagnostic_jobs}</dd></div>
          <div><dt>{t("cacheDisk")}</dt><dd>{bytes(status.disk_bytes)}</dd></div>
          <div><dt>{t("cacheRetention")}</dt><dd>{t("cacheRetentionValue")}</dd></div>
          <div><dt>{t("cacheLastCleanup")}</dt><dd>{status.last_cleanup_at ? new Date(status.last_cleanup_at).toLocaleString(language === "zh" ? "zh-CN" : "en-US") : t("cacheNever")}</dd></div>
          <div><dt>{t("cacheLastReleased")}</dt><dd>{bytes(status.last_reclaimed_bytes)} · {status.last_reclaimed_resources}</dd></div>
          {status.pending_retired_datasets > 0 && <div><dt>{t("cachePendingReplacement")}</dt><dd>{status.pending_retired_datasets}</dd></div>}
        </dl>
        {status.state === "protected_over_budget" && <div className="notice warning compact-notice">⚠ {t("cacheProtectedWarning")}</div>}
        {status.pending_retired_datasets > 0 && <div className="notice warning compact-notice">↻ {t("cachePendingReplacementHelp")}</div>}
        {status.reclaimable_resources === 0 && <p className="cache-empty">✓ {t("cacheNothing")}</p>}
      </>}
      {error && <div className="notice error cache-error" role="alert"><span>{error}</span><button onClick={refresh}>{t("retry")}</button></div>}
      <div className="cache-actions"><button className="secondary-button" disabled={busy} onClick={refresh}>{t("cacheRefresh")}</button><button ref={cleanupButtonRef} className="primary-button" disabled={busy || !status || status.reclaimable_resources === 0} onClick={requestCleanup}>{t("cacheCleanNow")}</button></div>
    </div>
  </details>;
}

export function Modal({ title, close, children, active = true, className = "" }: { title: string; close: () => void; children: React.ReactNode; active?: boolean; className?: string }) {
  const modalRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(close);
  closeRef.current = close;
  const titleId = useId();
  useEffect(() => {
    if (!active || !modalRef.current) return;
    const previous = document.activeElement as HTMLElement | null;
    const modal = modalRef.current;
    const focusable = () => [...modal.querySelectorAll<HTMLElement>('button:not([disabled]), summary, input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
    (modal.querySelector<HTMLElement>("[data-autofocus]") ?? focusable()[0] ?? modal).focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      if (modal.contains(document.activeElement)) previous?.focus();
    };
  }, [active]);
  return <div className="modal-backdrop" onMouseDown={active ? close : undefined} aria-hidden={!active || undefined}><div ref={modalRef} className={`modal ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onMouseDown={(event) => event.stopPropagation()}><div className="drawer-header"><h2 id={titleId}>{title}</h2><button aria-label="Close" onClick={close}>×</button></div>{children}</div></div>;
}

function PageHeading({ index, title, subtitle }: { index: string; title: string; subtitle: string }) {
  return <div className="page-heading"><span>{index}</span><div><h1>{title}</h1><p>{subtitle}</p></div></div>;
}

function ExpiredResultState({ t, onReturn }: { t: T; onReturn: () => void }) {
  return <div className="card diagnostic-empty expired-result-state"><span>♻</span><h2>{t("cacheExpiredResult")}</h2><p>{t("cacheRetentionValue")}</p><button className="primary-button" onClick={onReturn}>{t("returnToConfig")}</button></div>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>;
}

function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="labeled"><span>{label}</span>{children}</label>;
}

function IssueList({ title, items, tone }: { title: string; items: string[]; tone: "error" | "warning" }) {
  return <div className={`notice ${tone}`}><strong>{title}</strong><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div>;
}
