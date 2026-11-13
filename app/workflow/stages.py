"""Stable seam for participant 2's synchronous stage functions."""
from dataclasses import dataclass
from importlib import import_module
from inspect import iscoroutinefunction
from typing import Callable, Protocol

from app.llm.usage import UsageSink
from app.execution import before_external_action

STAGES = ("brief", "research", "options", "communications", "review", "selection", "report")
AGENT_STAGES = tuple(stage for stage in STAGES if stage != "selection")


@dataclass(frozen=True)
class StageContext:
    inputs: list[dict]
    feedback: list[str]
    repairs_used: int
    synthetic: bool
    excluded_sources: tuple[dict, ...] = ()

    def check_pause(self):
        """Tools must call this before every external action; never swallow PauseRequested."""
        before_external_action()

    def tool_call(self, **kwargs):
        """Invoke a transport through shared reservations, retries, cache and Usage."""
        from app.tools.control import tool_call
        return tool_call(**kwargs)


class StageHandler(Protocol):
    def __call__(self, request: dict, context: StageContext) -> dict: ...


@dataclass(frozen=True)
class StageSpec:
    handler: StageHandler
    prompt_version: str
    model_id: str
    reuse_brief_fields: tuple[str, ...] | None = None
    reuse_collections: tuple[str, ...] | None = None
    reuse_max_age_seconds: int = 3600


@dataclass(frozen=True)
class StageRegistry:
    name: str
    stages: dict[str, StageSpec]
    synthetic: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not isinstance(self.stages, dict) or set(self.stages) != set(AGENT_STAGES):
            raise ValueError("A registry must provide all six agent stages; selection is owned by the platform")
        if type(self.synthetic) is not bool or any(not isinstance(spec, StageSpec) or not callable(spec.handler)
               or iscoroutinefunction(spec.handler) or not isinstance(spec.prompt_version, str) or not spec.prompt_version.strip()
               or not isinstance(spec.model_id, str) or not spec.model_id.strip() for spec in self.stages.values()):
            raise ValueError("Stage metadata and a callable handler are required")
        from app.storage.validation import COLLECTIONS, validator
        fields = set(validator('BriefInput').schema['$defs']['BriefInput']['properties'])
        for stage, spec in self.stages.items():
            values = (spec.reuse_brief_fields, spec.reuse_collections)
            if values == (None, None):
                continue
            if (stage in ('brief', 'report') or any(not isinstance(value, tuple) for value in values)
                    or type(spec.reuse_max_age_seconds) is not int or not 0 < spec.reuse_max_age_seconds <= 86400
                    or not set(spec.reuse_brief_fields) <= fields
                    or not set(spec.reuse_collections) <= set(COLLECTIONS.values())):
                raise ValueError('Reuse requires explicit valid dependencies for a reusable agent stage')

    def manifest(self):
        return dict(name=self.name, synthetic=self.synthetic,
                    stages={stage: dict(prompt_version=spec.prompt_version, model_id=spec.model_id,
                                       **({'reuse_brief_fields': list(spec.reuse_brief_fields),
                                           'reuse_collections': list(spec.reuse_collections),
                                           'reuse_max_age_seconds': spec.reuse_max_age_seconds}
                                          if spec.reuse_brief_fields is not None else {}))
                            for stage, spec in self.stages.items()})

    def accepts_manifest(self, saved):
        current = self.manifest()
        if saved == current:
            return True
        # A5/A6 manifests predate reuse declarations. Existing results have no reuse key.
        upgraded = {**saved, 'stages': {stage: dict(spec) for stage, spec in saved.get('stages', {}).items()}}
        for stage, spec in upgraded['stages'].items():
            if 'reuse_brief_fields' not in spec and 'reuse_collections' not in spec and stage in current['stages']:
                spec.update({key: value for key, value in current['stages'][stage].items() if key.startswith('reuse_')})
        return upgraded == current


@dataclass(frozen=True)
class RecordingSink:
    """Pass only the recording operation to a stage factory, not the storage API."""
    callback: Callable[[dict], None]
    requires_execution_control = True

    def record(self, event: dict) -> None:
        self.callback(event)


def load_registry(factory: str | None, settings, sink: UsageSink) -> StageRegistry:
    if factory == "demo":
        from app.workflow.demo import build_stages
        return build_stages(settings, sink)
    if not factory or ":" not in factory:
        raise ValueError("Set APP_STAGE_FACTORY=package.module:build_stages or explicitly use --stages demo")
    module, function = factory.split(":", 1)
    # This path is operator configuration, never supplied by an agent or external text.
    registry = getattr(import_module(module), function)(settings, sink)
    if not isinstance(registry, StageRegistry):
        raise ValueError("The stage factory must return StageRegistry")
    return registry
