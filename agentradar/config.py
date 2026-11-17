"""Local configuration. Secrets never become report data."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def load_config(root: Path = ROOT) -> dict:
    config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    local_path = root / "config.local.json"
    if local_path.exists():
        local = json.loads(local_path.read_text(encoding="utf-8-sig"))
        config["llm"].update(local.get("llm", {}))
    env = {**os.environ, **{k: v for k, v in dotenv_values(root / ".env").items() if v}}
    for key, name in (("base_url", "RADAR_BASE_URL"), ("model", "RADAR_MODEL"), ("api_key", "RADAR_API_KEY")):
        value = env.get(name)
        if value:
            config["llm"][key] = value
    config["github_token"] = env.get("GITHUB_TOKEN", "")
    config["llm"].setdefault("api_key", "")
    validate_endpoint(config["llm"].get("base_url", ""))
    return config


def validate_endpoint(url: str) -> None:
    if not url:
        return
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接口地址不能包含凭据、查询参数或片段")
    if not parsed.hostname or (parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )):
        raise ValueError("模型接口须使用 HTTPS；仅本机接口允许 HTTP")


def model_ready(config: dict) -> bool:
    llm = config["llm"]
    return bool(llm.get("base_url") and llm.get("model") and llm.get("api_key"))
