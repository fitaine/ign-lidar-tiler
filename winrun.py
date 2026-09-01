"""One place for the Windows flag that keeps child processes windowless.

Every PDAL call reaches into the QGIS install, and Windows gives a console
application its own console window unless told not to. On a job that probes
sixteen tiles that is sixteen black windows titled "qgis" stealing focus while
Tiphaine is working. blender.exe -b does the same thing.

CREATE_NO_WINDOW suppresses the window, but it also costs the child the
inherited console: a child that simply printed to the parent's stdout comes
back silent. Measured on 2026-09-01, plain subprocess.run showed the child's
output and the same call with the flag showed nothing. So anything whose output
belongs in the log has to pipe it explicitly — capture_output for short calls,
stream() below for long ones that must show progress while they run.
"""
import subprocess
import sys

# 0 on anything that is not Windows, where the flag does not exist and no
# window would appear anyway.
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def run(*args, **kw):
    """subprocess.run, without the console window."""
    kw.setdefault("creationflags", NO_WINDOW["creationflags"])
    return subprocess.run(*args, **kw)


def popen(*args, **kw):
    """subprocess.Popen, without the console window."""
    kw.setdefault("creationflags", NO_WINDOW["creationflags"])
    return subprocess.Popen(*args, **kw)


def stream(cmd, echo=True):
    """Run with the child's output piped back line by line as it arrives.

    Replaces a bare subprocess.run() that relied on the child inheriting
    stdout, which CREATE_NO_WINDOW takes away. Returns a CompletedProcess so
    callers can keep checking .returncode, with .stdout holding everything.
    """
    proc = popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                 stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in proc.stdout:
        if echo:
            sys.stdout.write(line)
            sys.stdout.flush()
        lines.append(line)
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(lines), "")
