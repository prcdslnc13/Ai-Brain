"""Entry point: `python -m brain_mcp` or the `brain-mcp` script."""

import asyncio

from .server import run


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
