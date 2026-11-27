"""Configuration, performance presets and persistence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields
from typing import Any, Dict, Optional

PRESETS: Dict[str, Dict[str, Any]] = {
    "low": {
        "concurrency": 50,
        "per_proxy_concurrency": 5,
        "checkpoint_interval": 2.0,
        "connect_timeout": 15.0,
        "request_timeout": 25.0,
        "max_body_bytes": 262144,
        "dashboard_fps": 2.0,
    },
    "balanced": {
        "concurrency": 200,
        "per_proxy_concurrency": 10,
        "checkpoint_interval": 1.5,
        "connect_timeout": 12.0,
        "request_timeout": 20.0,
        "max_body_bytes": 262144,
        "dashboard_fps": 4.0,
    },
    "high": {
        "concurrency": 500,
        "per_proxy_concurrency": 20,
        "checkpoint_interval": 1.0,
        "connect_timeout": 10.0,
        "request_timeout": 15.0,
        "max_body_bytes": 131072,
        "dashboard_fps": 6.0,
    },
    "extreme": {
        "concurrency": 1500,
        "per_proxy_concurrency": 50,
        "checkpoint_interval": 1.0,
        "connect_timeout": 8.0,
        "request_timeout": 12.0,
        "max_body_bytes": 65536,
        "dashboard_fps": 8.0,
    },
}


@dataclass
class Settings:
    # checking
    mode: str = "simple"  # simple | advanced
    concurrency: int = 200
    per_proxy_concurrency: int = 10
    max_redirects: int = 10
    connect_timeout: float = 12.0
    request_timeout: float = 20.0
    head_first: bool = True
    max_body_bytes: int = 262144
    retries: int = 3  # retries through alternative proxies after proxy-side failure
    verify_tls: bool = True
    https_fallback_http: bool = True  # only for genuine TLS failures

    # input / dedup
    dedup: bool = True
    prefer_https: bool = True

    # session
    checkpoint_interval: float = 1.5
    output_dir: str = "output"

    # proxy engine
    proxy_health_threshold: int = 3  # consecutive proxy-side failures before quarantine
    proxy_quarantine_seconds: float = 120.0
    proxy_check_timeout: float = 10.0

    # ui
    dashboard_fps: float = 4.0
    color: bool = True

    # advanced
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    proxy_file: Optional[str] = None
    input_file: Optional[str] = None
    preset: str = "balanced"

    def apply_preset(self, name: str) -> None:
        name = name.strip().lower()
        if name not in PRESETS:
            return
        self.preset = name
        for key, value in PRESETS[name].items():
            setattr(self, key, value)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Settings":
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "Settings":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(fh.read())

    @staticmethod
    def available_presets() -> Dict[str, Dict[str, Any]]:
        return dict(PRESETS)
