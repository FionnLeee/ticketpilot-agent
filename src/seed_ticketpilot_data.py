import asyncio
import json
import sys
from dataclasses import asdict

from ticketpilot.db import apply_migrations, get_ticketpilot_pool
from ticketpilot.seed import seed_orders


async def main() -> None:
    async with get_ticketpilot_pool() as pool:
        await apply_migrations(pool)
        result = await seed_orders(pool)
    print(json.dumps(asdict(result), ensure_ascii=False))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
