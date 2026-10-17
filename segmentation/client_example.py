"""Usage: python client_example.py image.jpg [prompt] [url]"""
import sys

import requests

path = sys.argv[1]
prompt = sys.argv[2] if len(sys.argv) > 2 else "person"
url = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:8000/segment"
with open(path, "rb") as f:
    r = requests.post(url, params={"prompt": prompt}, data=f.read(), headers={"Content-Type": "image/jpeg"})
r.raise_for_status()
print(r.json())
