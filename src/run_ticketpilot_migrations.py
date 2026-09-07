import asyncio
import sys

from ticketpilot.db import apply_migrations, get_ticketpilot_pool


async def main() -> None:
    async with get_ticketpilot_pool() as pool:
        applied = await apply_migrations(pool)
    if applied:
        print(f"Applied TicketPilot migrations: {', '.join(applied)}")  # noqa: T201
    else:
        print("TicketPilot database schema is up to date.")  # noqa: T201


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
