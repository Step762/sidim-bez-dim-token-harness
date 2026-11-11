"""Monotonic run budget and interruptible synchronous operations on Linux."""

from contextlib import contextmanager
import math
import signal
import threading
import time


DEFAULT_RUN_SECONDS = 3540  # Leave 60 seconds before the official one-hour kill.
DEFAULT_OPERATION_SECONDS = 30


class OperationTimeout(Exception):
    pass


class RuntimeLimits:
    def __init__(self,total=DEFAULT_RUN_SECONDS,operation=DEFAULT_OPERATION_SECONDS,*,started=None):
        if (not math.isfinite(total) or not 0<total<=DEFAULT_RUN_SECONDS
            or not math.isfinite(operation) or operation<=0):
            raise ValueError("time limits must be finite and positive; run limit cannot exceed 3540 seconds")
        self.deadline = (time.monotonic() if started is None else started)+total
        self.operation = operation

    @property
    def remaining(self):
        return max(0,self.deadline-time.monotonic())

    @property
    def expired(self):
        return self.remaining<=0

    @contextmanager
    def sync(self):
        """Only synchronous scopes: an asyncio timeout cannot interrupt those."""
        seconds = min(self.operation,self.remaining)
        if seconds<=0: raise OperationTimeout("run deadline reached")
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal,"setitimer"):
            raise RuntimeError("runtime limits require the Linux main thread")
        previous = signal.getsignal(signal.SIGALRM)
        timer = signal.getitimer(signal.ITIMER_REAL)
        if timer!=(0.0,0.0): raise RuntimeError("another real-time alarm is already active")
        interrupted = False
        def interrupt(signum,frame):
            nonlocal interrupted
            interrupted = True
            raise OperationTimeout("synchronous operation time limit reached")
        signal.signal(signal.SIGALRM,interrupt)
        signal.setitimer(signal.ITIMER_REAL,seconds)
        try:
            try:
                yield
            except BaseException:
                if interrupted: raise OperationTimeout("synchronous operation time limit reached") from None
                raise
            if interrupted: raise OperationTimeout("synchronous operation time limit reached")
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous)
