"""Explicit synthetic-data creation; importing/running the API never resets data."""

import argparse
import asyncio

from main import Base, async_engine, create_test_data, init_db, sync_engine


async def seed(notes, reset=False):
    if reset:
        Base.metadata.drop_all(bind=sync_engine)
    init_db()
    try:
        await create_test_data(notes)
    finally:
        await async_engine.dispose()
        sync_engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notes', type=int, default=1000)
    parser.add_argument('--reset', action='store_true', help='Delete existing notes before seeding.')
    args = parser.parse_args()
    if not 1 <= args.notes <= 1_000_000:
        parser.error('--notes must be between 1 and 1000000')
    asyncio.run(seed(args.notes, args.reset))


if __name__ == '__main__':
    main()
