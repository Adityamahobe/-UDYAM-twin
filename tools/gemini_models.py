"""List the Gemini models your GEMINI_API_KEY can use for the Advisor chatbot.

    python -m tools.gemini_models          (reads GEMINI_API_KEY from the environment or .env)

Pick one of the printed IDs for GEMINI_MODEL. Only models that support generateContent are shown.
The key is read from the environment and never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_dotenv()
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key or key.startswith("your-"):
        print("GEMINI_API_KEY is not set (put it in .env). Get a key at aistudio.google.com/apikey.", file=sys.stderr)
        return 1
    from google import genai
    from google.genai import errors

    client = genai.Client(api_key=key)  # keep a reference: the pager needs the client open while it fetches
    try:
        models = [m for m in client.models.list() if "generateContent" in (m.supported_actions or [])]
    except errors.APIError as err:
        print(f"Gemini API error {err.code}: {err.message}", file=sys.stderr)
        return 1
    for m in sorted(models, key=lambda m: m.name or ""):
        print(f"{(m.name or '').removeprefix('models/'):45s} {m.display_name or ''}")
    print(f"\n{len(models)} models. Set one as GEMINI_MODEL in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
