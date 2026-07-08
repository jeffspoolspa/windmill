
import wmill
from ringcentral import SDK
import requests
import io
import time

RECORDING_ID = "2406983278051"

def main():
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("RC authenticated")

    rec_resp = platform.get(f"/restapi/v1.0/account/~/recording/{RECORDING_ID}/content")
    audio_bytes = rec_resp.response().content
    size_mb = len(audio_bytes) / (1024 * 1024)
    print(f"Downloaded recording {RECORDING_ID}: {size_mb:.2f} MB")

    if size_mb > 24.5:
        return {"recording_id": RECORDING_ID, "size_mb": round(size_mb, 2),
                "error": "File exceeds whisper-1 25MB limit; needs chunking."}

    openai_key = wmill.get_variable("u/carter/openai_api_key")
    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {openai_key}"},
        files={"file": ("rec.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
        data={
            "model": "whisper-1",
            "response_format": "verbose_json",
            "language": "en",
            "prompt": "Phone call with Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia. Office staff include Mary and Anna. Topics may include billing, credits, invoices, and pool service."
        },
        timeout=600
    )
    print(f"OpenAI status: {resp.status_code}")
    if resp.status_code != 200:
        return {"recording_id": RECORDING_ID, "size_mb": round(size_mb, 2),
                "error": f"OpenAI {resp.status_code}: {resp.text[:400]}"}

    j = resp.json()
    full_text = j.get("text", "")
    # Build a timestamped segment list so we can locate the credit discussion
    segments = []
    for s in j.get("segments", []):
        segments.append({
            "start": round(s.get("start", 0), 1),
            "end": round(s.get("end", 0), 1),
            "text": s.get("text", "").strip()
        })

    return {
        "recording_id": RECORDING_ID,
        "size_mb": round(size_mb, 2),
        "duration_sec": round(j.get("duration", 0), 1),
        "full_text": full_text,
        "segments": segments,
    }
