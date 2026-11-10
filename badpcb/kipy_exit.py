"""Exit helper for every $KIPY (pcbnew) entry point: skips interpreter teardown, which segfaults (exit 139) in KiCad's
python when SWIG wrappers outlive their C++ objects. Call as the very last statement, on success AND error paths."""
import os
import sys


def done(code=0):
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)
