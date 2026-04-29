
import wmill
from ringcentral import SDK
import requests
import io

def main(recording_id: str):
    """
    Download a call recording from RC and transcribe with OpenAI diarization.
    
    Args:
        recording_id: The RingCentral recording ID (from call log)
    """
    # --- 1. Download recording from RC ---
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print(f"✓ RC authenticated")
    
    # Download the recording
    rec_resp = platform.get(f"/restapi/v1.0/account/~/recording/{recording_id}/content")
    audio_bytes = rec_resp.response().content
    size_kb = len(audio_bytes) / 1024
    print(f"✓ Downloaded recording {recording_id}: {size_kb:.1f} KB")
    
    # --- 2. Transcribe with OpenAI diarization ---
    openai_key = wmill.get_variable("u/carter/openai_api_key")
    
    print("Sending to OpenAI gpt-4o-transcribe-diarize...")
    openai_resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {openai_key}"},
        files={
            "file": (f"rec_{recording_id}.mp3", io.BytesIO(audio_bytes), "audio/mpeg")
        },
        data={
            "model": "gpt-4o-transcribe-diarize",
            "response_format": "diarized_json",
            "language": "en",
            "chunking_strategy": "auto"
        },
        timeout=600
    )
    
    if openai_resp.status_code != 200:
        # Fallback to non-diarized model
        print(f"Diarize model failed ({openai_resp.status_code}), falling back to gpt-4o-transcribe...")
        openai_resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {openai_key}"},
            files={
                "file": (f"rec_{recording_id}.mp3", io.BytesIO(audio_bytes), "audio/mpeg")
            },
            data={
                "model": "gpt-4o-transcribe",
                "response_format": "json",
                "language": "en",
                "prompt": "This is a phone call with a pool service company called Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia."
            },
            timeout=600
        )
        
        if openai_resp.status_code != 200:
            return {"error": openai_resp.status_code, "detail": openai_resp.text[:1000]}
        
        result = openai_resp.json()
        return {
            "recording_id": recording_id,
            "model": "gpt-4o-transcribe",
            "transcript": result.get('text', ''),
            "diarized": False
        }
    
    # Parse diarized result
    result = openai_resp.json()
    segments = result.get('segments', [])
    
    # Build clean readable transcript
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
    
    formatted = "\n\n".join(lines)
    speakers = list(set(seg.get('speaker') for seg in segments))
    
    print(f"✓ Transcribed — {len(segments)} segments, {len(speakers)} speakers: {speakers}")
    
    return {
        "recording_id": recording_id,
        "model": "gpt-4o-transcribe-diarize",
        "speakers": speakers,
        "formatted_transcript": formatted,
        "raw_text": result.get('text', ''),
        "segments": segments,
        "diarized": True
    }
