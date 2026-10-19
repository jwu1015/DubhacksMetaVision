# Make Any Room a Storefront

> No matter where you are, if you see something you like, we'll find it for you. Live.

A Meta glasses plug-in that turns whatever you're looking at into something you can shop. Walk into a room, look at a product, and get instant feedback on it — then act on it with your voice.

The iPhone app is **Storefront**. The FastAPI service in `backend/` identifies what the camera sees, prices it, and checks out. A Gemini Live agent in the same LiveKit room handles voice.

---

## Tracks

| Track | Sponsor |
|-------|---------|
| **Main Track** | Visa |
| **Side Track** | T-Mobile (Matchmaker) |

Checkout runs through **Stripe test mode**. The Visa story is an agent that can shop inside a limit the user sets, with Face ID before the checkout sheet opens.

---

## How it works

1. **See it** — The phone or the Meta glasses publish one camera track into a LiveKit room (`POST /session/token`).
2. **Track it** — The backend follows objects through the SAM segmentation server and attaches a quick label (name and price range) once an object has been on screen briefly.
3. **Price it** — A Look describes the object, searches stores, and ranks matches.
4. **Act on it** — Tap a label, or turn voice on. The Gemini Live agent joins that room, sees the camera, and can save, add to cart, or start checkout.

| User says | What happens |
|-----------|--------------|
| "What is this?" | Identifies the product in view and reads back a short summary |
| "Tell me more" | Opens the ranked matches (specs, other sellers) |
| "Add to cart" | Adds the product to the cart |
| "Save it for later" | Saves the product |
| "Buy it" | Face ID, then a Stripe test checkout |

The phone draws labels and product cards from the room's `updates` topic. Taps go out as the `select` RPC. Voice on is the `voice` RPC, which brings the agent into the session room.

---

## Repo

| Path | What it is |
|------|------------|
| `frontend/` | Storefront, the SwiftUI iPhone app (its Xcode project is still named `Spotted`). Screen-level detail is in [`frontend/FRONTEND.md`](frontend/FRONTEND.md). |
| `backend/` | FastAPI app (`storefront.app`): tracking, Looks, cart, checkout, and the voice worker. |
| `backend/segmentation/` | SAM server the backend calls for object tracking. It runs on the GPU box, not on the laptop. |

---

## Deploy

Runs the segmentation server, the backend and the voice agent on one GPU machine.

1. **Install** `tmux`, `uv`, `curl` and `cloudflared`.
2. **Configure:**
   ```bash
   cd backend
   cp deploy.example.yaml deploy.yaml
   ```
   Fill in the API keys in `deploy.yaml`. It's gitignored, so your keys stay out of the repo.
3. **Deploy:**
   ```bash
   ./deploy.sh
   ```
   It prints the backend's public URL when it's done.
4. **Check on it:** `tmux attach -t storefront` (one window per service; detach with `Ctrl-b d`).

**After pulling new code:** `./deploy.sh --restart`. To restart only some services, name them, e.g. `./deploy.sh --restart backend voice`.

---

## Getting started (local development)

### Backend

From `backend/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in keys; see comments in the example
uvicorn storefront.app:app --reload --port 8000 --timeout-graceful-shutdown 1
```

`.env` needs at least `ANTHROPIC_API_KEY` (labels and ranking), store-search keys (`SERPAPI_KEY` or `OPENWEB_NINJA_API_KEY`), `STRIPE_SECRET_KEY`, and `MODEL_SERVICE_URL` for the segmentation server. Live sessions also need `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`.

Voice worker, in a second terminal from `backend/` (also needs `GOOGLE_API_KEY`):

```bash
python -m storefront.voice.agent start
```

Tests (live tests stay off unless you pass `-m live`):

```bash
pytest
```

### iOS app

Open `frontend/Spotted.xcodeproj` in Xcode and run the **Spotted** scheme on a paired iPhone. Glasses streaming needs the Meta Wearables DAT SDK and the Meta AI app; the phone camera works without glasses.

`Spotted.xcconfig` points at `https://dubhacks.tamkar.ai`. For a laptop or ngrok backend, copy `frontend/Config.example.xcconfig` to the untracked `Config.xcconfig` and set `API_BASE_URL` there. Setup, Info.plist keys, and Cloudflare Access headers are in [`frontend/FRONTEND.md`](frontend/FRONTEND.md#setup).

### Segmentation server

The backend sends frames to `MODEL_SERVICE_URL` (Cloudflare Access headers `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` when the server is not on localhost). `backend/segmentation/server.py` is that server; on the GPU box, `deploy.sh` runs it (see [Deploy](#deploy)).
