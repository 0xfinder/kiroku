"""Pinned, checksum-verified Fluid Speech Mini weights. No downloaded Python is executed."""

import hashlib
import json
from pathlib import Path

import httpx

from kiroku.errors import KirokuError

REVISION = "fc592620325027419883cf272e70588d8a788c0a"
MODEL = "FluidVoice-Labs/fluid-speech-mini-onnx"
FILES: dict[str, str] = json.loads(Path(__file__).with_name("fluid_manifest.json").read_text())


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify(directory: Path) -> None:
    for name, expected in FILES.items():
        path = directory / name
        if not path.is_file() or digest(path) != expected:
            raise KirokuError(
                f"Fluid model missing or changed: {path}. Run `kiroku transcriber install-fluid`."
            )


def install(http: httpx.Client, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        destination = directory / name
        if destination.is_file() and digest(destination) == expected:
            continue
        # Unique temporary files keep concurrent installs independent; rename only verified bytes.
        import tempfile

        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
            temporary = Path(file.name)
            try:
                with http.stream(
                    "GET",
                    f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}",
                    follow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    for block in response.iter_bytes():
                        file.write(block)
                file.flush()
                if digest(temporary) != expected:
                    raise KirokuError(f"Fluid checksum mismatch for {name}")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    verify(directory)
