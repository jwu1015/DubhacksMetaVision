"""Read deploy.yaml for deploy.sh: write each service's environment to a file, print the deploy settings.

Usage: python deploy_config.py CONFIG OUT_DIR
Writes OUT_DIR/segmentation.env, OUT_DIR/backend.env and OUT_DIR/tunnel.env (mode 600, `export K='v'` lines,
sourced by the tmux windows so secrets never show up in `ps`), and prints shell assignments
(SESSION=..., SEG_PORT=..., ...) for deploy.sh to eval. Exits 1 with a message on a bad config.
"""
import shlex
import sys
from pathlib import Path

import yaml

SECTIONS = {"session", "tunnel", "segmentation", "backend", "voice"}
TUNNEL_MODES = ("quick", "named", "none")


def fail(message: str):
    sys.exit(f"deploy.yaml: {message}")


def section(config: dict, name: str) -> dict:
    value = config.get(name) or {}
    if not isinstance(value, dict):
        fail(f"'{name}' must be a mapping")
    return value


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def env_map(sec: dict, name: str) -> dict[str, str]:
    env = sec.get("env") or {}
    if not isinstance(env, dict):
        fail(f"'{name}.env' must be a mapping of NAME: value")
    # Blank values stay unset, so the code's defaults apply
    return {str(k): text(v) for k, v in env.items() if text(v) != ""}


def write_env(path: Path, env: dict[str, str]):
    path.write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()))
    path.chmod(0o600)


def main(config_path: str, out_dir: str):
    try:
        config = yaml.safe_load(Path(config_path).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        fail(str(e))
    if not isinstance(config, dict):
        fail("expected a mapping at the top level")
    if unknown := set(config) - SECTIONS:
        fail(f"unknown keys: {', '.join(sorted(unknown))}")

    tunnel, seg, backend, voice = (section(config, n) for n in ("tunnel", "segmentation", "backend", "voice"))
    mode = text(tunnel.get("mode", "quick"))
    if mode not in TUNNEL_MODES:
        fail(f"tunnel.mode must be one of {', '.join(TUNNEL_MODES)}, not '{mode}'")
    token, public_url = text(tunnel.get("token")), text(tunnel.get("public_url")).rstrip("/")
    if mode == "named" and not (token and public_url):
        fail("tunnel.mode 'named' needs tunnel.token and tunnel.public_url")

    seg_host, seg_port = text(seg.get("host", "127.0.0.1")), text(seg.get("port", 10100))
    backend_port = text(backend.get("port", 8000))
    seg_env = env_map(seg, "segmentation") | {"HOST": seg_host, "PORT": seg_port}
    backend_env = env_map(backend, "backend") | {
        # The backend reaches the segmentation server over localhost, so it skips the tunnel and Cloudflare Access
        "MODEL_SERVICE_URL": f"http://127.0.0.1:{seg_port}",
        "CF_ACCESS_CLIENT_ID": "",
        "CF_ACCESS_CLIENT_SECRET": "",
        "STOREFRONT_DOTENV": "0",  # deploy.yaml is the only config: ignore a stray backend/.env
    }
    if public_url:  # quick mode: deploy.sh adds the tunnel's URL once it has one
        backend_env["PUBLIC_BASE_URL"] = public_url

    voice_on = text(voice.get("enabled", True)) == "true"
    if voice_on and not (backend_env.get("GOOGLE_API_KEY") and backend_env.get("LIVEKIT_URL")):
        print("warning: no voice agent: set GOOGLE_API_KEY and LIVEKIT_URL in backend.env", file=sys.stderr)
        voice_on = False

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    write_env(out / "segmentation.env", seg_env)
    write_env(out / "backend.env", backend_env)
    write_env(out / "tunnel.env", {"TUNNEL_TOKEN": token})  # cloudflared reads TUNNEL_TOKEN

    settings = {
        "SESSION": text(config.get("session", "storefront")),
        "SEG_VENV": text(seg.get("venv", "/venv/main")),
        "SEG_PORT": seg_port,
        "BACKEND_VENV": text(backend.get("venv", "/venv/backend")),
        "BACKEND_PORT": backend_port,
        "TUNNEL_MODE": mode,
        "PUBLIC_URL": public_url,
        "VOICE": "1" if voice_on else "0",
    }
    print("\n".join(f"{k}={shlex.quote(v)}" for k, v in settings.items()))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
