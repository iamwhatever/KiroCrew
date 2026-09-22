"""Cancel must reach a manual run from the instant Run accepted it.

``POST /api/crons/{id}/run`` stores the task it creates in ``_running_tasks``
and returns; ``CronService.run_job`` then sits in its first ``await`` -- the
offloaded ``_synced_snapshot``, up to a full store-lock spin. ``cancel()``'s
guard reads only ``_executing``, so a claim that reaches ``_executing`` only
after that await opens a window in which ``POST /api/crons/{id}/cancel``
answers 409 "job is not running" while a second Run answers 409 "job is
already running" about the same job, and the run then executes anyway. The
claim is therefore taken synchronously while ``run_job(job_id)`` is evaluated,
before the route's ``create_task`` has scheduled anything.

The window is only open while the snapshot is held, which no product route
holds on demand, so this harness is the bar: a gate parks ``run_job``'s
refresh in its worker thread and the requests land inside the window.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.cron import CronJob, CronService, _RunMarkers
from kiro_crew.cron_inflight import clear_marker as _clear_marker
from kiro_crew.dashboard.handlers.cron import api_cron_cancel, api_cron_run


def _make_state(svc: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = svc
    state.push_refresh = MagicMock()
    return state


def _make_app(state: MagicMock) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/run", api_cron_run)
    app.router.add_post("/api/crons/{job_id}/cancel", api_cron_cancel)
    return app


async def _service(tmp_path: Path, on_job) -> tuple[CronService, CronJob]:
    svc = CronService(base_dir=tmp_path, on_job=on_job)
    svc._sessions = None
    job = await svc.add_job_async("etl job", "do something", every_secs=3600)
    return svc, job


class _SnapshotGate:
    """Let the route's lookup through; park ``run_job``'s refresh until released.

    Runs in the ``asyncio.to_thread`` worker. The route resolves the job through
    ``get_job_async`` first (call 1); ``run_job``'s own refresh is call 2 and is
    held until :attr:`release` is set, which keeps the claim window open for as
    long as the test needs to land requests inside it. ``park_call`` names a
    later refresh instead when the parked run is not the first one.
    """

    def __init__(self, svc: CronService, park_call: int = 2) -> None:
        self._svc = svc
        self._park_call = park_call
        self.calls = 0
        self.parked = threading.Event()
        self.release = threading.Event()

    def __call__(self, include_disabled: bool = True) -> list[CronJob]:
        self.calls += 1
        if self.calls == self._park_call:
            self.parked.set()
            assert self.release.wait(timeout=10), "the snapshot gate was never released"
        return list(self._svc._jobs)


class _FirstCallGate:
    """Park the first call of a worker-thread function until released; later calls pass.

    Wraps a function ``_run_job_isolated``'s ``finally`` offloads through
    ``asyncio.to_thread`` -- ``cron_inflight.clear_marker`` before its marker
    pops, ``_merge_job_result`` after them -- so that run's finalizer stays
    pending exactly there for as long as the test holds :attr:`release`.
    """

    def __init__(self, delegate: Callable[..., Any]) -> None:
        self._delegate = delegate
        self.calls = 0
        self.parked = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            self.parked.set()
            assert self.release.wait(timeout=10), "the finalizer gate was never released"
        return self._delegate(*args)


class _EachCallGate:
    """Park every call of a worker-thread function until that call is released.

    Same seam as :class:`_FirstCallGate`, for a test that needs two runs'
    finalizers pending in ``cron_inflight.clear_marker`` at once and released in
    an order of its choosing: call ``n`` (1-based) waits on ``release(n)`` and
    reports itself through ``parked(n)``.
    """

    def __init__(self, delegate: Callable[..., Any]) -> None:
        self._delegate = delegate
        self._lock = threading.Lock()
        self._parked: dict[int, threading.Event] = {}
        self._release: dict[int, threading.Event] = {}
        self._open = False
        self.calls = 0

    def parked(self, call: int) -> threading.Event:
        with self._lock:
            return self._parked.setdefault(call, threading.Event())

    def release(self, call: int) -> threading.Event:
        with self._lock:
            return self._release.setdefault(call, threading.Event())

    def release_all(self) -> None:
        """Release every parked call and let any later call straight through."""
        with self._lock:
            self._open = True
            events = list(self._release.values())
        for event in events:
            event.set()

    def __call__(self, *args: Any) -> Any:
        with self._lock:
            self.calls += 1
            call = self.calls
            parked = not self._open
        if parked:
            self.parked(call).set()
            assert self.release(call).wait(timeout=10), f"finalizer call {call} was never released"
        return self._delegate(*args)


async def _run_inside_the_gap(svc: CronService, app: web.Application, job_id: str) -> web.Response:
    """Drive the real Run route without yielding to the loop.

    A gap between a release and a stale finalizer can be one loop iteration
    wide, and the route's fresh-store lookup is an executor hop that costs that
    iteration, so the lookup is made await-free here; everything after it (the
    guard, the claim, the tracked task) is the route's own code.
    """

    async def await_free_lookup(job_id: str) -> CronJob | None:
        return next((j for j in svc._jobs if j.id == job_id), None)

    with patch.object(svc, "get_job_async", await_free_lookup):
        return await api_cron_run(
            make_mocked_request(
                "POST", f"/api/crons/{job_id}/run", match_info={"job_id": job_id}, app=app
            )
        )


class TestCancelInsideTheClaimWindow:
    @pytest.mark.asyncio
    async def test_cancel_reaches_a_manual_run_parked_in_its_store_refresh(
        self, tmp_path: Path
    ) -> None:
        """Inside the window Cancel finds the run; a late claim answers 409 "job is not running"."""
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            return "ran"

        svc, job = await _service(tmp_path, on_job)
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        wrapper: asyncio.Task[bool] | None = None
        with patch.object(svc, "_synced_snapshot", gate):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    wrapper = svc._running_tasks[job.id]
                    assert await asyncio.to_thread(
                        gate.parked.wait, 5
                    ), "run_job never reached its store refresh"

                    # Inside the window Run says the job is already running ...
                    again = await client.post(f"/api/crons/{job.id}/run")
                    assert again.status == 409
                    assert (await again.json())["error"] == "job is already running"

                    # ... so Cancel must be able to reach that same run.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel.json()
                    assert cancel.status == 200, (
                        f"Cancel inside the claim window answered {cancel.status} {body!r} "
                        "while Run answered 409 'job is already running' about the same job"
                    )
                    assert body["ok"] is True
            finally:
                gate.release.set()
                if wrapper is not None:
                    await asyncio.gather(wrapper, return_exceptions=True)

        # The pending run was cancelled, not executed, and left no marker behind.
        assert wrapper is not None and wrapper.cancelled()
        assert executed == []
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._cancelled_jobs
        assert job.last_status == "error"
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["trigger"] == "manual"

    @pytest.mark.asyncio
    async def test_a_refresh_returning_during_cancel_teardown_does_not_dispatch(
        self, tmp_path: Path
    ) -> None:
        """cancel() pops the claim first and cancels the task only after its teardown awaits.

        The snapshot is released from INSIDE that gap -- the process-kill call
        cancel() awaits in the executor -- and held there until the wrapper has
        resumed. A wrapper that re-checks its claim returns without dispatching;
        one that trusts the snapshot starts the run cancel() is reporting cancelled.
        """
        dispatched: list[str] = []
        resumed = threading.Event()

        async def fake_run(job: CronJob) -> None:
            dispatched.append(job.id)
            resumed.set()
            await asyncio.Event().wait()  # runs until cancelled

        def fake_kill(job_id: str) -> bool:
            # Executor thread, inside cancel()'s first await: let the snapshot
            # return now and hold cancel() here until the wrapper has resumed.
            gate.release.set()
            assert resumed.wait(timeout=10), "the wrapper never resumed past its refresh"
            return False

        svc, job = await _service(tmp_path, None)
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        with (
            patch.object(svc, "_synced_snapshot", gate),
            patch.object(svc, "_run_job_isolated", side_effect=fake_run),
            patch("kiro_crew.cron_script.kill_running_process", fake_kill),
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                run = await client.post(f"/api/crons/{job.id}/run")
                assert run.status == 200
                wrapper = svc._running_tasks[job.id]
                wrapper.add_done_callback(lambda _t: resumed.set())
                assert await asyncio.to_thread(
                    gate.parked.wait, 5
                ), "run_job never reached its store refresh"

                cancel = await client.post(f"/api/crons/{job.id}/cancel")
                assert cancel.status == 200
                assert (await cancel.json())["ok"] is True
            await asyncio.gather(wrapper, return_exceptions=True)

        assert (
            dispatched == []
        ), f"the run was dispatched {dispatched!r} while cancel() was tearing it down"
        assert wrapper.done() and not wrapper.cancelled() and wrapper.result() is False
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._cancelled_jobs
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_a_cancelled_wrappers_late_teardown_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted while cancel() tears down the previous one keeps its claim.

        cancel() pops ``_running_tasks``, cancels the parked wrapper and discards
        ``_executing`` in one synchronous step, then yields to persist. The
        cancelled wrapper's teardown -- the ``except`` around its refresh -- runs
        one loop iteration later, so a Run landing in that gap passes the route's
        guard and claims the job first. The teardown must see that the stored
        claim is not its own and leave it alone; releasing it anyway erases
        the replacement run's ``_executing`` entry and meta, so that run is either
        dropped by its own claim re-check after the route answered "started", or
        executes with Cancel answering 409 "job is not running".

        The test's own handle is queued no later than the wrapper's cancellation
        wakeup, so spinning ``sleep(0)`` until the first run's tracking is gone
        lands inside the gap deterministically. The replacement is driven through
        the real route from there; its fresh-store lookup is replaced by an
        await-free one, because a real executor hop costs the one iteration the
        gap is wide.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        gate = _SnapshotGate(svc)
        state = _make_state(svc)
        app = _make_app(state)
        with patch.object(svc, "_synced_snapshot", gate):
            try:
                async with TestClient(TestServer(app)) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    first = svc._running_tasks[job.id]
                    assert await asyncio.to_thread(
                        gate.parked.wait, 5
                    ), "run_job never reached its store refresh"

                    cancelling = asyncio.ensure_future(client.post(f"/api/crons/{job.id}/cancel"))
                    deadline = time.monotonic() + 5
                    while job.id in svc._executing or job.id in svc._running_tasks:
                        assert time.monotonic() < deadline, "cancel() never dropped the first run"
                        await asyncio.sleep(0)
                    assert (
                        not first.done()
                    ), "the cancelled wrapper tore down before cancel() yielded"

                    # Inside the gap: the replacement Run is accepted and claims the job.
                    replacement = await _run_inside_the_gap(svc, app, job.id)
                    assert replacement.status == 200
                    second = svc._running_tasks[job.id]
                    assert second is not first
                    claim = svc._job_run_meta[job.id]
                    assert claim[1] == "manual"

                    # Now the first wrapper's teardown runs, with a claim that is not its own.
                    await asyncio.gather(first, return_exceptions=True)
                    assert first.cancelled()
                    assert svc.is_running(job.id) and svc._job_run_meta.get(job.id) is claim, (
                        "the cancelled wrapper's teardown erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}"
                    )
                    cancel = await cancelling
                    assert cancel.status == 200
                    assert (await cancel.json())["ok"] is True

                    # The replacement run executes, and Cancel reaches it.
                    await asyncio.wait_for(started.wait(), 5)
                    assert executed == [job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel_again.json()
                    assert (
                        cancel_again.status == 200
                    ), f"Cancel answered {cancel_again.status} {body!r} about the replacement run"
                    assert body["ok"] is True
                    await asyncio.gather(second, return_exceptions=True)
            finally:
                gate.release.set()

        assert second.done() and not second.cancelled() and second.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._cancelled_jobs
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_finalizer_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted while a cancelled run is still finalizing keeps its claim.

        cancel() releases the run it found -- meta, start stamps, ``_executing``,
        ``_running_tasks`` -- and that run's own ``finally`` then spends a full
        executor round trip (``cron_inflight.clear_marker``) before its marker
        pops, so a Run accepted through the real route during that trip claims
        the job first. The prior run's finalizer must leave that claim alone:
        popping it anyway drops the replacement's ``_executing`` entry, run meta
        and tracked task, so its claim re-check returns without dispatching after
        the route answered "started" -- or, past the re-check, it runs with Cancel
        answering 409 and a further Run accepted beside it.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        # Calls 1-3 pass (the first Run's lookup and refresh, the replacement's
        # lookup); call 4, the replacement's refresh, is parked so its claim
        # re-check runs only after the prior run's finalizer has had its turn.
        snapshot = _SnapshotGate(svc, park_call=4)
        clearing = _FirstCallGate(_clear_marker)
        state = _make_state(svc)
        with (
            patch.object(svc, "_synced_snapshot", snapshot),
            patch("kiro_crew.cron_inflight.clear_marker", clearing),
        ):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    await asyncio.wait_for(started.wait(), 5)
                    prior = svc._running_tasks[job.id]
                    started.clear()

                    # Cancel releases the run and cancels it; its finalizer parks
                    # in the marker clear with its pops still ahead of it.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    assert await asyncio.to_thread(
                        clearing.parked.wait, 5
                    ), "the cancelled run never reached its marker clear"
                    assert not prior.done()
                    assert job.id not in svc._executing
                    assert job.id not in svc._running_tasks

                    # The replacement is accepted through the real route and claims the job.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    assert await asyncio.to_thread(
                        snapshot.parked.wait, 5
                    ), "the replacement run never reached its store refresh"
                    claim = svc._job_run_meta[job.id]
                    assert claim[1] == "manual"
                    tracked = svc._running_tasks[job.id]
                    assert tracked is not prior

                    # Now the prior run's finalizer pops, holding a claim that is not its own.
                    clearing.release.set()
                    await asyncio.gather(prior, return_exceptions=True)
                    assert prior.cancelled()
                    assert (
                        svc.is_running(job.id)
                        and svc._job_run_meta.get(job.id) is claim
                        and svc._running_tasks.get(job.id) is tracked
                    ), (
                        "the prior run's finalizer erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}, "
                        f"tracked={sorted(svc._running_tasks)!r}"
                    )

                    # The replacement dispatches, and Cancel reaches it.
                    snapshot.release.set()
                    await asyncio.wait_for(started.wait(), 5)
                    assert executed == [job.id, job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    body = await cancel_again.json()
                    assert (
                        cancel_again.status == 200
                    ), f"Cancel answered {cancel_again.status} {body!r} about the replacement run"
                    assert body["ok"] is True
                    await asyncio.gather(tracked, return_exceptions=True)
            finally:
                clearing.release.set()
                snapshot.release.set()

        assert tracked.done() and not tracked.cancelled() and tracked.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        assert job.id not in svc._job_start_times
        assert job.id not in svc._job_start_monotonic
        assert job.id not in svc._cancelled_jobs
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_a_prior_runs_late_finalizer_consumes_only_its_own_cancel_marker(
        self, tmp_path: Path
    ) -> None:
        """Two cancellations in flight at once each finalize as cancelled.

        cancel() marks the run it releases, and that run's ``finally`` consumes
        the marker only after a full executor round trip (``clear_marker``).
        While the first run's finalizer is still in that trip, a replacement Run
        is accepted through the real route, dispatches, and is cancelled too;
        then the first finalizer resumes. A marker keyed by job id alone is ONE
        marker for both cancellations: the first finalizer consumes it, the
        replacement's finalizer finds none and treats its run as completed --
        merging the job and appending a ``failure`` row (the cancel message as
        its summary) after the ``cancelled`` row cancel() already wrote for it.
        """
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        clearing = _EachCallGate(_clear_marker)
        state = _make_state(svc)
        with patch("kiro_crew.cron_inflight.clear_marker", clearing):
            try:
                async with TestClient(TestServer(_make_app(state))) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    await asyncio.wait_for(started.wait(), 5)
                    prior = svc._running_tasks[job.id]
                    prior_claim = svc._job_run_meta[job.id]
                    started.clear()

                    # Cancel the first run: its finalizer parks in the marker
                    # clear with its marker read still ahead of it.
                    cancel = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel.status == 200
                    assert await asyncio.to_thread(
                        clearing.parked(1).wait, 5
                    ), "the first run never reached its marker clear"
                    assert not prior.done()
                    assert job.id in svc._cancelled_jobs

                    # The replacement is accepted, dispatches, and is cancelled too.
                    replacement = await client.post(f"/api/crons/{job.id}/run")
                    assert replacement.status == 200
                    await asyncio.wait_for(started.wait(), 5)
                    assert executed == [job.id, job.id]
                    later = svc._running_tasks[job.id]
                    assert later is not prior
                    later_claim = svc._job_run_meta[job.id]
                    cancel_again = await client.post(f"/api/crons/{job.id}/cancel")
                    assert cancel_again.status == 200
                    assert (await cancel_again.json())["ok"] is True
                    assert await asyncio.to_thread(
                        clearing.parked(2).wait, 5
                    ), "the replacement run never reached its marker clear"
                    assert not later.done()
                    runs, total = await svc._history.get_job_history(job.id)
                    assert total == 2
                    assert [r["status"] for r in runs] == ["cancelled", "cancelled"]

                    # The first finalizer resumes and consumes ITS cancellation;
                    # the replacement's marker has to survive it.
                    clearing.release(1).set()
                    await asyncio.gather(prior, return_exceptions=True)
                    assert prior.cancelled()
                    assert job.id in svc._cancelled_jobs, (
                        "the prior run's finalizer consumed the replacement run's cancel "
                        "marker, so the replacement will finalize as if it had completed"
                    )
                    assert svc._cancelled_jobs.has(job.id, later_claim) and not (
                        svc._cancelled_jobs.has(job.id, prior_claim)
                    ), "the prior run's finalizer consumed a marker that was not its own"

                    # Then the replacement's finalizer: cancelled, not completed.
                    clearing.release(2).set()
                    await asyncio.gather(later, return_exceptions=True)
                    assert later.cancelled()
            finally:
                clearing.release_all()

        assert job.id not in svc._cancelled_jobs
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        runs, total = await svc._history.get_job_history(job.id)
        assert [r["status"] for r in runs] == ["cancelled", "cancelled"], (
            "the replacement run appended a row after its cancelled row: "
            f"history={[(r['status'], r['summary']) for r in runs]!r}"
        )
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_the_manual_wrappers_backstop_leaves_a_replacement_claim_alone(
        self, tmp_path: Path
    ) -> None:
        """A Run accepted between a run's own release and its wrapper's resume keeps its claim.

        ``_run_job_isolated`` releases the run's claim in its finally and then
        awaits its result merge and history append; the manual wrapper awaiting
        that task resumes only once it is done. A Run accepted in between claims
        the job, and the wrapper's backstop -- there for a finally cut short --
        must not discard that run's ``_executing`` entry or its tracked task.
        """
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            return "ran"

        svc, job = await _service(tmp_path, on_job)
        # Calls 1-2 are the first Run's lookup and refresh; the replacement's
        # lookup is await-free (see _run_inside_the_gap), so its refresh is call 3.
        gate = _SnapshotGate(svc, park_call=3)
        state = _make_state(svc)
        app = _make_app(state)
        with patch.object(svc, "_synced_snapshot", gate):
            try:
                async with TestClient(TestServer(app)) as client:
                    run = await client.post(f"/api/crons/{job.id}/run")
                    assert run.status == 200
                    first = svc._running_tasks[job.id]
                    deadline = time.monotonic() + 5
                    while job.id in svc._executing or job.id in svc._running_tasks:
                        assert time.monotonic() < deadline, "the first run never released its claim"
                        await asyncio.sleep(0)
                    assert not first.done(), "the wrapper resumed before its run's finally ended"

                    # Inside the gap: the replacement Run is accepted and claims the job.
                    replacement = await _run_inside_the_gap(svc, app, job.id)
                    assert replacement.status == 200
                    second = svc._running_tasks[job.id]
                    claim = svc._job_run_meta[job.id]
                    assert await asyncio.to_thread(
                        gate.parked.wait, 5
                    ), "the replacement run never reached its store refresh"

                    # Now the first wrapper's backstop runs, holding a claim that is not its own.
                    await asyncio.gather(first, return_exceptions=True)
                    assert first.result() is True
                    assert (
                        svc.is_running(job.id)
                        and svc._job_run_meta.get(job.id) is claim
                        and svc._running_tasks.get(job.id) is second
                    ), (
                        "the first wrapper's backstop erased the replacement run's claim: "
                        f"_executing={sorted(svc._executing)!r}, "
                        f"run meta={svc._job_run_meta.get(job.id)!r}, "
                        f"tracked={sorted(svc._running_tasks)!r}"
                    )
                    gate.release.set()
                    await asyncio.gather(second, return_exceptions=True)
            finally:
                gate.release.set()

        assert executed == [job.id, job.id]
        assert second.result() is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._job_run_meta
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 2
        assert [r["status"] for r in runs] == ["success", "success"]
        assert [r["trigger"] for r in runs] == ["manual", "manual"]

    @pytest.mark.asyncio
    async def test_cancel_after_the_claim_still_cancels_the_running_job(
        self, tmp_path: Path
    ) -> None:
        """The happy path -- Run, then Cancel once the run is executing -- is unchanged."""
        started = asyncio.Event()
        executed: list[str] = []

        async def on_job(job: CronJob) -> str | None:
            executed.append(job.id)
            started.set()
            await asyncio.Event().wait()  # runs until cancelled
            return None

        svc, job = await _service(tmp_path, on_job)
        state = _make_state(svc)
        async with TestClient(TestServer(_make_app(state))) as client:
            run = await client.post(f"/api/crons/{job.id}/run")
            assert run.status == 200
            wrapper = svc._running_tasks[job.id]
            await asyncio.wait_for(started.wait(), 5)
            assert svc.is_running(job.id)

            cancel = await client.post(f"/api/crons/{job.id}/cancel")
            assert cancel.status == 200
            assert (await cancel.json())["ok"] is True
            await asyncio.gather(wrapper, return_exceptions=True)

        assert executed == [job.id]
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks
        assert job.id not in svc._cancelled_jobs
        assert (job.last_error or "").startswith("Cancelled by user")
        runs, total = await svc._history.get_job_history(job.id)
        assert total == 1
        assert runs[0]["status"] == "cancelled"
        assert runs[0]["trigger"] == "manual"


class TestRunJobClaim:
    @pytest.mark.asyncio
    async def test_claim_is_taken_when_run_job_is_called(self, tmp_path: Path) -> None:
        """``_executing`` and the manual run meta exist before the first await."""
        svc, job = await _service(tmp_path, None)

        async def fake_run(job: CronJob) -> None:
            pass

        with patch.object(svc, "_run_job_isolated", side_effect=fake_run):
            before = time.time()
            pending = svc.run_job(job.id)
            assert svc.is_running(job.id)
            started_at, trigger = svc._job_run_meta[job.id]
            assert trigger == "manual"
            assert before <= started_at <= time.time()
            assert await pending is True
        assert job.id not in svc._executing
        assert job.id not in svc._running_tasks

    @pytest.mark.asyncio
    async def test_claim_is_released_when_the_store_has_no_such_job(self, tmp_path: Path) -> None:
        svc, _job = await _service(tmp_path, None)

        pending = svc.run_job("ghost")
        assert svc.is_running("ghost")
        assert await pending is False
        assert "ghost" not in svc._executing
        assert "ghost" not in svc._job_run_meta

    @pytest.mark.asyncio
    async def test_a_job_already_executing_is_refused_and_its_claim_untouched(
        self, tmp_path: Path
    ) -> None:
        svc, job = await _service(tmp_path, None)
        meta = (time.time() - 5, "scheduled")
        svc._executing.add(job.id)
        svc._job_run_meta[job.id] = meta

        assert await svc.run_job(job.id) is False
        assert svc.is_running(job.id)
        assert svc._job_run_meta[job.id] is meta


class TestRunMarkers:
    def test_markers_are_keyed_by_run_identity_not_equality(self) -> None:
        """Two equal meta tuples are two runs: each marker is consumed by its own run only."""
        markers = _RunMarkers()
        first = (1.0, "manual")
        second = (first[0], first[1])  # built at runtime: equal, not the folded constant
        assert first == second and first is not second

        markers.mark("job", first)
        markers.mark("job", first)  # marking the same run twice is one marker
        assert "job" in markers
        assert markers.has("job", first) and not markers.has("job", second)
        assert not markers.has("job", None)

        markers.mark("job", second)
        assert markers.consume("job", first) is True
        assert "job" in markers, "consuming one run's marker removed the other's"
        assert markers.consume("job", first) is False
        assert markers.consume("job", second) is True
        assert "job" not in markers
        assert markers.consume("job", second) is False
        assert markers.consume("other", None) is False
