"""concurrency - shared threading primitives and utilities."""
import threading
from functools import wraps
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)


def locked(method: F) -> F:
    """
    Decorator that wraps every method call with ``with self._lock:``.

    The instance must have a ``_lock`` attribute (RLock or Lock).
    This replaces all manual ``with self._lock:`` boilerplate.

    Example::

        class MyService:
            _lock: ClassVar[threading.RLock] = threading.RLock()

            @locked
            def get_data(self, key: str) -> str: ...

            @locked
            def put_data(self, key: str, value: str) -> None: ...

    The decorator is reentrant-safe (RLock) and costs nothing when
    the lock is not contended.
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper  # type: ignore[return价值]
