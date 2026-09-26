"""Build the developer reference and start the backend service."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "_build" / "html"
sys.path.insert(0, str(ROOT))


def build_code_docs() -> None:
    command = [sys.executable, "-m", "sphinx", "-b", "html", "docs/sphinx", "docs/_build/html", "--keep-going"]
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        print(f"Code documentation: http://127.0.0.1:8000/code/ ({OUTPUT})", flush=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"WARNING: Sphinx documentation was not built: {exc}", flush=True)


if __name__ == "__main__":
    build_code_docs()
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=8000)
