# requirements:
# wmill
# requests
import wmill, requests
KEY = wmill.get_variable("f/service_billing/OPENAI_API_KEY")
SYS = ("You clean up a pool-service technician's raw voice note into concise, "
 "readable field notes. Fix grammar, punctuation, and obviously mis-heard "
 "pool-industry terms. Keep it factual and in the tech's own voice. Do NOT "
 "add information that isn't in the note. Return only the cleaned note text.")
def main(examples: list = []):
    out = []
    for raw in examples:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "temperature": 0.2,
                  "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": raw}]},
            timeout=30)
        out.append({"raw": raw, "cleaned": (r.json()["choices"][0]["message"]["content"].strip()
                    if r.ok else f"ERR {r.status_code}: {r.text[:150]}")})
    return out
