import unittest
from datetime import datetime

import httpx

import testing_db  # noqa: F401  (must precede the app import)
import main  # noqa: E402

# Fixed timestamps keep the date-range assertions independent of the clock.
FIXTURE_NOTES = [
    dict(tenant_id=1, facility_id=11, patient_id='fictional-a', category='medication', priority=1,
         created_at=datetime(2026, 3, 2, 8, 0)),
    dict(tenant_id=1, facility_id=11, patient_id='fictional-a', category='observation', priority=3,
         created_at=datetime(2026, 3, 2, 23, 30)),
    dict(tenant_id=1, facility_id=12, patient_id='fictional-b', category='observation', priority=5,
         created_at=datetime(2026, 3, 3, 12, 0)),
    dict(tenant_id=2, facility_id=21, patient_id='fictional-c', category='treatment', priority=2,
         created_at=datetime(2026, 3, 2, 9, 0)),
]


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.Base.metadata.drop_all(bind=main.sync_engine)
        main.init_db()
        async with main.async_session_maker() as db:
            for fields in FIXTURE_NOTES:
                db.add(main.CareNote(created_by='fictional-carer', note_content='Synthetic fixture', **fields))
            await db.commit()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await main.async_engine.dispose()

    async def stats(self, **params):
        return await self.client.get('/api/care-stats', params=params)


class CareStatsTests(ApiTestCase):
    async def test_custom_range_counts_and_groups_in_sql(self):
        response = await self.stats(tenant_id=1, start_date='2026-03-02', end_date='2026-03-03')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['total_notes'], 3)
        self.assertEqual(body['by_category'], {'medication': 1, 'observation': 2})
        self.assertEqual(body['by_priority'], {'1': 1, '2': 0, '3': 1, '4': 0, '5': 1})
        self.assertEqual(body['by_facility'], {'11': 2, '12': 1})
        self.assertEqual(body['avg_notes_per_patient'], 1.5)

    async def test_date_only_end_bound_includes_the_whole_day(self):
        response = await self.stats(tenant_id=1, start_date='2026-03-02', end_date='2026-03-02')
        self.assertEqual(response.json()['total_notes'], 2)
        self.assertEqual(response.json()['date_range']['end'], '2026-03-02T23:59:59.999999')

    async def test_offset_timestamps_are_converted_to_utc(self):
        # 2026-03-03T01:00+02:00 is 2026-03-02T23:00Z, so only the 23:30 note is after it.
        response = await self.stats(tenant_id=1, start_date='2026-03-03T01:00:00+02:00',
                                    end_date='2026-03-02T23:59:00')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total_notes'], 1)

    async def test_facility_filter_narrows_the_statistics(self):
        response = await self.stats(tenant_id=1, facility_ids='12', range='all_time')
        self.assertEqual(response.json()['total_notes'], 1)
        self.assertEqual(response.json()['by_facility'], {'12': 1})

    async def test_all_time_has_open_bounds(self):
        response = await self.stats(tenant_id=1, range='all_time')
        self.assertEqual(response.json()['total_notes'], 3)
        self.assertEqual(response.json()['date_range'], {'start': None, 'end': None})

    async def test_other_tenants_are_not_counted(self):
        response = await self.stats(tenant_id=2, range='all_time')
        self.assertEqual(response.json()['total_notes'], 1)
        self.assertEqual(response.json()['by_category'], {'treatment': 1})

    async def test_named_range_excludes_old_notes(self):
        response = await self.stats(tenant_id=1, range='today')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total_notes'], 0)

    async def test_rejected_inputs_are_client_errors(self):
        cases = [
            {'range': 'last_decade'},
            {'start_date': '2026-03-02'},
            {'end_date': '2026-03-02'},
            {'start_date': '2026-03-03', 'end_date': '2026-03-02'},
            {'start_date': 'yesterday', 'end_date': '2026-03-02'},
            {'facility_ids': 'abc'},
            {'facility_ids': '0'},
            {'facility_ids': ',,'},
        ]
        for params in cases:
            with self.subTest(params=params):
                response = await self.stats(tenant_id=1, **params)
                self.assertEqual(response.status_code, 400)


def tearDownModule():
    main.sync_engine.dispose()


if __name__ == '__main__':
    unittest.main()
