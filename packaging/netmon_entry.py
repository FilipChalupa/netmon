"""Entry point for the released single binary.

    netmon [single] [--network NAME] [--port 8000] ...   all-in-one (default)
    netmon monitor [--config monitor.ini]                measuring agent only
    netmon server [--host 0.0.0.0] [--port 8000]         evaluation server only
                                                         (NETMON_* env config)
"""

from __future__ import annotations

import sys

COMMANDS = ("single", "monitor", "server")


def main() -> int:
    args = sys.argv[1:]
    cmd = "single"
    if args and args[0] in COMMANDS:
        cmd = args[0]
        args = args[1:]
    elif args and args[0] in ("-h", "--help") and len(args) == 1:
        print(__doc__.strip())
        print("\nRun `netmon <command> --help` for command options.")
        return 0

    if cmd == "monitor":
        from netmon_monitor.__main__ import main as monitor_main
        sys.argv = ["netmon monitor"] + args
        return monitor_main()

    if cmd == "server":
        import argparse

        import uvicorn
        ap = argparse.ArgumentParser(
            prog="netmon server",
            description="evaluation server only (config via NETMON_* env "
                        "and monitors.toml)")
        ap.add_argument("--host", default="0.0.0.0")
        ap.add_argument("--port", type=int, default=8000)
        a = ap.parse_args(args)
        from netmon_server.main import app
        uvicorn.run(app, host=a.host, port=a.port, log_level="info")
        return 0

    from netmon_server.single import main as single_main
    return single_main(args)


if __name__ == "__main__":
    sys.exit(main())
