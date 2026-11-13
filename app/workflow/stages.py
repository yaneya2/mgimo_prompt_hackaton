"""Stable seam for participant 2's synchronous stage functions."""
from dataclasses import dataclass
from importlib import import_module
from inspect import iscoroutinefunction
from typing import Callable, Protocol

from app.llm.usage import UsageSink

STAGES = ("brief", "research", "options", "communications", "review", "selection", "report")
AGENT_STAGES = tuple(stage for stage in STAGES if stage != "selection")


@dataclass(frozen=True)
class StageContext:
    inputs: list[dict]
    feedback: list[str]
    repairs_used: int
    synthetic: bool
    excluded_sources: tuple[dict, ...] = ()


class StageHandler(Protocol):
    def __call__(self, request: dict, context: StageContext) -> dict: ...


@dataclass(frozen=True)
class StageSpec:
    handler: StageHandler
    prompt_version: str
    model_id: str


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

    def manifest(self):
        return dict(name=self.name, synthetic=self.synthetic,
                    stages={stage: dict(prompt_version=spec.prompt_version, model_id=spec.model_id)
                            for stage, spec in self.stages.items()})


@dataclass(frozen=True)
class RecordingSink:
    """Pass only the recording operation to a stage factory, not the storage API."""
    callback: Callable[[dict], None]

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
