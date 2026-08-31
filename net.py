"""Resilient HTTP for IGN's services.

IGN's download and WFS endpoints fail intermittently, and the more tiles a job
touches the more likely it is that at least one request dies. A 25-tile ortho
fetch used to be thrown away by a single HTTP 502. Everything that talks to
IGN goes through here instead.

What this gives every request:

  * retries with exponential backoff, on transport errors and on the 5xx and
    429 responses that mean "try again", but not on 404, which never will
  * resumed downloads via HTTP Range, so a tile interrupted at 300 MB of 400
    continues rather than restarting
  * size verification against Content-Length, because a truncated LAZ is
    accepted silently by everything downstream until PDAL fails on it much
    later with an unhelpful error
  * an atomic rename, so a partly-written file is never mistaken for a
    complete one by the next run
"""

import time
import urllib.error
import urllib.request
from pathlib import Path

UA = {"User-Agent": "ign-lidar-tiler/0.1 (+https://github.com/fitaine/ign-lidar-tiler)"}
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_RETRIES = 6


def _retryable(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRY_STATUS
    return True          # timeouts, resets, DNS: all worth another go


def _backoff(attempt):
    return min(60.0, 2.0 ** attempt)


def get(url, timeout=120, retries=DEFAULT_RETRIES, log=print):
    """GET the whole body, with retries. Returns bytes."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if not _retryable(e) or attempt == retries:
                break
            w = _backoff(attempt)
            log(f"      request failed ({e}); retry {attempt}/{retries - 1} in {w:.0f}s")
            time.sleep(w)
    raise last


def head_size(url, timeout=60, retries=3):
    """Content-Length, or 0 if the server will not say."""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return int(r.headers.get("Content-Length", 0))
        except Exception as e:
            if not _retryable(e) or attempt == retries:
                return 0
            time.sleep(_backoff(attempt))
    return 0


def download(url, dest, expected=None, timeout=300, retries=DEFAULT_RETRIES,
             chunk=1 << 20, log=print, progress=True):
    """Download to `dest`, resuming and retrying. Returns the byte count.

    A `.part` file beside the destination holds the partial download between
    attempts, so an interrupted tile resumes instead of starting over.
    """
    dest = Path(dest)
    part = dest.with_suffix(dest.suffix + ".part")
    if expected is None:
        expected = head_size(url)

    if dest.is_file() and expected and dest.stat().st_size == expected:
        log(f"      {dest.name} already complete")
        return dest.stat().st_size

    last = None
    for attempt in range(1, retries + 1):
        pos = part.stat().st_size if part.is_file() else 0
        if expected and pos > expected:          # stale or corrupt part file
            part.unlink()
            pos = 0
        headers = dict(UA)
        if pos:
            headers["Range"] = f"bytes={pos}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resuming = (r.status == 206 and pos > 0)
                if not resuming:
                    pos = 0                      # server ignored Range
                total = int(r.headers.get("Content-Length", 0)) + pos
                mode = "ab" if resuming else "wb"
                got = pos
                with open(part, mode) as f:
                    while True:
                        block = r.read(chunk)
                        if not block:
                            break
                        f.write(block)
                        got += len(block)
                        if progress and total:
                            print(f"\r      {dest.name}  {got/1e6:8.1f} / "
                                  f"{total/1e6:.1f} MB", end="", flush=True)
                if progress and total:
                    print()

            size = part.stat().st_size
            if expected and size != expected:
                raise IOError(f"got {size} bytes, expected {expected}")
            part.replace(dest)
            return size

        except Exception as e:
            last = e
            if not _retryable(e) or attempt == retries:
                break
            w = _backoff(attempt)
            done = part.stat().st_size if part.is_file() else 0
            log(f"\n      {dest.name} failed at {done/1e6:.1f} MB ({e}); "
                f"resuming in {w:.0f}s  [{attempt}/{retries - 1}]")
            time.sleep(w)

    raise IOError(f"could not download {dest.name} after {retries} attempts: {last}")
