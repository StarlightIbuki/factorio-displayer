"""Allow ``python -m factorio_display`` to run the CLI (used by the web API job runner)."""

from .cli import main

if __name__ == "__main__":
    main()
