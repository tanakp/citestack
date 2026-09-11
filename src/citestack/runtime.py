"""Request context and bounded inference execution with honest cancellation semantics."""

import asyncio
import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor

request_id_var = contextvars.ContextVar("request_id", default="cli")
deadline_var = contextvars.ContextVar("request_deadline", default=None)
tenant_id_var = contextvars.ContextVar("tenant_id", default="default")


def remaining_seconds(default: float) -> float:
    deadline = deadline_var.get()
    return default if deadline is None else max(0.0, min(default, deadline - time.monotonic()))


class Overloaded(Exception):
    pass


class InferenceExecutor:
    """No unbounded submission queue. Timed-out callers do not release active worker slots."""

    def __init__(self, capacity: int):
        self.pool = ThreadPoolExecutor(
            max_workers=capacity, thread_name_prefix="citestack-inference"
        )
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.active = 0
        self.draining = False

    async def run(self, function, *args):
        with self.lock:
            if self.draining or not self.slots.acquire(blocking=False):
                raise Overloaded
            self.active += 1
            try:
                context = contextvars.copy_context()
                future = self.pool.submit(context.run, function, *args)
            except BaseException:
                self.active -= 1
                self.slots.release()
                raise

        def done(_):
            with self.lock:
                self.active -= 1
                self.slots.release()

        future.add_done_callback(done)
        wrapped = asyncio.wrap_future(future)
        # Consume eventual exceptions even if the caller times out or disconnects.
        wrapped.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None
        )
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout=remaining_seconds(120))

    def close(self):
        with self.lock:
            self.draining = True
        self.pool.shutdown(wait=False, cancel_futures=True)
