export type Language = "zh" | "en";
export type Mode = "wizard" | "advanced";

export interface VariableInfo {
  name: string;
  label: string;
  dtype: string;
  numeric: boolean;
  non_missing: number;
  missing: number;
  examples: unknown[];
  derived: boolean;
}

export interface DerivedSpec {
  name: string;
  kind: "log" | "ratio";
  source: string;
  denominator?: string;
}

export interface DatasetInfo {
  id: string;
  filename: string;
  size_bytes: number;
  rows: number;
  filtered_rows: number;
  columns: number;
  encoding: string;
  file_format: string;
  variables: VariableInfo[];
  derived: DerivedSpec[];
  filter_expression: string;
  filter_applied: boolean;
  revision: number;
}

export type ControlLevel = "firm" | "regional" | "other";
export type ClassificationConfidence = "high" | "medium" | "low" | "unrecognized";

export interface VariableClassification {
  variable: string;
  suggested_level: ControlLevel;
  suggested_dimension: string;
  level: ControlLevel;
  dimension: string;
  confidence: ClassificationConfidence;
  reason: string;
  manually_modified: boolean;
}

export interface ClassificationResponse {
  rule_version: string;
  classifications: VariableClassification[];
}

export interface AnalysisSpec {
  dataset_id: string;
  language: Language;
  dependent: string;
  core: string;
  required_controls: string[];
  firm_candidate_controls: string[];
  regional_candidate_controls: string[];
  fixed_other_controls: string[];
  firm_control_target: number;
  regional_control_target: number;
  control_classifications: VariableClassification[];
  classification_rule_version: string;
  classification_confirmed: boolean;
  dimension_conflict_overrides: string[];
  model_type: "ols" | "fixed_effects";
  fixed_effects: string[];
  standard_error: "iid" | "robust" | "cluster";
  cluster_variable: string | null;
  expected_sign: "positive" | "negative";
  top_n: 5 | 10 | 20 | 50 | 100;
  significance: 0.05;
}

export interface Preview {
  valid: boolean;
  errors: string[];
  warnings: string[];
  source_rows: number;
  filtered_rows: number;
  common_rows: number;
  dropped_for_common_sample: number;
  candidate_pool: number;
  candidate_count: number;
  firm_candidate_pool: number;
  regional_candidate_pool: number;
  firm_required_count: number;
  regional_required_count: number;
  other_control_count: number;
  firm_candidate_count: number;
  regional_candidate_count: number;
  total_control_count: number;
  raw_combination_count: number;
  dimension_excluded_count: number;
  feasible_combination_count: number;
  total_combinations: number;
  combination_examples: string[][];
  combination_example_groups: Array<{
    required: string[];
    firm: string[];
    regional: string[];
    other: string[];
  }>;
  combination_examples_truncated: boolean;
  target_adjustments: string[];
  blocking_errors: string[];
  resource_level: "low" | "medium" | "high";
}

export type JobState = "queued" | "running" | "completed" | "cancelled" | "failed";

export interface JobProgress {
  job_id: string;
  status: JobState;
  total: number;
  completed: number;
  successful: number;
  excluded: number;
  elapsed_seconds: number;
  message: string;
  failure_counts: Record<string, number>;
}

export interface CoefficientResult {
  variable: string;
  estimate: number;
  std_error: number;
  statistic: number;
  p_value: number;
  conf_low: number;
  conf_high: number;
}

export interface ModelResult {
  rank: number | null;
  controls: string[];
  core_estimate: number;
  core_std_error: number;
  core_statistic: number;
  core_p_value: number;
  observations: number;
  df_residual: number | null;
  r_squared: number | null;
  adjusted_r_squared: number | null;
  within_r_squared: number | null;
  coefficients: CoefficientResult[];
}

export interface AnalysisResult {
  job_id: string;
  spec: AnalysisSpec;
  sample: Record<string, unknown>;
  total_combinations: number;
  successful_models: number;
  excluded_models: number;
  failure_counts: Record<string, number>;
  top_models: ModelResult[];
  best_model: ModelResult | null;
  stata_command: string | null;
  completed_at: string;
  dataset_revision: number;
}

export type EventStudyMethod = "saturated" | "did2s";
export type PlaceboMethod = "random_group" | "random_timing" | "permute_outcome";

export interface DiagnosticSpec {
  analysis_job_id: string;
  model_rank: number;
  language: Language;
  panel_id: string;
  time_variable: string;
  treatment: {
    mode: "cohort" | "indicator";
    variable: string;
    never_treated_mode: "zero" | "missing" | "custom";
    never_treated_value: number | null;
  };
  window_start: number;
  window_end: number;
  extra_fixed_effects: string[];
  event_methods: EventStudyMethod[];
  run_pretrend_test: boolean;
  placebo_methods: PlaceboMethod[];
  placebo_estimator: EventStudyMethod;
  repetitions: 100 | 500 | 1000 | 2000;
  random_seed: number;
  show_density: boolean;
  labels: {
    title_zh: string;
    title_en: string;
    y_axis_zh: string;
    y_axis_en: string;
    unit_zh: string;
    unit_en: string;
  };
}

export interface DiagnosticPreview {
  valid: boolean;
  errors: string[];
  warnings: string[];
  rows: number;
  units: number;
  periods: number;
  clusters: number;
  treated_units: number;
  never_treated_units: number;
  event_models: number;
  placebo_regressions: number;
  sample: Record<string, unknown>;
}

export interface DiagnosticProgress {
  job_id: string;
  kind: "event_study" | "placebo";
  status: JobState;
  stage: string;
  method: string;
  total: number;
  completed: number;
  successful: number;
  failed: number;
  elapsed_seconds: number;
  message: string;
}

export interface EventStudyPoint {
  event_time: number;
  estimate: number | null;
  std_error: number | null;
  conf_low: number | null;
  conf_high: number | null;
  weight: number | null;
  identifiable: boolean;
}

export interface EventStudyResult {
  method: EventStudyMethod;
  points: EventStudyPoint[];
  observations: number;
  clusters: number;
  pretrend_statistic: number | null;
  pretrend_df: number | null;
  pretrend_p_value: number | null;
  warnings: string[];
}

export interface PlaceboResult {
  method: PlaceboMethod;
  estimator: EventStudyMethod;
  actual_att: number;
  draws: { iteration: number; estimate: number }[];
  mean: number;
  std_dev: number;
  quantile_low: number;
  quantile_high: number;
  empirical_p_value: number;
  attempted: number;
  failed: number;
  warnings: string[];
}

export interface DiagnosticResult {
  job_id: string;
  kind: "event_study" | "placebo";
  spec: DiagnosticSpec;
  dataset_revision: number;
  model: ModelResult;
  sample: Record<string, unknown>;
  event_studies: EventStudyResult[];
  placebos: PlaceboResult[];
  completed_at: string;
}

export interface CacheStatus {
  state: "normal" | "near_limit" | "protected_over_budget" | "cleaning";
  estimated_total_bytes: number;
  protected_bytes: number;
  historical_bytes: number;
  reclaimable_bytes: number;
  disk_bytes: number;
  history_budget_bytes: number;
  retention_seconds: number;
  sweep_seconds: number;
  lease_seconds: number;
  counts: {
    datasets: number;
    analysis_jobs: number;
    diagnostic_jobs: number;
  };
  reclaimable_resources: number;
  last_cleanup_at: string | null;
  last_reclaimed_bytes: number;
  last_reclaimed_resources: number;
  total_reclaimed_bytes: number;
  pending_retired_datasets: number;
  missing_resources?: Array<{
    kind: "dataset" | "analysis" | "diagnostic";
    id: string;
    expired: boolean;
  }>;
}

export interface CacheCleanupPreview {
  resources: number;
  estimated_bytes: number;
}

export interface CacheCleanupResult {
  reclaimed_bytes: number;
  reclaimed_resources: number;
  status: CacheStatus;
}

