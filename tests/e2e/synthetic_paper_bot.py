"""SYNTHETIC PAPER bot for dashboard QA. NOT real market data.

Runs the real TradingBot (paper broker, risk, execution, control plane) on
generated NIFTY ticks: the ORB day from the integration tests, then a slow
random walk, one tick per second. It never connects to Upstox. Every second
it runs the control plane, so commands clicked in the dashboard are executed
by the bot.

    python -m tests.e2e.synthetic_paper_bot --db <path> --seconds 900
    streamlit run dashboard/app.py -- --db <same path>
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import timedelta
from pathlib import Path

from data.storage.database import Database
from tests.synthetic import at
from tests.unit.test_bot_integration import ORB_DAY, Sim


async def run(db: Path, seconds: int) -> None:
    sim = Sim(at(9, 14), db=Database(db))
    await sim.bot.prepare()
    sim.bot.start()
    await sim.run_path(ORB_DAY)
    await sim.bot.control.tick()
    rng = random.Random(7)  # noqa: S311 - synthetic prices, not cryptography
    t, spot = at(9, 50), 24565.0
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    print(f"SYNTHETIC PAPER bot running on {db} for {seconds}s", flush=True)
    while loop.time() < end:
        t += timedelta(seconds=1)
        spot += rng.gauss(0.0, 1.5)
        await sim.step(t, spot, premium=max(5.0, 100.0 + (spot - 24500.0) * 0.5))
        await sim.bot.control.tick()
        await sim.bot.settle()
        await asyncio.sleep(1.0)
    sim.db.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--seconds", type=int, default=900)
    a = p.parse_args()
    asyncio.run(run(a.db, a.seconds))


if __name__ == "__main__":
    main()
