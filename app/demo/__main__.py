"""``python -m app.demo``: reset a SQLite demo DB, seed it, and serve the live demo.

The web process also runs the worker and the lead simulator (``DEMO_MODE=1``), so this one
command is the whole demo. Open http://127.0.0.1:8000/login and use the demo token shown.
"""

from __future__ import annotations

import argparse
import os

DEFAULT_DB = "sqlite:///./demo.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--seed-only", action="store_true", help="reset + seed, then exit")
    args = parser.parse_args(argv)

    os.environ.setdefault("DATABASE_URL", DEFAULT_DB)
    os.environ["DEMO_MODE"] = "1"
    os.environ["PORT"] = str(args.port)

    from app.config import get_settings
    from app.db import make_engine, make_session_factory
    from app.demo.runtime import reseed
    from app.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    if args.seed_only:
        engine = make_engine(settings.database_url)
        report = reseed(settings, engine, make_session_factory(engine))
        engine.dispose()
        print(f"seeded {report.leads} leads: {report.stages}")
        return 0

    import uvicorn

    print(
        f"\n  Demo: http://{args.host}:{args.port}/login  (token: "
        f"{settings.demo_token.get_secret_value() if settings.demo_token else '?'})\n"
    )
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
