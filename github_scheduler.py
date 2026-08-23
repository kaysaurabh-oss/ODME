from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, List

import requests

from runtime_config import get_secret

WORKFLOW_PATH = ".github/workflows/odme_scheduled.yml"
API_ROOT = "https://api.github.com"


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalise_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        hh, mm = [int(x) for x in text.split(":", 1)]
    except Exception:
        return ""
    if 0 <= hh <= 23 and 0 <= mm <= 59 and mm % 5 == 0:
        return f"{hh:02d}:{mm:02d}"
    return ""


def selected_scan_times(settings) -> List[str]:
    """Return unique enabled scan times from the persistent instrument settings."""
    times = set()
    if settings is None or getattr(settings, "empty", True):
        return []
    for _, row in settings.iterrows():
        if not _as_bool(row.get("active")) or not _as_bool(row.get("scan_enabled")):
            continue
        expiry = str(row.get("selected_expiry", "")).strip()
        if not expiry:
            continue
        for raw in str(row.get("scan_times", "") or "").split(","):
            t = _normalise_time(raw)
            if t:
                times.add(t)
    return sorted(times)


def scheduler_configured() -> bool:
    return bool(
        str(get_secret("GITHUB_REPO", "") or "").strip()
        and str(get_secret("GITHUB_SCHEDULE_TOKEN", "") or "").strip()
    )


def render_workflow(scan_times: Iterable[str]) -> str:
    """Render the GitHub Actions workflow using only the user's selected IST slots.

    This intentionally avoids 5-minute polling. On a private repository that would
    consume thousands of GitHub-hosted runner minutes per month. Each unique saved
    scan time creates only one scheduled workflow run per day.
    """
    times = sorted({_normalise_time(x) for x in scan_times if _normalise_time(x)})

    lines = [
        "name: ODME Scheduled Scan",
        "",
        "on:",
        "  workflow_dispatch:",
    ]
    if times:
        lines.append("  schedule:")
        for item in times:
            hh, mm = item.split(":")
            lines.extend([
                f"    - cron: '{int(mm)} {int(hh)} * * *'",
                '      timezone: "Asia/Kolkata"',
            ])

    lines.extend([
        "",
        "permissions:",
        "  contents: read",
        "",
        "concurrency:",
        "  group: odme-scheduled-scan",
        "  cancel-in-progress: false",
        "",
        "jobs:",
        "  scan:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 20",
        "    env:",
        "      ANGEL_API_KEY: ${{ secrets.ANGEL_API_KEY }}",
        "      ANGEL_CLIENT_ID: ${{ secrets.ANGEL_CLIENT_ID }}",
        "      ANGEL_PIN: ${{ secrets.ANGEL_PIN }}",
        "      ANGEL_TOTP_SECRET: ${{ secrets.ANGEL_TOTP_SECRET }}",
        "      GMAIL_SENDER: ${{ secrets.GMAIL_SENDER }}",
        "      GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}",
        "      ALERT_EMAILS: ${{ secrets.ALERT_EMAILS }}",
        '      USE_GOOGLE_SHEETS: "true"',
        "      GOOGLE_SHEET_NAME: ${{ secrets.GOOGLE_SHEET_NAME }}",
        "      GCP_SERVICE_ACCOUNT_JSON: ${{ secrets.GCP_SERVICE_ACCOUNT_JSON }}",
        "    steps:",
        "      - name: Checkout",
        "        uses: actions/checkout@v4",
        "",
        "      - name: Set up Python",
        "        uses: actions/setup-python@v5",
        "        with:",
        '          python-version: "3.11"',
        '          cache: "pip"',
        "          cache-dependency-path: requirements.txt",
        "",
        "      - name: Install dependencies",
        "        run: python -m pip install --disable-pip-version-check -r requirements.txt",
        "",
        "      - name: Run ODME scheduled worker",
        "        run: python scheduled_worker.py",
        "",
    ])
    return "\n".join(lines)


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ODME-Scheduler",
    }


def sync_schedule_from_store(store) -> Dict[str, Any]:
    """Synchronize selected scan times into the repository workflow file.

    Streamlit needs these top-level secrets:
      GITHUB_REPO = "owner/repository"
      GITHUB_SCHEDULE_TOKEN = fine-grained PAT with Contents: write + Workflows: write
      GITHUB_BRANCH = "main"  # optional
    """
    repo = str(get_secret("GITHUB_REPO", "") or "").strip().strip("/")
    token = str(get_secret("GITHUB_SCHEDULE_TOKEN", "") or "").strip()
    branch = str(get_secret("GITHUB_BRANCH", "main") or "main").strip() or "main"
    if not repo or "/" not in repo:
        raise RuntimeError("GITHUB_REPO is missing or invalid. Use owner/repository.")
    if not token:
        raise RuntimeError("GITHUB_SCHEDULE_TOKEN is not configured.")

    settings = store.list_instrument_settings(active_only=True)
    times = selected_scan_times(settings)
    content = render_workflow(times)

    url = f"{API_ROOT}/repos/{repo}/contents/{WORKFLOW_PATH}"
    headers = _github_headers(token)
    current_sha = None
    get_resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    if get_resp.status_code == 200:
        current = get_resp.json()
        current_sha = current.get("sha")
        try:
            existing = base64.b64decode(current.get("content", "")).decode("utf-8")
        except Exception:
            existing = ""
        if existing.strip() == content.strip():
            return {"changed": False, "times": times, "count": len(times), "branch": branch}
    elif get_resp.status_code != 404:
        raise RuntimeError(f"GitHub schedule read failed ({get_resp.status_code}): {get_resp.text[:300]}")

    payload: Dict[str, Any] = {
        "message": "Sync ODME scheduled scan times",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if current_sha:
        payload["sha"] = current_sha

    put_resp = requests.put(url, headers=headers, json=payload, timeout=30)
    if put_resp.status_code not in {200, 201}:
        raise RuntimeError(f"GitHub schedule update failed ({put_resp.status_code}): {put_resp.text[:400]}")

    return {"changed": True, "times": times, "count": len(times), "branch": branch}
