
import wmill
from ringcentral import SDK
import requests
import io
import time

RECORDINGS = [
    {"label": "COWSERT 6/3 outbound (pmt 6/3)", "recording_id": "2381875973050"},
    {"label": "DAKE 6/2 inbound main line (pmt 6/5)", "recording_id": "2381107951051"},
    {"label": "DAKE 6/4 outbound Zach Taylor (pmt 6/5)", "recording_id": "2383104080051"},
    {"label": "WAITES 6/4 outbound (pmt 6/9)", "recording_id": "2383060816051"},
    {"label": "GRAY 6/9 inbound (pmt 6/10)", "recording_id": "2386550143050"},
    {"label": "GRAY 6/10 outbound Mary Kidd (pmt 6/10)", "recording_id": "2386924678050"},
]

def transcribe_one(platform, openai_key, rec_id):
    rec_resp = platform.get(f"/restapi/v1.0/account/~/recording/{rec_id}/content")
    audio_bytes = rec_resp.response().content
    print(f"  downloaded {rec_id}: {len(audio_bytes)/1024:.0f} KB")

    r = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {openai_key}"},
        files={"file": (f"rec_{rec_id}.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
        data={
            "model": "gpt-4o-transcribe-diarize",
            "response_format": "diarized_json",
            "language": "en",
            "chunking_strategy": "auto"
        },
        timeout=600
    )
    if r.status_code == 200:
        result = r.json()
        segments = result.get('segments', [])
        current_speaker = None
        lines = []
        current_line = ""
        for seg in segments:
            speaker = seg.get('speaker', '?')
            text = seg.get('text', '').strip()
            if speaker != current_speaker:
                if current_line:
                    lines.append(current_line)
                current_line = f"Speaker {speaker}: {text}"
                current_speaker = speaker
            else:
                current_line += f" {text}"
        if current_line:
            lines.append(current_line)
        return {"diarized": True, "transcript": "\n\n".join(lines)}

    # fallback
    r = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {openai_key}"},
        files={"file": (f"rec_{rec_id}.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
        data={
            "model": "gpt-4o-transcribe",
            "response_format": "json",
            "language": "en",
            "prompt": "Phone call with Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia."
        },
        timeout=600
    )
    if r.status_code != 200:
        return {"error": f"{r.status_code}: {r.text[:300]}"}
    return {"diarized": False, "transcript": r.json().get('text', '')}

def main():
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    openai_key = wmill.get_variable("u/carter/openai_api_key")
    print("Authenticated; transcribing", len(RECORDINGS), "recordings")

    out = []
    for item in RECORDINGS:
        print(f"-> {item['label']}")
        try:
            res = transcribe_one(platform, openai_key, item['recording_id'])
        except Exception as e:
            res = {"error": str(e)[:300]}
        res['label'] = item['label']
        res['recording_id'] = item['recording_id']
        out.append(res)
        time.sleep(2)

    return out
