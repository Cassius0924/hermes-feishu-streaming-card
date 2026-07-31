from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from typing import Any

from .config import load_config
from .maintenance_card import FeishuJobPublisher
from .maintenance_store import MaintenanceRefused, load_job
from .maintenance_update import run_job
from .process import fetch_health
from .runner import NoopFeishuClient, build_feishu_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hfc-maintenance-runner")
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    try:
        job = load_job(Path(args.job), require_private=True)
        config = (
            load_config(job.config_path, env_file=job.env_file)
            if job.env_file is not None
            else load_config(job.config_path)
        )
        profile_config = _profile_config(config, job.profile_id)
        client = build_feishu_client(profile_config)
        if isinstance(client, NoopFeishuClient):
            raise MaintenanceRefused("Feishu credentials are unavailable")
        publisher = FeishuJobPublisher(client)
        result = run_job(
            job.path,
            fetch_health=lambda: fetch_health(profile_config),
            publish=lambda current: asyncio.run(publisher.publish(current)),
            maintenance_python=Path(sys.executable),
        )
    except (MaintenanceRefused, OSError, ValueError):
        return 1
    return 0 if result.phase == "succeeded" else 1


def _profile_config(
    config: dict[str, Any],
    profile_id: str,
) -> dict[str, Any]:
    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return config
    profile = profiles.get(profile_id)
    if not isinstance(profile, dict):
        raise MaintenanceRefused("maintenance profile is unavailable")
    merged = dict(config)
    for section, value in profile.items():
        if isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **value}
        else:
            merged[section] = value
    return merged


if __name__ == "__main__":
    raise SystemExit(main())
