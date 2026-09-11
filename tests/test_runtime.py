import asyncio
import threading
import time

import pytest

from citestack.runtime import InferenceExecutor, Overloaded, deadline_var, request_id_var


@pytest.mark.parametrize("cancel", [False, True])
def test_abandoned_worker_keeps_slot_until_actual_completion(cancel):
    started, release = threading.Event(), threading.Event()
    executor = InferenceExecutor(1)

    def work():
        started.set()
        release.wait(3)
        return request_id_var.get()

    async def scenario():
        request_token = request_id_var.set("correlated")
        deadline_token = deadline_var.set(time.monotonic() + 0.05)
        try:
            task = asyncio.create_task(executor.run(work))
            assert await asyncio.to_thread(started.wait, 1)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            assert executor.active == 1
            with pytest.raises(Overloaded):
                await executor.run(lambda: "must not run")
            release.set()
            for _ in range(100):
                if not executor.active:
                    break
                await asyncio.sleep(0.01)
            assert executor.active == 0
            deadline_var.set(time.monotonic() + 1)
            assert await executor.run(lambda: request_id_var.get()) == "correlated"
            executor.close()
            with pytest.raises(Overloaded):
                await executor.run(lambda: None)
        finally:
            release.set()
            executor.close()
            deadline_var.reset(deadline_token)
            request_id_var.reset(request_token)

    asyncio.run(scenario())
