from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Language(StrEnum):
    zh = "zh"
    en = "en"


class DerivedKind(StrEnum):
    log = "log"
    ratio = "ratio"


class DerivedVariableSpec(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
    kind: DerivedKind
    source: str
    denominator: str | None = None

    @model_validator(mode="after")
    def validate_ratio(self) -> DerivedVariableSpec:
        if self.kind == DerivedKind.ratio and not self.denominator:
            raise ValueError("A ratio variable requires a denominator")
        return self


class FilterRequest(BaseModel):
    expression: str = ""


class ModelType(StrEnum):
    ols = "ols"
    fixed_effects = "fixed_effects"


class StandardErrorType(StrEnum):
    iid = "iid"
    robust = "robust"
    cluster = "cluster"


class ExpectedSign(StrEnum):
    positive = "positive"
    negative = "negative"


class ControlLevel(StrEnum):
    firm = "firm"
    regional = "regional"
    other = "other"


class ClassificationConfidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"
    unrecognized = "unrecognized"


class VariableClassification(BaseModel):
    variable: str
    suggested_level: ControlLevel
    suggested_dimension: str = "unclassified"
    level: ControlLevel
    dimension: str = "unclassified"
    confidence: ClassificationConfidence = ClassificationConfidence.unrecognized
    reason: str = ""
    manually_modified: bool = False


class AnalysisSpec(BaseModel):
    dataset_id: str
    language: Language = Language.zh
    dependent: str
    core: str
    required_controls: list[str] = Field(default_factory=list)
    firm_candidate_controls: list[str] = Field(default_factory=list)
    regional_candidate_controls: list[str] = Field(default_factory=list)
    fixed_other_controls: list[str] = Field(default_factory=list)
    firm_control_target: int = Field(default=0, ge=0)
    regional_control_target: int = Field(default=0, ge=0)
    control_classifications: list[VariableClassification] = Field(default_factory=list)
    classification_rule_version: str = ""
    classification_confirmed: bool = False
    dimension_conflict_overrides: list[str] = Field(default_factory=list)
    # Accepted only so requests made by earlier local builds can still be opened.
    candidate_controls: list[str] | None = Field(default=None, exclude=True)
    candidate_count: int | None = Field(default=None, ge=0, exclude=True)
    model_type: ModelType = ModelType.ols
    fixed_effects: list[str] = Field(default_factory=list)
    standard_error: StandardErrorType = StandardErrorType.robust
    cluster_variable: str | None = None
    expected_sign: ExpectedSign
    top_n: Literal[5, 10, 20, 50, 100] = 20
    significance: float = 0.05

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_candidate_pool(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "firm_candidate_controls" in value:
            return value
        if "candidate_controls" not in value:
            return value
        migrated = dict(value)
        candidates = list(migrated.get("candidate_controls") or [])
        required = list(migrated.get("required_controls") or [])
        count = int(migrated.get("candidate_count") or 0)
        migrated["firm_candidate_controls"] = candidates
        migrated["regional_candidate_controls"] = []
        migrated["fixed_other_controls"] = []
        migrated["firm_control_target"] = len(required) + count
        migrated["regional_control_target"] = 0
        migrated["classification_confirmed"] = True
        migrated["classification_rule_version"] = "legacy-single-pool"
        migrated["control_classifications"] = [
            {
                "variable": name,
                "suggested_level": "firm",
                "suggested_dimension": "unclassified",
                "level": "firm",
                "dimension": "unclassified",
                "confidence": "unrecognized",
                "reason": "Migrated from the legacy candidate pool",
            }
            for name in dict.fromkeys([*required, *candidates])
        ]
        return migrated

    @model_validator(mode="after")
    def validate_spec(self) -> AnalysisSpec:
        roles = [
            self.dependent,
            self.core,
            *self.required_controls,
            *self.firm_candidate_controls,
            *self.regional_candidate_controls,
            *self.fixed_other_controls,
        ]
        if len(roles) != len(set(roles)):
            raise ValueError("Regression variable roles must be mutually exclusive")
        classified = [item.variable for item in self.control_classifications]
        if len(classified) != len(set(classified)):
            raise ValueError("Each variable can have only one control classification")
        if self.model_type == ModelType.fixed_effects and not self.fixed_effects:
            raise ValueError("At least one fixed effect is required")
        if self.model_type == ModelType.ols and self.fixed_effects:
            raise ValueError("OLS cannot include absorbed fixed effects")
        if set(self.fixed_effects) & set(roles):
            raise ValueError("Fixed effects cannot also be regression terms")
        if self.standard_error == StandardErrorType.cluster and not self.cluster_variable:
            raise ValueError("A cluster variable is required")
        if self.significance != 0.05:
            raise ValueError("v1 uses a fixed significance threshold of 0.05")
        return self


class CoefficientResult(BaseModel):
    variable: str
    estimate: float
    std_error: float
    statistic: float
    p_value: float
    conf_low: float
    conf_high: float


class ModelResult(BaseModel):
    rank: int | None = None
    controls: list[str]
    core_estimate: float
    core_std_error: float
    core_statistic: float
    core_p_value: float
    observations: int
    df_residual: float | None = None
    r_squared: float | None = None
    adjusted_r_squared: float | None = None
    within_r_squared: float | None = None
    coefficients: list[CoefficientResult]


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class JobProgress(BaseModel):
    job_id: str
    status: JobStatus
    total: int
    completed: int
    successful: int
    excluded: int
    elapsed_seconds: float
    message: str = ""
    failure_counts: dict[str, int] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    job_id: str
    spec: AnalysisSpec
    sample: dict[str, Any]
    total_combinations: int
    successful_models: int
    excluded_models: int
    failure_counts: dict[str, int]
    top_models: list[ModelResult]
    best_model: ModelResult | None
    stata_command: str | None
    completed_at: str
    dataset_revision: int = 0


class TreatmentInputMode(StrEnum):
    cohort = "cohort"
    indicator = "indicator"


class NeverTreatedMode(StrEnum):
    zero = "zero"
    missing = "missing"
    custom = "custom"


class EventStudyMethod(StrEnum):
    saturated = "saturated"
    did2s = "did2s"


class PlaceboMethod(StrEnum):
    random_group = "random_group"
    random_timing = "random_timing"
    permute_outcome = "permute_outcome"


class TreatmentTimingSpec(BaseModel):
    mode: TreatmentInputMode = TreatmentInputMode.cohort
    variable: str
    never_treated_mode: NeverTreatedMode = NeverTreatedMode.zero
    never_treated_value: float | None = None

    @model_validator(mode="after")
    def validate_never_treated(self) -> TreatmentTimingSpec:
        if self.never_treated_mode == NeverTreatedMode.custom and self.never_treated_value is None:
            raise ValueError("A custom never-treated code is required")
        return self


class FigureLabels(BaseModel):
    title_zh: str = "事件研究估计结果"
    title_en: str = "Event-study estimates"
    y_axis_zh: str = "政策效应"
    y_axis_en: str = "Treatment effect"
    unit_zh: str = ""
    unit_en: str = ""


class FigureRenderSettings(BaseModel):
    language: Language
    labels: FigureLabels
    show_density: bool = True


class DiagnosticSpec(BaseModel):
    analysis_job_id: str
    model_rank: int = Field(ge=1)
    language: Language = Language.zh
    panel_id: str
    time_variable: str
    treatment: TreatmentTimingSpec
    window_start: int = Field(default=-5, ge=-8, le=-2)
    window_end: int = Field(default=5, ge=0, le=8)
    extra_fixed_effects: list[str] = Field(default_factory=list)
    event_methods: list[EventStudyMethod] = Field(
        default_factory=lambda: [EventStudyMethod.saturated, EventStudyMethod.did2s]
    )
    run_pretrend_test: bool = True
    placebo_methods: list[PlaceboMethod] = Field(
        default_factory=lambda: [PlaceboMethod.random_group]
    )
    placebo_estimator: EventStudyMethod = EventStudyMethod.did2s
    repetitions: Literal[100, 500, 1000, 2000] = 500
    random_seed: int = 12345
    show_density: bool = True
    labels: FigureLabels = Field(default_factory=FigureLabels)

    @model_validator(mode="after")
    def validate_diagnostic(self) -> DiagnosticSpec:
        if not self.event_methods and not self.placebo_methods:
            raise ValueError("Select at least one diagnostic method")
        if self.panel_id == self.time_variable:
            raise ValueError("Panel ID and time variable must differ")
        return self


class EventStudyPoint(BaseModel):
    event_time: int
    estimate: float | None
    std_error: float | None
    conf_low: float | None
    conf_high: float | None
    weight: float | None = None
    identifiable: bool = True


class EventStudyResult(BaseModel):
    method: EventStudyMethod
    points: list[EventStudyPoint]
    observations: int
    clusters: int
    pretrend_statistic: float | None = None
    pretrend_df: int | None = None
    pretrend_p_value: float | None = None
    warnings: list[str] = Field(default_factory=list)


class PlaceboDraw(BaseModel):
    iteration: int
    estimate: float


class PlaceboResult(BaseModel):
    method: PlaceboMethod
    estimator: EventStudyMethod
    actual_att: float
    draws: list[PlaceboDraw]
    mean: float
    std_dev: float
    quantile_low: float
    quantile_high: float
    empirical_p_value: float
    attempted: int
    failed: int
    warnings: list[str] = Field(default_factory=list)


class DiagnosticResult(BaseModel):
    job_id: str
    kind: Literal["event_study", "placebo"]
    spec: DiagnosticSpec
    dataset_revision: int
    model: ModelResult
    sample: dict[str, Any]
    event_studies: list[EventStudyResult] = Field(default_factory=list)
    placebos: list[PlaceboResult] = Field(default_factory=list)
    completed_at: str


class DiagnosticProgress(BaseModel):
    job_id: str
    kind: Literal["event_study", "placebo"]
    status: JobStatus
    stage: str = ""
    method: str = ""
    total: int = 0
    completed: int = 0
    successful: int = 0
    failed: int = 0
    elapsed_seconds: float = 0
    message: str = ""


class ActiveCacheLease(BaseModel):
    dataset_id: str | None = None
    analysis_job_ids: list[str] = Field(default_factory=list)
    diagnostic_job_ids: list[str] = Field(default_factory=list)
