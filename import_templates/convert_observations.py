#!/usr/bin/env python3
"""Convert observations_import.csv → FHIR Observation JSON, optionally
POSTing to a CDR's /api/v1/ingest/batch endpoint.

Usage:
  # Just print FHIR JSON to stdout (preview)
  python3 convert_observations.py observations_import.csv

  # POST to a CDR (service-key auth: requires X-Source-Service +
  # X-Service-Key headers — see CDR_app/app/api/auth.py KNOWN_FHIR_SERVICES)
  python3 convert_observations.py observations_import.csv \
      --target https://cdr2.pdhc.se \
      --source manual.pdhc \
      --key   "$MANUAL_PDHC_SERVICE_KEY"

The script batches at 100 rows/request (CDR's documented limit). Status
codes per batch are printed; non-2xx triggers a non-zero exit.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Iterator

OBS_CAT_SYSTEM = "http://terminology.hl7.org/CodeSystem/observation-category"
UCUM = "http://unitsofmeasure.org"
BATCH_SIZE = 100


def row_to_observation(row: dict) -> dict:
    obs: dict = {
        "resourceType": "Observation",
        "status": (row.get("status") or "final").strip(),
        "code": {
            "coding": [{
                "system": (row.get("code_system") or "").strip(),
                "code":   (row.get("code") or "").strip(),
                **({"display": row["code_display"].strip()}
                   if row.get("code_display") else {}),
            }],
        },
        "subject": {"reference": f"Patient/{(row.get('patient_guid') or '').strip()}"},
        "effectiveDateTime": (row.get("effective_datetime") or "").strip(),
    }
    cat = (row.get("category") or "").strip()
    if cat:
        obs["category"] = [{"coding": [{"system": OBS_CAT_SYSTEM, "code": cat}]}]
    val = (row.get("value_quantity") or "").strip()
    unit = (row.get("value_unit") or "").strip()
    if val:
        try:
            v = float(val)
        except ValueError:
            v = None
        if v is not None:
            obs["valueQuantity"] = {
                "value": v,
                **({"unit": unit, "system": UCUM, "code": unit} if unit else {}),
            }
    note = (row.get("note") or "").strip()
    if note:
        obs["note"] = [{"text": note}]
    src = (row.get("source_service") or "").strip()
    if src:
        obs.setdefault("meta", {}).setdefault("tag", []).append({
            "system": "https://pdhc.se/source-service",
            "code": src,
        })
    return obs


def read_rows(path: str) -> Iterator[dict]:
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield row


def post_batch(target: str, source: str, key: str, batch: list[dict]) -> int:
    import requests
    url = target.rstrip("/") + "/api/v1/ingest/batch"
    r = requests.post(
        url,
        json={"resources": batch},
        headers={
            "Content-Type": "application/json",
            "X-Source-Service": source,
            "X-Service-Key": key,
        },
        timeout=30,
    )
    print(f"  POST {url}  →  {r.status_code}  ({len(batch)} rows)")
    if r.status_code >= 400:
        print(f"    body: {r.text[:300]}")
    return r.status_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", help="Input CSV (see observations_import.csv template)")
    ap.add_argument("--target", help="CDR base URL, e.g. https://cdr2.pdhc.se. "
                                     "If omitted, just prints JSON to stdout.")
    ap.add_argument("--source", default="manual.pdhc",
                    help="X-Source-Service (default: manual.pdhc).")
    ap.add_argument("--key", default="",
                    help="X-Service-Key. Required if --target is given.")
    args = ap.parse_args()

    rows = list(read_rows(args.csv))
    obs = [row_to_observation(r) for r in rows]
    print(f"Parsed {len(rows)} rows.")

    if not args.target:
        print(json.dumps(obs, indent=2))
        return 0

    if not args.key:
        print("ERROR: --key is required when --target is set.", file=sys.stderr)
        return 2

    fail = 0
    for i in range(0, len(obs), BATCH_SIZE):
        batch = obs[i:i + BATCH_SIZE]
        if post_batch(args.target, args.source, args.key, batch) >= 400:
            fail += 1
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
