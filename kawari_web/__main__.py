"""Entry point: python -m kawari_web"""

from __future__ import annotations

import sys


def main() -> int:
    import os

    from . import __version__
    from .app import create_app
    from .config import Settings

    settings = Settings.from_env()
    # Printed before anything can fail, so the log always identifies which build
    # is actually running and which identity it has.
    sys.stderr.write(
        f"ArchonEngine ControlPanel {__version__} starting\n"
        f"  uid={os.getuid()} gid={os.getgid()}\n"
        f"  data dir : {settings.data_dir}\n"
        f"  web dir  : {settings.web_dir}\n"
        f"  host view: {settings.host_data_dir}\n"
        f"  port     : {settings.port}\n")
    try:
        stat = os.stat(settings.data_dir)
        sys.stderr.write(f"  data dir owner uid={stat.st_uid} gid={stat.st_gid}, "
                         f"writable={os.access(settings.data_dir, os.W_OK)}\n")
    except OSError as exc:
        sys.stderr.write(f"  data dir not readable: {exc}\n")

    try:
        app = create_app(settings)
    except PermissionError as exc:
        # One clear message beats the same traceback every restart.
        sys.stderr.write(f"\nArchonEngine ControlPanel cannot start.\n\n{exc}\n\n")
        return 1
    try:
        from waitress import serve
        print(f"ArchonEngine ControlPanel on http://{settings.host}:{settings.port}  "
              f"(data: {settings.data_dir})")
        serve(app, host=settings.host, port=settings.port, threads=8)
    except ImportError:
        print("waitress not installed; falling back to the Flask dev server.")
        app.run(host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
