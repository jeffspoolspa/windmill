"""Signed download links for route-sheet photos in JPS Internal's private ion-outage bucket, so ionservice can fetch
each file without holding a key to the bucket (Carter 2026-10-30). Returns JSON text: [{path, url}]."""
import json

import requests
import wmill

BUCKET = "ion-outage"


def sign(base: str, key: str, paths: list[str], expires_in: int) -> list[dict]:
    resp = requests.post(f"{base}/storage/v1/object/sign/{BUCKET}", timeout=30,
                         headers={"Authorization": f"Bearer {key}", "apikey": key},
                         json={"expiresIn": expires_in, "paths": paths})
    resp.raise_for_status()
    signed = resp.json()
    missing = [s["path"] for s in signed if s.get("error") or not s.get("signedURL")]
    if missing:
        raise Exception(f"not signed: {missing}")
    return [{"path": s["path"], "url": f"{base}/storage/v1{s['signedURL']}"} for s in signed]


def main(paths: list[str], expires_in: int = 7 * 24 * 3600) -> str:  # storage refuses 30 days; 7 holds
    base = wmill.get_variable("f/SUPABASE/URL").rstrip("/")
    return json.dumps(sign(base, wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"), paths, expires_in))
