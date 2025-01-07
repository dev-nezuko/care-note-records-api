import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select

_directory = tempfile.TemporaryDirectory(prefix='care-notes-tests-')
os.environ['CARE_NOTES_DB_PATH'] = str(Path(_directory.name) / 'test.db')
import main


class StartupAndInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.Base.metadata.drop_all(bind=main.sync_engine)
        main.init_db()
        async with main.async_session_maker() as db:
            db.add(main.CareNote(tenant_id=1, facility_id=11, patient_id='synthetic-patient',
                category='observation', priority=2, created_at=datetime.utcnow(),
                created_by='test-user', note_content='Synthetic regression fixture'))
            await db.commit()

    async def asyncTearDown(self):
        await main.async_engine.dispose()

    async def count(self):
        async with main.async_session_maker() as db:
            return await db.scalar(select(func.count()).select_from(main.CareNote))

    async def test_startup_preserves_notes_and_does_not_seed(self):
        with patch('main.create_test_data', new_callable=AsyncMock) as seeder:
            await main.startup_event()
            await main.startup_event()
            seeder.assert_not_awaited()
        self.assertEqual(await self.count(), 1)

    async def test_explicit_demo_seeding_appends_the_requested_count(self):
        await main.create_test_data(3)
        self.assertEqual(await self.count(), 4)

    async def test_invalid_pagination_is_rejected_instead_of_dividing_by_zero(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            for query in ['page=0', 'page_size=0', 'page_size=101', 'page=-1']:
                with self.subTest(query=query):
                    response = await client.get('/api/care-notes?' + query)
                    self.assertEqual(response.status_code, 422)

    async def test_invalid_facility_input_is_a_client_error(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            for value in ['invalid', '-1', ',,']:
                response = await client.get('/api/care-notes', params={'facility_ids': value})
                self.assertEqual(response.status_code, 400)

    async def test_existing_notes_remain_readable_after_startup(self):
        await main.startup_event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.get('/api/care-notes', params={'tenant_id': 1, 'facility_ids': '11'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['pagination']['total'], 1)
        self.assertEqual(response.json()['notes'][0]['note_content'], 'Synthetic regression fixture')


def tearDownModule():
    main.sync_engine.dispose()
    _directory.cleanup()


if __name__ == '__main__':
    unittest.main()
