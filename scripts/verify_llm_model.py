"""Verify whether a model name is available on the configured LLM endpoint.

Usage:
  python scripts/verify_llm_model.py [model_name]

If model_name is omitted, uses llm.model from config.json.

The script queries the LLM provider's /v1/models endpoint
and reports whether the model is in the list.

Env var OLLAMA_API_KEY is required for ollama.com / Anthropic
endpoints. Local daemon at http://localhost:11434 ignores the key.
"""
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_config_model() -> str | None:
    cfg_path = PROJECT_ROOT / "config.json"
    if not cfg_path.exists():
        return None
    try:
        with cfg_path.open() as f:
            data = json.load(f)
        return data.get("news", {}).get("llm", {}).get("model")
    except (OSError, ValueError):
        return None


def _fetch_models(host: str, api_key: str | None, timeout: int = 10) -> list[str]:
    base = host.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/v1/models"
    req = Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    elif api_key is None and "anthropic" in host:
        key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
            "CLAUDE_API_KEY"
        )
        if key:
            req.add_header("x-api-key", key)
            req.add_header("anthropic-version", "2023-06-01")
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (HTTPError, URLError) as e:
        print(f"  ✗ network error: {e}")
        return []
    if "data" in data:
        return [m.get("id", "") for m in data["data"]]
    return []


def main() -> int:
    cfg_path = PROJECT_ROOT / "config.json"
    model_arg = sys.argv[1] if len(sys.argv) > 1 else None
    model = model_arg or _load_config_model()

    if not model:
        print(f"Usage: {sys.argv[0]} <model_name>")
        print(f"  Or set news.llm.model in {cfg_path}")
        return 1

    if not cfg_path.exists():
        print(f"✗ {cfg_path} not found")
        return 1
    with cfg_path.open() as f:
        data = json.load(f)
    cfg = data.get("news", {}).get("llm", {})
    host = cfg.get("host", "http://localhost:11434")
    api_key = cfg.get("api_key") or os.environ.get("OLLAMA_API_KEY")

    print(f"Checking model: {model!r}")
    print(f"  endpoint:    {host}")
    print(f"  api_key:     {'<set>' if api_key else '<not set>'}")

    models = _fetch_models(host, api_key)
    if not models:
        print(f"  ✗ could not reach endpoint (auth issue or daemon down)")
        return 2

    is_present = any(model in m for m in models)
    print(f"\n  Result: {model!r} is {'AVAILABLE' if is_present else 'NOT FOUND'}")
    print(f"  Total models at {host}: {len(models)}")
    if not is_present:
        # Suggest similar names
        similar = [m for m in models
                   if any(part in m for part in model.split(":")[0].split("-"))]
        if similar:
            print(f"  Similar models: {similar[:5]}")
    return 0 if is_present else 1


if __name__ == "__main__":
    sys.exit(main())