import random
import unittest
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select

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


class SeedingTests(ApiTestCase):
    async def notes(self):
        async with main.async_session_maker() as db:
            return (await db.execute(select(main.CareNote).order_by(main.CareNote.id))).scalars().all()

    async def test_seeding_spans_several_batches_in_one_transaction(self):
        await main.create_test_data(main.SEED_BATCH_SIZE * 2 + 5, random.Random(1))
        async with main.async_session_maker() as db:
            total = await db.scalar(select(func.count()).select_from(main.CareNote))
        self.assertEqual(total, len(FIXTURE_NOTES) + main.SEED_BATCH_SIZE * 2 + 5)

    async def test_seeded_values_are_fictional_and_in_range(self):
        await main.create_test_data(200, random.Random(7))
        seeded = (await self.notes())[len(FIXTURE_NOTES):]
        cutoff = datetime.utcnow() - timedelta(days=31)
        for note in seeded:
            self.assertTrue(note.patient_id.startswith('DEMO-P'))
            self.assertTrue(note.created_by.startswith('demo-carer-'))
            self.assertIn(note.category, main.DEMO_CATEGORIES)
            self.assertIn(note.priority, range(1, 6))
            self.assertEqual(note.facility_id // 10, note.tenant_id)
            self.assertGreater(note.created_at, cutoff)

    async def test_a_random_seed_reproduces_the_same_notes(self):
        await main.create_test_data(20, random.Random(42))
        first = [(n.patient_id, n.category, n.priority) for n in (await self.notes())[len(FIXTURE_NOTES):]]
        await main.create_test_data(20, random.Random(42))
        second = [(n.patient_id, n.category, n.priority) for n in (await self.notes())[len(FIXTURE_NOTES) + 20:]]
        self.assertEqual(first, second)


class PerformanceTestEndpointTests(ApiTestCase):
    async def test_both_paths_count_the_same_day_and_agree(self):
        day = (datetime.utcnow() - timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)
        async with main.async_session_maker() as db:
            for hour, facility_id in [(0, 11), (12, 12), (23, 15)]:
                db.add(main.CareNote(tenant_id=1, facility_id=facility_id, patient_id='fictional-d',
                                     category='treatment', priority=4, created_by='fictional-carer',
                                     note_content='Benchmark fixture', created_at=day + timedelta(hours=hour)))
            db.add(main.CareNote(tenant_id=1, facility_id=11, patient_id='fictional-d', category='treatment',
                                 priority=4, created_by='fictional-carer', note_content='Next day',
                                 created_at=day + timedelta(days=1)))
            await db.commit()

        response = await self.client.post('/api/run-performance-test', params={'iterations': 2, 'concurrency': 2})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['day'], day.date().isoformat())
        self.assertEqual(body['notes_in_window'], 3)
        self.assertTrue(body['results_match'])
        self.assertEqual(body['sequential']['iterations'], 2)
        self.assertEqual(body['concurrent']['requests'], 2)

    async def test_run_size_is_bounded(self):
        for params in [{'iterations': 0}, {'iterations': 51}, {'concurrency': 0}, {'concurrency': 21}]:
            with self.subTest(params=params):
                response = await self.client.post('/api/run-performance-test', params=params)
                self.assertEqual(response.status_code, 422)


VALID_NOTE = {
    'tenant_id': 1,
    'facility_id': 12,
    'patient_id': 'fictional-e',
    'category': 'observation',
    'priority': 2,
    'created_by': 'fictional-carer',
    'note_content': 'Ate a full breakfast; no concerns.',
}


class CareNoteTests(ApiTestCase):
    async def test_created_note_is_returned_and_listed(self):
        response = await self.client.post('/api/care-notes', json=VALID_NOTE)
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertIsInstance(created['id'], int)
        self.assertEqual({k: created[k] for k in VALID_NOTE}, VALID_NOTE)

        listing = await self.client.get('/api/care-notes', params={'tenant_id': 1, 'facility_ids': '12'})
        self.assertEqual([note['id'] for note in listing.json()['notes']][0], created['id'])

    async def test_json_timestamps_with_offsets_are_stored_in_utc(self):
        response = await self.client.post('/api/care-notes',
                                          json={**VALID_NOTE, 'created_at': '2026-03-04T10:00:00+02:00'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['created_at'], '2026-03-04T08:00:00')

    async def test_invalid_notes_are_rejected_before_reaching_the_database(self):
        cases = {
            'unknown field': {**VALID_NOTE, 'diagnosis': 'x'},
            'client id': {**VALID_NOTE, 'id': 99},
            'bad category': {**VALID_NOTE, 'category': 'gossip'},
            'priority too high': {**VALID_NOTE, 'priority': 6},
            'priority too low': {**VALID_NOTE, 'priority': 0},
            'empty content': {**VALID_NOTE, 'note_content': ''},
            'missing patient': {k: v for k, v in VALID_NOTE.items() if k != 'patient_id'},
            'bad timestamp': {**VALID_NOTE, 'created_at': 'yesterday'},
        }
        for label, body in cases.items():
            with self.subTest(label):
                response = await self.client.post('/api/care-notes', json=body)
                self.assertEqual(response.status_code, 422)
        listing = await self.client.get('/api/care-notes')
        self.assertEqual(listing.json()['pagination']['total'], len(FIXTURE_NOTES))

    async def test_pages_are_newest_first_and_do_not_overlap(self):
        first = (await self.client.get('/api/care-notes', params={'page_size': 3})).json()
        second = (await self.client.get('/api/care-notes', params={'page_size': 3, 'page': 2})).json()
        self.assertEqual(first['pagination'], {'total': 4, 'page': 1, 'page_size': 3, 'total_pages': 2})
        timestamps = [note['created_at'] for note in first['notes'] + second['notes']]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        self.assertEqual(len({note['id'] for note in first['notes'] + second['notes']}), 4)


def tearDownModule():
    main.sync_engine.dispose()


if __name__ == '__main__':
    unittest.main()
