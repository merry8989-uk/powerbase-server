#!/usr/bin/env python3
"""Development entry point:  python run.py"""

import uvicorn

from powerbase.api import create_app
from powerbase.config import Settings
from powerbase.logging_setup import configure_logging

if __name__ == "__main__":
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,  # ours is structured and carries the app id
    )
