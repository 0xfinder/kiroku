"""Private JSON-lines IPC worker. Only this process imports ONNX Runtime and NumPy."""

import base64
import json
import os
import signal
import sys


def main() -> None:
    # Ctrl-C and systemd broadcast to children too. The supervisor owns the drain;
    # stdin EOF ends this worker, and its timeout path escalates to SIGKILL.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.environ.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    try:
        from kiroku.transcriber._fluid_onnx import Model
    except ImportError:
        print(
            json.dumps({"error": "Install local inference with `uv sync --extra fluid`."}),
            flush=True,
        )
        return
    try:
        model = Model(sys.argv[1], threads=int(sys.argv[2]), spin=False)
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            pcm = base64.b64decode(request["pcm"], validate=True)
            segments = model.transcribe(pcm)
            print(json.dumps({"segments": segments}, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {str(error)[:500]}"}), flush=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
