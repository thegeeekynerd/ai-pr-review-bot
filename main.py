from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import os
import json
import hmac
import hashlib
import httpx

from dotenv import load_dotenv  # ✅ Add this
load_dotenv() 

app = FastAPI()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")

# AI prompt
AI_PR_REVIEW_PROMPT = """
You are a senior engineer. Review this GitHub pull request diff and provide:
1. Code quality feedback
2. Potential bugs
3. Security issues
4. Performance tips
5. Best practices

Be concise and constructive.
"""

# HMAC signature verification
def verify_signature(payload: bytes, signature: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        return True  # skip if not set
    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
    expected = f"sha256={mac.hexdigest()}"
    return hmac.compare_digest(expected, signature)

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)

    if event == "pull_request" and payload["action"] in ["opened", "synchronize"]:
        background_tasks.add_task(handle_pr, payload)

    return {"status": "ok"}

async def handle_pr(payload: dict):
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    pr_number = pr["number"]

    diff_url = pr["_links"].get("diff", {}).get("href")
    if not diff_url:
        diff_url = f"https://github.com/{repo}/pull/{pr_number}.diff"

    # Fetch diff
    diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    async with httpx.AsyncClient() as client:
        diff_resp = await client.get(diff_url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3.diff"
        })
        if diff_resp.status_code != 200:
            print(f"Failed to fetch diff: {diff_resp.status_code} - {diff_resp.text}")
            return
        diff = diff_resp.text
        print(f"Diff length: {len(diff)}")
        print(f"Diff preview:\n{diff[:500]}")


    # Call Groq
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": AI_PR_REVIEW_PROMPT},
                    {"role": "user", "content": diff}
                ],
                "max_tokens": 1000,
                "temperature": 0.3
            }
        )
        result = res.json()
        print("Groq raw response:", result)
        review = result["choices"][0]["message"]["content"]

    # Post comment on PR
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json"
            },
            json={"body": f"### 🤖 AI Code Review\n\n{review}"}
        )
