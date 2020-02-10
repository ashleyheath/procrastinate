import datetime
import functools
import json
import random
import string

import pendulum
import pytest

from procrastinate import aiopg_connector, jobs

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "job",
    [
        jobs.Job(
            id=2,
            queue="queue_a",
            task_name="task_2",
            lock="lock_2",
            task_kwargs={"c": "d"},
        ),
        jobs.Job(
            id=2,
            queue="queue_a",
            task_name="task_3",
            lock="lock_3",
            task_kwargs={"i": "j"},
            scheduled_at=pendulum.datetime(2000, 1, 1),
        ),
    ],
)
async def test_fetch_job(pg_job_store, job):
    # Add a first started job
    await pg_job_store.defer_job(
        jobs.Job(
            id=1,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    await pg_job_store.fetch_job(queues=None)

    # Now add the job we're testing
    await pg_job_store.defer_job(job)

    assert await pg_job_store.fetch_job(queues=["queue_a"]) == job


@pytest.mark.parametrize(
    "job",
    [
        # We won't see this one because of the lock
        jobs.Job(
            id=2,
            queue="queue_a",
            task_name="task_2",
            lock="lock_1",
            task_kwargs={"e": "f"},
        ),
        # We won't see this one because of the queue
        jobs.Job(
            id=2,
            queue="queue_b",
            task_name="task_3",
            lock="lock_3",
            task_kwargs={"i": "j"},
        ),
        # We won't see this one because of the scheduled date
        jobs.Job(
            id=2,
            queue="queue_a",
            task_name="task_4",
            lock="lock_4",
            task_kwargs={"i": "j"},
            scheduled_at=pendulum.datetime(2100, 1, 1),
        ),
    ],
)
async def test_get_job_no_result(pg_job_store, job):
    # Add a first started job
    await pg_job_store.defer_job(
        jobs.Job(
            id=1,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    await pg_job_store.fetch_job(queues=None)

    # Now add the job we're testing
    await pg_job_store.defer_job(job)

    assert await pg_job_store.fetch_job(queues=["queue_a"]) is None


async def test_get_stalled_jobs(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    job_id = (await get_all("procrastinate_jobs", "id"))[0]["id"]

    # No started job
    assert await pg_job_store.get_stalled_jobs(nb_seconds=3600) == []

    # We start a job and fake its `started_at`
    job = await pg_job_store.fetch_job(queues=["queue_a"])
    await pg_job_store.execute_query(
        f"UPDATE procrastinate_jobs SET started_at=NOW() - INTERVAL '30 minutes' "
        f"WHERE id={job_id}"
    )

    # Nb_seconds parameter
    assert await pg_job_store.get_stalled_jobs(nb_seconds=3600) == []
    assert await pg_job_store.get_stalled_jobs(nb_seconds=1800) == [job]

    # Queue parameter
    assert await pg_job_store.get_stalled_jobs(nb_seconds=1800, queue="queue_a") == [
        job
    ]
    assert await pg_job_store.get_stalled_jobs(nb_seconds=1800, queue="queue_b") == []
    # Task name parameter
    assert await pg_job_store.get_stalled_jobs(nb_seconds=1800, task_name="task_1") == [
        job
    ]
    assert (
        await pg_job_store.get_stalled_jobs(nb_seconds=1800, task_name="task_2") == []
    )


async def test_delete_old_jobs_job_is_not_finished(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )

    # No started job
    await pg_job_store.delete_old_jobs(nb_hours=0)
    assert len(await get_all("procrastinate_jobs", "id")) == 1

    # We start a job
    job = await pg_job_store.fetch_job(queues=["queue_a"])
    # We back date the started event
    await pg_job_store.execute_query(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id}"
    )

    # The job is not finished so it's not deleted
    await pg_job_store.delete_old_jobs(nb_hours=0)
    assert len(await get_all("procrastinate_jobs", "id")) == 1


async def test_delete_old_jobs_multiple_jobs(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_b",
            task_name="task_2",
            lock="lock_2",
            task_kwargs={"a": "b"},
        )
    )

    # We start both jobs
    job_a = await pg_job_store.fetch_job(queues=["queue_a"])
    job_b = await pg_job_store.fetch_job(queues=["queue_b"])
    # We finish both jobs
    await pg_job_store.finish_job(job_a, status=jobs.Status.SUCCEEDED)
    await pg_job_store.finish_job(job_b, status=jobs.Status.SUCCEEDED)
    # We back date the events for job_a
    await pg_job_store.execute_query(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job_a.id}"
    )

    # Only job_a is deleted
    await pg_job_store.delete_old_jobs(nb_hours=2)
    rows = await get_all("procrastinate_jobs", "id")
    assert len(rows) == 1
    assert rows[0]["id"] == job_b.id


async def test_delete_old_job_filter_on_end_date(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    # We start the job
    job = await pg_job_store.fetch_job(queues=["queue_a"])
    # We finish the job
    await pg_job_store.finish_job(job, status=jobs.Status.SUCCEEDED)
    # We back date only the start event
    await pg_job_store.execute_query(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id} AND TYPE='started'"
    )

    # Job is not deleted since it finished recently
    await pg_job_store.delete_old_jobs(nb_hours=2)
    rows = await get_all("procrastinate_jobs", "id")
    assert len(rows) == 1


@pytest.mark.parametrize(
    "status, nb_hours, queue, include_error, should_delete",
    [
        # nb_hours
        (jobs.Status.SUCCEEDED, 1, None, False, True),
        (jobs.Status.SUCCEEDED, 3, None, False, False),
        # queue
        (jobs.Status.SUCCEEDED, 1, "queue_a", False, True),
        (jobs.Status.SUCCEEDED, 3, "queue_a", False, False),
        (jobs.Status.SUCCEEDED, 1, "queue_b", False, False),
        (jobs.Status.SUCCEEDED, 1, "queue_b", False, False),
        # include_error
        (jobs.Status.FAILED, 1, None, False, False),
        (jobs.Status.FAILED, 1, None, True, True),
    ],
)
async def test_delete_old_jobs_parameters(
    get_all, pg_job_store, status, nb_hours, queue, include_error, should_delete
):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )

    # We start a job and fake its `started_at`
    job = await pg_job_store.fetch_job(queues=["queue_a"])
    # We finish the job
    await pg_job_store.finish_job(job, status=status)
    # We back date its events
    await pg_job_store.execute_query(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id}"
    )

    await pg_job_store.delete_old_jobs(
        nb_hours=nb_hours, queue=queue, include_error=include_error
    )
    nb_jobs = len(await get_all("procrastinate_jobs", "id"))
    if should_delete:
        assert nb_jobs == 0
    else:
        assert nb_jobs == 1


async def test_finish_job(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    job = await pg_job_store.fetch_job(queues=["queue_a"])

    assert await get_all("procrastinate_jobs", "status") == [{"status": "doing"}]
    started_at = (await get_all("procrastinate_jobs", "started_at"))[0]["started_at"]
    assert started_at.date() == datetime.datetime.utcnow().date()
    assert await get_all("procrastinate_jobs", "attempts") == [{"attempts": 0}]

    await pg_job_store.finish_job(job=job, status=jobs.Status.SUCCEEDED)

    expected = [{"status": "succeeded", "started_at": started_at, "attempts": 1}]
    assert (
        await get_all("procrastinate_jobs", "status", "started_at", "attempts")
        == expected
    )


async def test_finish_job_retry(get_all, pg_job_store):
    await pg_job_store.defer_job(
        jobs.Job(
            id=0,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            task_kwargs={"a": "b"},
        )
    )
    job1 = await pg_job_store.fetch_job(queues=None)
    await pg_job_store.finish_job(job=job1, status=jobs.Status.TODO)

    job2 = await pg_job_store.fetch_job(queues=None)

    assert job2.id == job1.id
    assert job2.attempts == job1.attempts + 1


async def test_listen_queue(pg_job_store):
    queue = "".join(random.choices(string.ascii_letters, k=10))
    queue_full_name = f"procrastinate_queue#{queue}"
    await pg_job_store.listen_for_jobs(queues=[queue])

    count = await pg_job_store.execute_query_one(
        """SELECT COUNT(*) FROM pg_listening_channels()
                          WHERE pg_listening_channels = %(queue)s""",
        queue=queue_full_name,
    )
    assert count["count"] == 1


async def test_listen_all_queue(pg_job_store):
    await pg_job_store.listen_for_jobs()

    count = await pg_job_store.execute_query_one(
        """SELECT COUNT(*) FROM pg_listening_channels()
           WHERE pg_listening_channels = 'procrastinate_any_queue'"""
    )
    assert count["count"] == 1


async def test_enum_synced(pg_job_store):
    # If this test breaks, it means you've changed either the task_status PG enum
    # or the python procrastinate.jobs.Status Enum without updating the other.
    pg_enum_rows = await pg_job_store.execute_query_all(
        """SELECT e.enumlabel FROM pg_enum e
               JOIN pg_type t ON e.enumtypid = t.oid WHERE t.typname = %(type_name)s""",
        type_name="procrastinate_job_status",
    )

    pg_values = {row["enumlabel"] for row in pg_enum_rows}
    python_values = {status.value for status in jobs.Status.__members__.values()}
    assert pg_values == python_values


async def test_get_connection(connection):
    dsn = connection.dsn
    async with await aiopg_connector.get_connection(dsn=dsn) as new_connection:

        assert new_connection.dsn == dsn


async def test_get_connection_json_loads(connection, mocker):
    dsn = connection.dsn
    json_loads = mocker.MagicMock()
    register_default_jsonb = mocker.patch("psycopg2.extras.register_default_jsonb")
    async with await aiopg_connector.get_connection(
        dsn=dsn, json_loads=json_loads
    ) as new_connection:
        register_default_jsonb.assert_called_with(new_connection.raw, loads=json_loads)


async def test_execute_query_one_json_loads(connection, mocker):
    class NotJSONSerializableByDefault:
        pass

    def encode(obj):
        if isinstance(obj, NotJSONSerializableByDefault):
            return "foo"
        raise TypeError()

    query = "SELECT %(arg)s::jsonb as json"
    arg = {"a": "a", "b": NotJSONSerializableByDefault()}
    json_dumps = functools.partial(json.dumps, default=encode)
    result = await aiopg_connector.execute_query_one(
        connection, query, json_dumps=json_dumps, arg=arg
    )
    assert result["json"] == {"a": "a", "b": "foo"}


async def test_execute_query_all_json_loads(connection, mocker):
    class NotJSONSerializableByDefault:
        pass

    def encode(obj):
        if isinstance(obj, NotJSONSerializableByDefault):
            return "foo"
        raise TypeError()

    query = "SELECT %(arg)s::jsonb as json"
    arg = {"a": "a", "b": NotJSONSerializableByDefault()}
    json_dumps = functools.partial(json.dumps, default=encode)
    result = await aiopg_connector.execute_query_all(
        connection, query, json_dumps=json_dumps, arg=arg
    )
    assert len(result) == 1
    assert result[0]["json"] == {"a": "a", "b": "foo"}


async def test_defer_job(pg_job_store, get_all):
    queue = "marsupilami"
    job = jobs.Job(
        id=0, queue=queue, task_name="bob", lock="sher", task_kwargs={"a": 1, "b": 2}
    )
    pk = await pg_job_store.defer_job(job=job)

    result = await get_all(
        "procrastinate_jobs", "id", "args", "status", "lock", "task_name"
    )
    assert result == [
        {
            "id": pk,
            "args": {"a": 1, "b": 2},
            "status": "todo",
            "lock": "sher",
            "task_name": "bob",
        }
    ]


async def test_execute_query(pg_job_store):
    await pg_job_store.execute_query(
        "COMMENT ON TABLE \"procrastinate_jobs\" IS 'foo' "
    )
    result = await pg_job_store.execute_query_one(
        "SELECT obj_description('public.procrastinate_jobs'::regclass)"
    )
    assert result == {"obj_description": "foo"}

    result = await pg_job_store.execute_query_all(
        "SELECT obj_description('public.procrastinate_jobs'::regclass)"
    )
    assert result == [{"obj_description": "foo"}]


async def test_close_connection(pg_job_store):
    await pg_job_store.get_connection()
    await pg_job_store.close_connection()
    assert pg_job_store._connection.closed == 1


async def test_close_connection_no_connection(pg_job_store):
    await pg_job_store.close_connection()
    # Well we didn't crash. Great.


async def test_stop_no_connection(pg_job_store):
    pg_job_store.stop()
    # Well we didn't crash. Great.


async def test_get_connection_called_twice(pg_job_store):
    conn1 = await pg_job_store.get_connection()
    assert not conn1.closed
    conn2 = await pg_job_store.get_connection()
    assert conn2 is conn1


async def test_get_connection_after_close(pg_job_store):
    conn1 = await pg_job_store.get_connection()
    assert not conn1.closed
    await pg_job_store.close_connection()
    conn2 = await pg_job_store.get_connection()
    assert not conn2.closed
    assert conn2 is not conn1


async def test_get_connection_no_psycopg2_adapter_registration(pg_job_store, mocker):
    register_adapter = mocker.patch("psycopg2.extensions.register_adapter")
    await pg_job_store.get_connection()
    assert not register_adapter.called