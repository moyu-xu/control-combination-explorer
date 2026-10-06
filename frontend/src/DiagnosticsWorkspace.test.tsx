import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DiagnosticsWorkspace, type DiagnosticController } from "./DiagnosticsWorkspace";
import type { AnalysisResult, DatasetInfo, DiagnosticResult, DiagnosticSpec } from "./types";

const dataset: DatasetInfo = {
  id: "data",
  filename: "panel.dta",
  size_bytes: 100,
  rows: 80,
  filtered_rows: 80,
  columns: 6,
  encoding: "utf-8",
  file_format: "Stata",
  variables: ["firm", "year", "g", "treated", "y", "x"].map((name) => ({
    name,
    label: name === "y" ? "Outcome" : "",
    dtype: "int64",
    numeric: true,
    non_missing: 80,
    missing: 0,
    examples: [],
    derived: false
  })),
  derived: [],
  filter_expression: "",
  filter_applied: false,
  revision: 0
};

const result: AnalysisResult = {
  job_id: "analysis",
  spec: {
    dataset_id: "data",
    language: "zh",
    dependent: "y",
    core: "treated",
    required_controls: [],
    firm_candidate_controls: ["x"],
    regional_candidate_controls: [],
    fixed_other_controls: [],
    firm_control_target: 1,
    regional_control_target: 0,
    control_classifications: [{ variable: "x", suggested_level: "firm", suggested_dimension: "unclassified", level: "firm", dimension: "unclassified", confidence: "unrecognized", reason: "fixture", manually_modified: false }],
    classification_rule_version: "fixture",
    classification_confirmed: true,
    dimension_conflict_overrides: [],
    model_type: "fixed_effects",
    fixed_effects: ["firm", "year"],
    standard_error: "cluster",
    cluster_variable: "firm",
    expected_sign: "positive",
    top_n: 20,
    significance: 0.05
  },
  sample: {},
  total_combinations: 1,
  successful_models: 1,
  excluded_models: 0,
  failure_counts: {},
  top_models: [{
    rank: 1,
    controls: ["x"],
    core_estimate: 1,
    core_std_error: 0.1,
    core_statistic: 10,
    core_p_value: 0.001,
    observations: 80,
    df_residual: 70,
    r_squared: 0.5,
    adjusted_r_squared: 0.4,
    within_r_squared: 0.3,
    coefficients: []
  }],
  best_model: null,
  stata_command: null,
  completed_at: "2026-10-06T00:00:00Z",
  dataset_revision: 0
};
result.best_model = result.top_models[0];

const spec: DiagnosticSpec = {
  analysis_job_id: "analysis",
  model_rank: 1,
  language: "zh",
  panel_id: "firm",
  time_variable: "year",
  treatment: {
    mode: "cohort",
    variable: "g",
    never_treated_mode: "zero",
    never_treated_value: null
  },
  window_start: -5,
  window_end: 5,
  extra_fixed_effects: [],
  event_methods: ["saturated", "did2s"],
  run_pretrend_test: true,
  placebo_methods: ["random_group"],
  placebo_estimator: "did2s",
  repetitions: 500,
  random_seed: 12345,
  show_density: true,
  labels: {
    title_zh: "事件研究",
    title_en: "Event study",
    y_axis_zh: "政策效应",
    y_axis_en: "Treatment effect",
    unit_zh: "",
    unit_en: ""
  }
};

const modelSnapshot = result.top_models[0];
const eventDiagnostic: DiagnosticResult = {
  job_id: "event-job",
  kind: "event_study",
  spec: { ...spec, event_methods: ["did2s"] },
  dataset_revision: 0,
  model: modelSnapshot,
  sample: { clusters: 10 },
  event_studies: [{
    method: "did2s",
    points: [{ event_time: -1, estimate: 0, std_error: 0, conf_low: 0, conf_high: 0, weight: 1, identifiable: true }],
    observations: 80,
    clusters: 10,
    pretrend_statistic: null,
    pretrend_df: null,
    pretrend_p_value: null,
    warnings: []
  }],
  placebos: [],
  completed_at: "2026-10-06T00:00:00Z"
};

const placeboDiagnostic: DiagnosticResult = {
  job_id: "placebo-job",
  kind: "placebo",
  spec,
  dataset_revision: 0,
  model: modelSnapshot,
  sample: { clusters: 10 },
  event_studies: [],
  placebos: [{
    method: "random_group",
    estimator: "did2s",
    actual_att: 1,
    draws: Array.from({ length: 60 }, (_, index) => ({ iteration: index + 1, estimate: index / 100 })),
    mean: 0.295,
    std_dev: 0.174,
    quantile_low: 0.01,
    quantile_high: 0.58,
    empirical_p_value: 0.02,
    attempted: 60,
    failed: 0,
    warnings: []
  }],
  completed_at: "2026-10-06T00:00:00Z"
};

function controller(overrides: Partial<DiagnosticController> = {}): DiagnosticController {
  return {
    spec,
    preview: null,
    progress: null,
    eventResult: null,
    placeboResult: null,
    eventStale: false,
    placeboStale: false,
    eventExpired: false,
    placeboExpired: false,
    update: vi.fn(),
    validate: vi.fn(async () => null),
    run: vi.fn(async () => undefined),
    cancel: vi.fn(async () => undefined),
    download: vi.fn(async () => undefined),
    expireResources: vi.fn(),
    ...overrides
  };
}

const t = (key: string) => key;

describe("DiagnosticsWorkspace", () => {
  it("switches treatment-dependent fields", () => {
    const state = controller();
    render(<DiagnosticsWorkspace dataset={dataset} result={result} controller={state} language="zh" t={t as never} />);
    expect(screen.getByText("neverTreated")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("treatmentInput"), { target: { value: "indicator" } });
    expect(state.update).toHaveBeenCalledWith(expect.objectContaining({ treatment: expect.objectContaining({ mode: "indicator" }) }));
  });

  it("keeps event and placebo runs independent", () => {
    const state = controller();
    render(<DiagnosticsWorkspace dataset={dataset} result={result} controller={state} language="zh" t={t as never} />);
    fireEvent.click(screen.getByRole("button", { name: "runEventStudy" }));
    fireEvent.click(screen.getByRole("button", { name: "runPlacebo" }));
    expect(state.run).toHaveBeenNthCalledWith(1, "event-study");
    expect(state.run).toHaveBeenNthCalledWith(2, "placebo");
  });

  it("keeps stale results visible while disabling exports", () => {
    const state = controller({ eventResult: eventDiagnostic, eventStale: true });
    render(<DiagnosticsWorkspace dataset={dataset} result={result} controller={state} language="zh" t={t as never} />);
    expect(screen.getByText(/staleDiagnostic/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "downloadPng" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "did2s" })).not.toBeInTheDocument();
  });

  it("paginates placebo draws instead of rendering the entire simulation", () => {
    const state = controller({ placeboResult: placeboDiagnostic });
    render(<DiagnosticsWorkspace dataset={dataset} result={result} controller={state} language="zh" t={t as never} />);
    fireEvent.click(screen.getByRole("tab", { name: "placeboTab" }));
    fireEvent.click(screen.getByRole("button", { name: "viewData" }));
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.getByText("51")).toBeInTheDocument();
  });

  it("replaces an expired diagnostic with a rerun-oriented empty state", () => {
    const state = controller({ eventExpired: true });
    render(<DiagnosticsWorkspace dataset={dataset} result={result} controller={state} language="zh" t={t as never} />);
    expect(screen.getByText("cacheExpiredResult")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "returnToConfig" })).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
