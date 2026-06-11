#!/usr/bin/env python3
"""
Novita AI image generation backend (Qwen image model).

Configuration keys:
  NOVITA_API_KEY        (required) — your Novita AI API key
  NOVITA_IMAGE_MODEL    (optional) — model name passed in the request body
  NOVITA_IMAGE_BASE_URL (optional) — override base URL (default: https://api.novita.ai/v3)

Uses Novita's async Qwen image endpoint:
  POST https://api.novita.ai/v3/async/qwen-image-txt2img   -> returns task_id
  GET  https://api.novita.ai/v3/async/task-result?task_id=  -> polls for completion
"""

import sys

if __name__ == "__main__" and any(arg in {"-h", "--help", "help"} for arg in sys.argv[1:]):
    print(__doc__)
    print("Use via: python3 skills/ppt-master/scripts/image_gen.py \"prompt\" --backend novita")
    raise SystemExit(0)

import os
import time

import requests

from image_backends.backend_common import (
    MAX_RETRIES,
    download_image,
    http_error,
    is_rate_limit_error,
    normalize_image_size,
    require_api_key,
    resolve_output_path,
    retry_delay,
)


DEFAULT_BASE_URL = "https://api.novita.ai/v3"

# Width x height for each (aspect_ratio, image_size) combination.
ASPECT_RATIO_SIZE_MAP = {
    "512px": {
        "1:1":  (1024, 1024),
        "16:9": (1280, 720),
        "9:16": (720,  1280),
        "3:2":  (1248, 832),
        "2:3":  (832,  1248),
        "4:3":  (1024, 768),
        "3:4":  (768,  1024),
        "4:5":  (896,  1120),
        "5:4":  (1120, 896),
        "21:9": (1344, 576),
    },
    "1K": {
        "1:1":  (1024, 1024),
        "16:9": (1280, 720),
        "9:16": (720,  1280),
        "3:2":  (1248, 832),
        "2:3":  (832,  1248),
        "4:3":  (1024, 768),
        "3:4":  (768,  1024),
        "4:5":  (896,  1120),
        "5:4":  (1120, 896),
        "21:9": (1344, 576),
    },
    "2K": {
        "1:1":  (2048, 2048),
        "16:9": (2048, 1152),
        "9:16": (1152, 2048),
        "3:2":  (2016, 1344),
        "2:3":  (1344, 2016),
        "4:3":  (1920, 1440),
        "3:4":  (1440, 1920),
        "4:5":  (1600, 2000),
        "5:4":  (2000, 1600),
        "21:9": (2560, 1088),
    },
    "4K": {
        "1:1":  (2048, 2048),
        "16:9": (2048, 1152),
        "9:16": (1152, 2048),
        "3:2":  (2016, 1344),
        "2:3":  (1344, 2016),
        "4:3":  (1920, 1440),
        "3:4":  (1440, 1920),
        "4:5":  (1600, 2000),
        "5:4":  (2000, 1600),
        "21:9": (2560, 1088),
    },
}

POLL_INTERVAL = 3   # seconds between task-result polls
POLL_TIMEOUT  = 300 # seconds before giving up


def _resolve_dims(aspect_ratio: str, image_size: str) -> tuple[int, int]:
    normalized = normalize_image_size(image_size)
    dims = (ASPECT_RATIO_SIZE_MAP.get(normalized) or {}).get(aspect_ratio)
    if not dims:
        supported = sorted(ASPECT_RATIO_SIZE_MAP["1K"])
        raise ValueError(
            f"Unsupported aspect ratio '{aspect_ratio}' for Novita backend. "
            f"Supported: {supported}"
        )
    return dims


def _poll_task(api_key: str, task_id: str, base_url: str) -> str:
    """Poll the task-result endpoint until the image URL is ready."""
    result_url = f"{base_url.rstrip('/')}/async/task-result"
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.time() + POLL_TIMEOUT
    dots = 0
    while time.time() < deadline:
        resp = requests.get(result_url, params={"task_id": task_id}, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise http_error(resp, "Novita task-result poll")
        data = resp.json()
        status = (data.get("task") or {}).get("status", "")
        if status == "TASK_STATUS_SUCCEED":
            images = (data.get("images") or [])
            if images:
                return images[0].get("image_url") or images[0].get("url") or ""
            raise RuntimeError(f"Novita task succeeded but no images in response: {data}")
        if status in ("TASK_STATUS_FAILED", "TASK_STATUS_CANCELED"):
            raise RuntimeError(f"Novita task {status}: {data}")
        # Still pending — print a dot and wait
        dots += 1
        if dots % 10 == 0:
            elapsed = int(time.time() - (deadline - POLL_TIMEOUT))
            print(f"\n  [..] Still waiting... ({elapsed}s)", end="", flush=True)
        else:
            print(".", end="", flush=True)
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Novita task {task_id} did not complete within {POLL_TIMEOUT}s")


def _generate_image(
    api_key: str,
    prompt: str,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    output_dir: str = None,
    filename: str = None,
    model: str = None,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    width, height = _resolve_dims(aspect_ratio, image_size)
    submit_url = f"{base_url.rstrip('/')}/async/qwen-image-txt2img"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "image_num": 1,
        "response_image_type": "png",
    }
    if model:
        payload["model_name"] = model

    print("[Novita AI — Qwen Image]")
    if model:
        print(f"  Model:        {model}")
    print(f"  Prompt:       {prompt[:120]}{'...' if len(prompt) > 120 else ''}")
    print(f"  Aspect Ratio: {aspect_ratio}")
    print(f"  Resolution:   {width}x{height}")
    print()
    print("  [..] Submitting task", end="", flush=True)

    start = time.time()
    resp = requests.post(submit_url, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise http_error(resp, "Novita qwen-image-txt2img submit")

    task_id = resp.json().get("task_id")
    if not task_id:
        raise RuntimeError(f"Novita submit response missing task_id: {resp.json()}")

    print(f" (task_id={task_id})")
    print("  [..] Polling for result", end="", flush=True)

    image_url = _poll_task(api_key, task_id, base_url)
    elapsed = time.time() - start
    print(f"\n  [DONE] Image ready ({elapsed:.1f}s)")

    path = resolve_output_path(prompt, output_dir, filename, ".png")
    return download_image(image_url, path)


def generate(
    prompt: str,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    output_dir: str = None,
    filename: str = None,
    model: str = None,
    max_retries: int = MAX_RETRIES,
) -> str:
    api_key = require_api_key(
        "NOVITA_API_KEY",
        message="No API key found. Set NOVITA_API_KEY in the environment or .env file.",
    )
    base_url = os.environ.get("NOVITA_IMAGE_BASE_URL") or DEFAULT_BASE_URL
    resolved_model = model or os.environ.get("NOVITA_IMAGE_MODEL") or None

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return _generate_image(
                api_key=api_key,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                output_dir=output_dir,
                filename=filename,
                model=resolved_model,
                base_url=base_url,
            )
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            limited = is_rate_limit_error(exc)
            delay = retry_delay(attempt, rate_limited=limited)
            label = "Rate limit hit" if limited else f"Error: {exc}"
            print(f"\n  [WARN] {label}. Retrying in {delay}s...")
            time.sleep(delay)

    raise RuntimeError(f"Failed after {max_retries + 1} attempts. Last error: {last_error}")
