from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
from typing import Any

from .config import load_config
from .maintenance_card import FeishuJobPublisher
from .maintenance_store import (
    MaintenanceRefused,
    consume_job_credentials,
    discard_job_credentials,
    load_job,
    maintenance_paths,
    release_drain_lease,
    transition_job,
)
from .maintenance_update import run_job, set_gateway_external_drain
from .process import fetch_health
from .runner import NoopFeishuClient, build_feishu_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hfc-maintenance-runner")
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    job = None
    paths = None
    try:
        job = load_job(Path(args.job), require_private=True)
        paths = maintenance_paths(job.path.parent.parent)
        for key, value in consume_job_credentials(
            paths,
            job_id=job.job_id,
        ).items():
            os.environ[key] = value
        config = (
            load_config(job.config_path, env_file=job.env_file)
            if job.env_file is not None
            else load_config(job.config_path)
        )
        profile_config = _profile_config(config, job.profile_id)
        profile_config = _bot_profile_config(profile_config, job.bot_id)
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
        discard_job_credentials(paths, job_id=job.job_id)
    except (MaintenanceRefused, OSError, ValueError):
        if job is not None and paths is not None:
            try:
                current = load_job(job.path)
                if current.phase not in {"succeeded", "failed", "cancelled"}:
                    transition_job(
                        current.path,
                        expected_phase=current.phase,
                        phase="failed",
                        result={
                            "error_code": "runner_initialization_failed",
                            "recovery_boundary": "no_mutation",
                            "status": "failed",
                        },
                    )
            except Exception:
                pass
            try:
                release_drain_lease(paths, owner_id=job.job_id)
            except Exception:
                pass
            try:
                set_gateway_external_drain(job.hermes_root, active=False)
            except Exception:
                pass
            try:
                discard_job_credentials(paths, job_id=job.job_id)
            except Exception:
                pass
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


def _bot_profile_config(
    config: dict[str, Any],
    bot_id: str,
) -> dict[str, Any]:
    selected = str(bot_id or "default").strip()
    bots = config.get("bots")
    items = bots.get("items") if isinstance(bots, dict) else None
    bot = items.get(selected) if isinstance(items, dict) else None
    if bot is None and selected == "default":
        return config
    if not isinstance(bot, dict):
        raise MaintenanceRefused("maintenance bot is unavailable")
    app_id = str(bot.get("app_id") or "").strip()
    app_secret = str(bot.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise MaintenanceRefused("maintenance bot credentials are unavailable")
    merged = dict(config)
    feishu = dict(merged.get("feishu") or {})
    for key in ("app_id", "app_secret", "base_url", "timeout_seconds"):
        if key in bot:
            feishu[key] = bot[key]
    merged["feishu"] = feishu
    return merged


if __name__ == "__main__":
    raise SystemExit(main())
