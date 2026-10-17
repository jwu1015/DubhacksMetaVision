"""Stream JPEG frames to a tracking session.
Usage: python client_example.py "person" frame1.jpg frame2.jpg ... [--url http://localhost:8000]
"""
import sys

import requests

args = sys.argv[1:]
url = "http://localhost:8000"
if "--url" in args:
    i = args.index("--url")
    url = args[i + 1]
    del args[i : i + 2]
prompt, frames = args[0], args[1:]

sid = requests.post(f"{url}/session", json={"prompt": prompt}).json()["session_id"]
try:
    for path in frames:
        with open(path, "rb") as f:
            r = requests.post(f"{url}/session/{sid}/frame", data=f.read(), headers={"Content-Type": "image/jpeg"})
        r.raise_for_status()
        print(path, r.json())
finally:
    requests.delete(f"{url}/session/{sid}")
