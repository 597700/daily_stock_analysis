#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared utilities for daily_stock_analysis scripts.

Import this module from scripts in the same directory:
    from _shared import call_deepseek, load_env, setup_script_logging, ...

All functions are designed to work without depending on the main src/ package.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEEPSEEK_BASE = "https://api.deepseek.com/anthropic"

# ---------------------------------------------------------------------------
# Project / Environment
# ---------------------------------------------------------------------------

def get_project_root() -> Path:
    """Detect project root from the caller's __file__."""
    import inspect
    frame = inspect.currentframe()
    # Walk up to find the caller
    caller = frame.f_back
    while caller:
        fname = caller.f_globals.get("__file__", "")
        if fname and "importlib" not in fname:
            break
        caller = caller.f_back
    if caller and "__file__" in caller.f_globals:
        script_dir = Path(caller.f_globals["__file__"]).resolve().parent
    else:
        script_dir = Path.cwd()
    return script_dir.parent if script_dir.name == "scripts" else script_dir


def load_env(project_root: Path):
    """Load .env file into os.environ (only sets keys not already present)."""
    env_file = project_root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


def setup_script_logging(name: str, log_dir: Path) -> logging.Logger:
    """Configure logging with consistent format, returns logger instance."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8"),
        ],
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# DeepSeek API
# ---------------------------------------------------------------------------

def call_deepseek(
    prompt: str,
    api_key: str,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 8192,
    timeout: int = 120,
) -> Optional[dict]:
    """Call DeepSeek API (Anthropic-compatible endpoint). Returns parsed JSON dict or None."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    payload = {
        "model": "deepseek-chat",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(
            f"{DEEPSEEK_BASE}/v1/messages",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        if r.status_code != 200:
            logging.getLogger("_shared").error(
                "DeepSeek API returned %d: %s", r.status_code, r.text[:300]
            )
            return None

        data = r.json()
        content = data.get("content", [])
        if isinstance(content, list):
            text = "".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        if not text:
            logging.getLogger("_shared").error("DeepSeek returned empty content")
            return None

        # Extract JSON from response
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                if len(text) > 200:
                    logging.getLogger("_shared").warning(
                        "JSON parse failed, using raw text (%d chars)", len(text)
                    )
                    return {"title": "", "content": text}
                logging.getLogger("_shared").error(
                    "JSON parse failed: %s", text[:300]
                )
                return None

        # Check if DeepSeek returned polished markdown directly (no JSON wrapper)
        if len(text) > 200 and ("#" in text or "|" in text or "**" in text):
            logging.getLogger("_shared").warning(
                "No JSON wrapper, using polished text directly (%d chars)", len(text)
            )
            return {"title": "", "content": text}
        logging.getLogger("_shared").error("No JSON found in response: %s", text[:300])
        return None

    except requests.exceptions.Timeout:
        logging.getLogger("_shared").error("DeepSeek API timeout")
        return None
    except Exception as e:
        logging.getLogger("_shared").error("DeepSeek API error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Report Utilities
# ---------------------------------------------------------------------------

def find_today_report(
    reports_dir: Path,
    specified: Optional[str] = None,
    prefix: str = "market_review",
) -> Optional[Path]:
    """Find today's report file by prefix. Falls back to glob."""
    if specified:
        p = Path(specified)
        if p.exists():
            return p
        p = reports_dir / specified
        if p.exists():
            return p
        return None

    today = datetime.now().strftime("%Y%m%d")
    candidate = reports_dir / f"{prefix}_{today}.md"
    if candidate.exists():
        return candidate

    # Fallback: any file matching today's date
    matches = list(reports_dir.glob(f"*{today}*.md"))
    if matches:
        return max(matches, key=lambda x: x.stat().st_mtime)

    return None


def auto_publish(
    report_path: str,
    project_root: Path,
    report_type: str = "market_review",
    skip_polish: bool = False,
):
    """Run polish_report.py then wechat_mp_publish.py for a given report."""
    import subprocess

    polish_script = project_root / "scripts" / "polish_report.py"
    publish_script = project_root / "scripts" / "wechat_mp_publish.py"
    logger = logging.getLogger("_shared")

    # Step 1: Polish
    if not skip_polish and polish_script.exists():
        logger.info("Polishing (%s)...", report_type)
        try:
            result = subprocess.run(
                ["python3", str(polish_script), "--type", report_type],
                capture_output=True, text=True, timeout=120,
                cwd=str(project_root),
            )
            if result.returncode != 0:
                logger.warning("Polish failed (exit=%d), continuing with raw", result.returncode)
        except Exception as e:
            logger.warning("Polish exception: %s, continuing with raw", e)

    # Step 2: Publish
    if publish_script.exists():
        logger.info("Publishing...")
        try:
            result = subprocess.run(
                ["python3", str(publish_script), "--report", report_path],
                capture_output=True, text=True, timeout=120,
                cwd=str(project_root),
            )
            if result.returncode == 0:
                logger.info("Published OK")
            else:
                logger.warning("Publish failed (exit=%d): %s", result.returncode, result.stderr[-200:])
        except Exception as e:
            logger.warning("Publish exception: %s", e)
