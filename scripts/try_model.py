"""Explicit live smoke call for the selected provider."""
import json
import sys
from app.config import Settings
from app.llm import create_model, JsonlUsageSink, ModelError


def main():
    settings = Settings()
    try:
        with create_model(settings, JsonlUsageSink(settings.data_dir / "usage.jsonl")) as model:
            result = model.generate(
                messages=[{"role":"user", "content":"Return JSON with answer equal to ok."}],
                schema={"type":"object", "properties":{"answer":{"type":"string"}}, "required":["answer"], "additionalProperties":False},
                run_id="run_smoke", stage="brief")
        print(json.dumps({"data":result.data,"model":result.model,"generation_id":result.generation_id,"usage_ids":result.usage_ids},ensure_ascii=False))
    except ModelError as error:
        print(json.dumps({"code":error.code,"message":str(error),"retryable":error.retryable,"usage_ids":error.usage_ids}),file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
