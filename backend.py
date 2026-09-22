"""Line-oriented local IPC for the Flutter desktop client (no HTTP server)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from app import Transcriber, devices


def handle(engine: Transcriber, message: dict) -> dict:
    command = message["command"]
    if command == "status":
        return engine.status()
    if command == "devices":
        return {"devices": devices()}
    if command == "start_live":
        engine.start("live", message["source"], message["output"], message["model"],
                     live_chunk_seconds=message.get("chunk_seconds", 8))
    elif command == "start_file":
        source = Path(message["source"]).expanduser().resolve()
        if not source.is_file():
            raise ValueError("Choose an existing audio or video file")
        # Never delete a user's original recording. HTTP uploads are handled
        # separately and are owned by the HTTP worker.
        engine.start("file", str(source), message["output"], message["model"])
    elif command == "stop":
        engine.stop()
    elif command == "output":
        engine.set_output(message["output"])
    else:
        raise ValueError(f"Unknown command: {command}")
    return engine.status()


def serve(stdin: TextIO, stdout: TextIO, engine: Transcriber | None = None) -> None:
    engine = engine or Transcriber()
    try:
        for line in stdin:
            request = None
            try:
                request = json.loads(line)
                result = handle(engine, request)
                reply = {"id": request.get("id"), "ok": True, "data": result}
            except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
                reply = {"id": request.get("id") if isinstance(request, dict) else None,
                         "ok": False, "error": str(exc)}
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    finally:
        engine.stop()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
