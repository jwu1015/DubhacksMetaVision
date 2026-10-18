"""Stream JPEG frames to a tracking session.
Usage: python client_example.py "person" frame1.jpg frame2.jpg ... [--url https://dubhacks.tamkar.ai/inference]
Put CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET (service token) in .env.
"""
import os
import sys

import requests
from dotenv import load_dotenv

args = sys.argv[1:]
url = "https://dubhacks.tamkar.ai/inference"
load_dotenv()
AUTH = {
    "CF-Access-Client-Id": os.environ.get("CF_ACCESS_CLIENT_ID", ""),
    "CF-Access-Client-Secret": os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
}
if "--url" in args:
    i = args.index("--url")
    url = args[i + 1]
    del args[i : i + 2]
prompt, frames = args[0], args[1:]

sid = requests.post(f"{url}/session", json={"prompt": prompt}, headers=AUTH).json()["session_id"]
try:
    for path in frames:
        with open(path, "rb") as f:
            r = requests.post(f"{url}/session/{sid}/frame", data=f.read(), headers={**AUTH, "Content-Type": "image/jpeg"})
        r.raise_for_status()
        print(path, r.json())
finally:
    requests.delete(f"{url}/session/{sid}", headers=AUTH)
