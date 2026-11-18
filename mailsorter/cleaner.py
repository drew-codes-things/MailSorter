import json
import logging
import os
import select
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .sanitize import CleanEmail, clean_email_html, is_too_complex

WORKERS = 2
CLEAN_SECONDS = 1.5
WAIT_FOR_WORKER_SECONDS = 1.0
STOP_SECONDS = 2.0
FRAME_HEADER = struct.Struct(">I")
READ_CHUNK_BYTES = 64 * 1024
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
WORKER_COMMAND = (sys.executable, "-m", "mailsorter.cleaner")

log = logging.getLogger("mailsorter.cleaner")


class TooComplex(Exception):
    pass


class CleanerBusy(Exception):
    pass


class WorkerFailed(Exception):
    pass


def read_frame(stream) -> bytes | None:
    header = stream.read(FRAME_HEADER.size)
    if len(header) < FRAME_HEADER.size:
        return None
    (size,) = FRAME_HEADER.unpack(header)
    payload = stream.read(size)
    return payload if len(payload) == size else None


def write_frame(stream, payload: bytes) -> None:
    stream.write(FRAME_HEADER.pack(len(payload)) + payload)
    stream.flush()


def serve(source, sink) -> None:
    while (frame := read_frame(source)) is not None:
        cleaned = clean_email_html(frame.decode("utf-8", errors="replace"))
        write_frame(sink, json.dumps({"html": cleaned.html, "remoteImages": cleaned.remote_images}).encode())


def _seconds_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise WorkerFailed("the cleaner ran out of time")
    return left


class _Worker:
    def __init__(self, command: tuple[str, ...]):
        self.process = subprocess.Popen(list(command), cwd=PACKAGE_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)

    def _send(self, payload: bytes, deadline: float) -> None:
        handle = self.process.stdin.fileno()
        view = memoryview(FRAME_HEADER.pack(len(payload)) + payload)
        while view:
            if not select.select([], [handle], [], _seconds_left(deadline))[1]:
                raise WorkerFailed("the cleaner stopped reading")
            try:
                view = view[os.write(handle, view):]
            except BlockingIOError:
                continue

    def _receive(self, size: int, deadline: float) -> bytes:
        handle = self.process.stdout.fileno()
        received = bytearray()
        while len(received) < size:
            if not select.select([handle], [], [], _seconds_left(deadline))[0]:
                raise WorkerFailed("the cleaner ran out of time")
            chunk = os.read(handle, min(size - len(received), READ_CHUNK_BYTES))
            if not chunk:
                raise WorkerFailed("the cleaner stopped")
            received += chunk
        return bytes(received)

    def clean(self, raw: str, deadline: float) -> CleanEmail:
        try:
            self._send(raw.encode("utf-8", errors="replace"), deadline)
            (size,) = FRAME_HEADER.unpack(self._receive(FRAME_HEADER.size, deadline))
            answer = json.loads(self._receive(size, deadline))
        except (OSError, ValueError) as error:
            raise WorkerFailed(f"the cleaner failed: {error}") from error
        return CleanEmail(html=answer["html"], remote_images=answer["remoteImages"])

    def stop(self) -> None:
        self.process.kill()
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            log.error("a cleaner process (pid %s) did not stop after kill", self.process.pid)
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()


class IsolatedCleaner:
    def __init__(self, workers: int = WORKERS, clean_seconds: float = CLEAN_SECONDS,
                 wait_seconds: float = WAIT_FOR_WORKER_SECONDS, command: tuple[str, ...] = WORKER_COMMAND):
        self.command = command
        self.clean_seconds = clean_seconds
        self.wait_seconds = wait_seconds
        self._slots = threading.BoundedSemaphore(workers)
        self._idle: list[_Worker] = []
        self._lock = threading.Lock()

    def _take_worker(self) -> _Worker:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        return _Worker(self.command)

    def _give_back(self, worker: _Worker) -> None:
        with self._lock:
            self._idle.append(worker)

    def clean(self, raw: str) -> CleanEmail:
        if is_too_complex(raw):
            raise TooComplex("the email HTML is too large or too deeply nested to clean")
        if not self._slots.acquire(timeout=self.wait_seconds):
            raise CleanerBusy("every cleaner is busy")
        try:
            worker = self._take_worker()
            try:
                cleaned = worker.clean(raw, time.monotonic() + self.clean_seconds)
            except WorkerFailed as error:
                worker.stop()
                log.warning("an email's HTML could not be cleaned in %.1f s, so it is shown another way: %s",
                            self.clean_seconds, error)
                raise TooComplex(str(error)) from error
            self._give_back(worker)
            return cleaned
        finally:
            self._slots.release()

    def close(self) -> None:
        with self._lock:
            workers, self._idle = self._idle, []
        for worker in workers:
            worker.stop()


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
