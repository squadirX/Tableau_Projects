#!/usr/bin/env python3
"""WCCM Windows Security authentication collector. Python 3 stdlib only."""
from __future__ import annotations
import argparse, fcntl, json, os, pathlib, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path.home() / "Documents/Personal_Network_Device_Command_Center/app/WCCM"
CONFIG_CANDIDATES = [
    ROOT / "P2_Health_Status_Prototype/mac/config.json",
    ROOT / "P4_Application_Control_Prototype/mac/config.json",
    ROOT / "config.json",
]
STATE_DIR = pathlib.Path.home() / "Library/Application Support/WCCM"
STATE_FILE = STATE_DIR / "windows_security_collector_state.json"
LOG_DIR = pathlib.Path.home() / "Library/Logs"
LOG_FILE = LOG_DIR / "WCCM_Windows_Security_Collector.log"
LOCK_FILE = STATE_DIR / "windows_security_collector.lock"
SIEM_URL = "http://127.0.0.1:18821/api/ingest"
EXPECTED_DEVICE = "DESKTOP-8U2AFU4"
EXPECTED_IDS = {4624, 4625}

def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        os.chmod(LOG_FILE, 0o600)
        f.write(f"{stamp} {message}\n")

def load_config():
    override = os.environ.get("WCCM_AGENT_CONFIG")
    paths = [pathlib.Path(override).expanduser()] if override else CONFIG_CANDIDATES
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            host = data.get("host") or data.get("agent_host") or data.get("windows_host") or "192.168.2.162"
            port = int(data.get("port") or data.get("agent_port") or 8765)
            token = data.get("token") or data.get("auth_token") or data.get("bearer_token")
            if isinstance(token, str) and token.strip():
                return str(host), port, token.strip(), str(path)
        except (OSError, ValueError, TypeError):
            continue
    raise RuntimeError("No readable WCCM Agent config with a bearer token was found.")

def load_state():
    try:
        obj = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "last_record_id": max(0, int(obj.get("last_record_id", 0))),
            "last_timestamp_utc": obj.get("last_timestamp_utc"),
            "last_successful_poll": obj.get("last_successful_poll"),
            "polls": int(obj.get("polls", 0)),
            "events_imported": int(obj.get("events_imported", 0)),
            "events_duplicate": int(obj.get("events_duplicate", 0)),
        }
    except (OSError, ValueError, TypeError):
        return {"last_record_id": 0, "last_timestamp_utc": None, "last_successful_poll": None,
                "polls": 0, "events_imported": 0, "events_duplicate": 0}

def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)

def request_json(url, token=None, payload=None, timeout=20):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1024).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:240]}") from None

def validate_event(e):
    try:
        return (
            str(e.get("device", "")).upper() == EXPECTED_DEVICE
            and str(e.get("channel", "")).lower() == "security"
            and int(e.get("event_id", 0)) in EXPECTED_IDS
            and str(e.get("category", "")).lower() == "authentication"
            and str(e.get("source", "")).lower() == "windows event log"
            and str(e.get("provenance", "")).lower() == "real_source"
            and e.get("is_synthetic") is False
            and e.get("includes_test_data") is False
            and int(e.get("record_id", 0)) > 0
            and bool(e.get("timestamp") or e.get("timestamp_utc"))
        )
    except (ValueError, TypeError, AttributeError):
        return False

def ingest_confirmed(result, count):
    obj = result.get("result") if isinstance(result.get("result"), dict) else result
    imported = obj.get("imported")
    duplicates = obj.get("duplicates")
    skipped = obj.get("skipped", 0)
    try:
        return int(imported) + int(duplicates) == count and int(skipped) == 0
    except (TypeError, ValueError):
        return False

def once(replay=False):
    host, port, token, config_path = load_config()
    state = load_state()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")
    if state["last_timestamp_utc"] and not replay:
        since = state["last_timestamp_utc"]
    after_id = 0 if replay else state["last_record_id"]
    query = urllib.parse.urlencode({"since_utc": since, "after_record_id": after_id})
    url = f"http://{host}:{port}/api/security/auth-events?{query}"
    status, payload = request_json(url, token=token)
    if status != 200 or payload.get("status") != "OK" or not isinstance(payload.get("events"), list):
        raise RuntimeError("Windows Agent returned an unexpected event response.")
    events = [e for e in payload["events"] if isinstance(e, dict) and validate_event(e)]
    if len(events) != len(payload["events"]):
        raise RuntimeError("Strict event validation rejected one or more records; checkpoint not advanced.")
    if not events:
        state["last_successful_poll"] = datetime.now(timezone.utc).isoformat()
        state["polls"] += 1
        save_state(state)
        log("poll=OK events=0 checkpoint_unchanged=1")
        return
    if replay:
        # Replay exactly the fetched batch to exercise SIEM native dedup; do not change checkpoint.
        pass
    status, result = request_json(SIEM_URL, payload={"source": "WCCM Windows Security Event Log", "events": events})
    if status != 200 or not ingest_confirmed(result, len(events)):
        raise RuntimeError("SIEM ingest acknowledgement did not confirm imported + duplicates = batch and skipped = 0; checkpoint not advanced.")
    if replay:
        log(f"replay=OK events={len(events)} siem_ack=confirmed checkpoint_unchanged=1")
        return
    max_id = max(int(e["record_id"]) for e in events)
    newest = max(str(e.get("timestamp_utc") or e.get("timestamp")) for e in events)
    state["last_record_id"] = max(state["last_record_id"], max_id)
    state["last_timestamp_utc"] = newest
    state["last_successful_poll"] = datetime.now(timezone.utc).isoformat()
    state["polls"] += 1
    state["events_imported"] += int(result.get("imported", result.get("result", {}).get("imported", 0)))
    state["events_duplicate"] += int(result.get("duplicates", result.get("result", {}).get("duplicates", 0)))
    save_state(state)
    log(f"poll=OK events={len(events)} siem_ack=confirmed checkpoint_record_id={max_id}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--replay", action="store_true", help="Re-fetch and repost the current 24h batch without advancing checkpoint.")
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with LOCK_FILE.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("poll=SKIPPED reason=collector_already_running")
            return 0
        try:
            if args.once or args.replay:
                once(replay=args.replay)
            else:
                while True:
                    try:
                        once()
                    except Exception as exc:
                        log("poll=ERROR reason=" + str(exc).replace("\n", " ")[:300])
                    time.sleep(60)
        except Exception as exc:
            log("poll=ERROR reason=" + str(exc).replace("\n", " ")[:300])
            print("Collector test failed; see local log (no credentials are logged).", file=sys.stderr)
            return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
