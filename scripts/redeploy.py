"""Out-of-band ops script: render + redeploy the tradingagents stack (67).

The repo `docker-compose.yml` is the source of truth. This script first RENDERS
the deploy payload from it (scripts/render_stack_payload.py — substitutes secrets
from the gitignored `.env`), then PUTs that payload to Portainer. So compose
changes (new services, new env vars) take effect, and secrets are never wiped
(they're inline literals in the rendered StackFileContent; Portainer Env[] stays
empty).

Flow:
  0. Render payload from docker-compose.yml + .env  -> STACK_PAYLOAD_OUT.
  1. Pull every first-party tradingagents image referenced by the rendered payload (repo+tag derived from the payload, not hardcoded).
  2. PUT the rendered payload to the stack.
  3. Recreate any container still on an older image id (a PUT alone often won't
     recreate just because :latest moved).
  4. Print final container state.

Credentials come from the environment — NEVER hardcode a Portainer token in a
committed file. Run e.g.:

    $env:PORTAINER_TOKEN="ptr_..."; python scripts/redeploy.py   # PowerShell
    PORTAINER_TOKEN=ptr_... python scripts/redeploy.py            # bash

Optional overrides: PORTAINER_URL, PORTAINER_ENDPOINT_ID, TRADINGAGENTS_STACK,
STACK_PAYLOAD_OUT.
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

BASE = os.environ.get("PORTAINER_URL", "https://192.168.7.50:9443").rstrip("/")
TOKEN = os.environ.get("PORTAINER_TOKEN")  # pragma: allowlist secret
ENDPOINT_ID = int(os.environ.get("PORTAINER_ENDPOINT_ID", "3"))
STACK_NAME = os.environ.get("TRADINGAGENTS_STACK", "tradingagents")
PAYLOAD_OUT = os.environ.get("STACK_PAYLOAD_OUT", r"C:\tmp\stack67_payload.json")
RENDER = Path(__file__).resolve().parent / "render_stack_payload.py"


def _images_from_payload(payload: dict) -> list[tuple[str, str]]:
    """Derive (repo, tag) pairs to pull/recreate from a rendered stack
    payload's StackFileContent (the compose YAML Portainer will receive).

    Parses every service's `image:` value, keeps only first-party
    tradingagents images (prefix match — excludes services riding along in
    the same compose file like switchboard/ollama), and de-duplicates while
    preserving first-seen order so a :latest-tagged payload derives exactly
    the historical hardcoded (tradingagents, tradingagents-web) pair, in the
    same order. Tag-aware in general: it derives the repo and tag actually
    specified by each service's `image:` field rather than assuming ":latest".
    """
    prefix = "ghcr.io/jemplayer82/tradingagents"
    compose = yaml.safe_load(payload["StackFileContent"]) or {}
    seen: list[tuple[str, str]] = []
    for svc in (compose.get("services") or {}).values():
        image = (svc or {}).get("image") or ""
        if not image.startswith(prefix):
            continue
        repo, sep, tag = image.rpartition(":")
        if not sep:
            repo, tag = image, "latest"
        pair = (repo, tag)
        if pair not in seen:
            seen.append(pair)
    return seen


def _repo_tag_from_image(image: str) -> tuple[str, str]:
    """Parse an image reference into (repo, tag) using the same rule as
    _images_from_payload: split on the last colon, defaulting to 'latest'
    when there is no colon.
    """
    repo, sep, tag = image.rpartition(":")
    if not sep:
        repo, tag = image, "latest"
    return (repo, tag)


def _build_latest_ids(
    images: list[tuple[str, str]],
    fetch_info,
) -> dict[tuple[str, str], str]:
    """Return a (repo, tag) -> Docker image Id mapping for `images`.

    `fetch_info(repo, tag)` should return the Docker image JSON dict
    (including an ``Id`` key) for that exact image reference.
    """
    latest_ids: dict[tuple[str, str], str] = {}
    for repo, tag in images:
        info = fetch_info(repo, tag)
        latest_ids[(repo, tag)] = info["Id"]
    return latest_ids


def _container_wants_image_id(
    container: dict,
    latest_ids: dict[tuple[str, str], str],
) -> str | None:
    """Return the latest image Id for `container` if its exact (repo, tag)
    is known and its current ImageID differs; otherwise None.

    The container's own ``Image`` string is parsed with the same
    repo-tag rule used throughout this module, so two tags of the same
    repo are never conflated.
    """
    image = container.get("Image") or ""
    pair = _repo_tag_from_image(image)
    want = latest_ids.get(pair)
    if want and container.get("ImageID") != want:
        return want
    return None


if not TOKEN:
    sys.exit("PORTAINER_TOKEN is not set — export it and re-run (see module docstring).")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def req(method, path, body=None, timeout=180):
    r = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"X-API-Key": TOKEN, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(r, context=ctx, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None


def _blocking_scan(status: dict) -> dict | None:
    """Return the scan that a redeploy would kill.

    The status endpoint exposes an actively running scan plus a separate
    ``waiting`` list of scans parked in the shared-research wait
    (``running_wait_research``), the market-open wait or the allocation-slot
    wait. Both represent live, heartbeating workers whose container this
    redeploy would recreate and kill. Prefer the running scan if present,
    otherwise report the first waiting scan, otherwise None.
    """
    running = status.get("running")
    if running:
        return running
    waiting = status.get("waiting")
    if waiting:
        return waiting[0]
    return None


# -1. pre-flight: refuse to deploy over a LIVE scan. Every redeploy recreates
# the portfolio container, killing any in-flight worker — the scan then sits
# 'running' with a dead worker until the reaper flags it "abandoned" an hour
# later (bitten three times: S&P scan 17 on 07-18, options 35 + portfolio 11
# on 07-31). Override deliberately with REDEPLOY_FORCE=1.
def _scan_running() -> dict | None:
    tok = None
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("INTERNAL_API_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        print("pre-flight: no INTERNAL_API_TOKEN in .env — skipping scan check")
        return None
    for base in ("http://192.168.7.50:8080", "https://trading.txferguson.net"):
        try:
            r = urllib.request.Request(base + "/api/portfolio/status",
                                       headers={"X-Internal-Token": tok})
            with urllib.request.urlopen(r, context=ctx, timeout=10) as resp:
                status = json.loads(resp.read()) or {}
                return _blocking_scan(status)
        except Exception:
            continue
    print("pre-flight: status endpoint unreachable — proceeding (can't confirm idle)")
    return None


_running = _scan_running()
if _running and os.environ.get("REDEPLOY_FORCE") != "1":
    sys.exit(
        f"ABORT: a scan is RUNNING ({_running.get('scan_type')} #{_running.get('id')}, "
        f"kind {_running.get('kind')}) — deploying now would kill its worker and the "
        "reaper would flag it 'abandoned' an hour later. Wait for it to finish, or "
        "rerun with REDEPLOY_FORCE=1 if you accept killing it."
    )

# 0. render payload from the repo compose (+ .env secrets)
print("rendering payload from docker-compose.yml ...")
proc = subprocess.run([sys.executable, str(RENDER)], env={**os.environ, "STACK_PAYLOAD_OUT": PAYLOAD_OUT})
if proc.returncode != 0:
    sys.exit("render failed — aborting deploy.")
payload = json.loads(Path(PAYLOAD_OUT).read_text(encoding="utf-8"))
IMAGES = _images_from_payload(payload)
if not IMAGES:
    sys.exit(
        "ABORT: zero first-party tradingagents images were found in the rendered "
        "payload's StackFileContent. Expected at least one service image starting with "
        "'ghcr.io/jemplayer82/tradingagents'. The render likely doesn't match the "
        "expected image prefix — investigate before deploying. No images pulled, "
        "no PUT, and no containers recreated."
    )

# 1. pull both images (public)
for repo, tag in IMAGES:
    req("POST", f"/api/endpoints/{ENDPOINT_ID}/docker/images/create?fromImage={repo}&tag={tag}")
    print(f"pulled {repo}:{tag}")

# 2. locate stack + PUT the rendered payload (secrets are inline literals)
stacks = req("GET", "/api/stacks")
stack_id = next(s["Id"] for s in stacks if s["Name"] == STACK_NAME)
print(f"stack id: {stack_id}")
result = req("PUT", f"/api/stacks/{stack_id}?endpointId={ENDPOINT_ID}", payload)
print(f"redeployed: {result.get('Name')} status={result.get('Status')}")


def _fetch_image_info(repo: str, tag: str) -> dict:
    return req(
        "GET",
        f"/api/endpoints/{ENDPOINT_ID}/docker/images/{urllib.request.quote(f'{repo}:{tag}', safe='')}/json",
    )


# 3. recreate any container still on an older image id
latest_ids = _build_latest_ids(IMAGES, _fetch_image_info)

filters = urllib.request.quote(
    json.dumps({"label": [f"com.docker.compose.project={STACK_NAME}"]})
)
containers = req(
    "GET",
    f"/api/endpoints/{ENDPOINT_ID}/docker/containers/json?all=true&filters={filters}",
)
for c in containers:
    name = c["Names"][0].lstrip("/")
    want = _container_wants_image_id(c, latest_ids)
    if want:
        print(f"{name} on {c['ImageID'][:19]}, latest {want[:19]} -> recreating")
        req("POST", f"/api/docker/{ENDPOINT_ID}/containers/{c['Id']}/recreate", {"PullImage": False})
        print("  recreated")
    else:
        print(f"{name} up to date")

# 4. show final container state
time.sleep(4)
containers = req(
    "GET",
    f"/api/endpoints/{ENDPOINT_ID}/docker/containers/json?all=true&filters={filters}",
)
print("\n--- stack containers ---")
for c in sorted(containers, key=lambda x: x["Names"][0]):
    print(f"  {c['Names'][0].lstrip('/'):32} {c.get('State',''):9} {c.get('Status','')}")
