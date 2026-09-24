"""确定性分片落库基准：python -m tests.performance.benchmark_stream_store。"""

from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from session.store import SessionStore
from tests.unit.session.test_stream_store import chunk, seed_legacy


def main():
    for count in (0, 20_000, 40_000):
        with TemporaryDirectory(prefix="bean-stream-benchmark-") as directory:
            path = Path(directory) / "sessions.db"
            seed_legacy(path, count)
            store = SessionStore(path)
            timings = []
            for index in range(count, count + 60):
                started = perf_counter()
                store.append_session_event(chunk(index))
                timings.append((perf_counter() - started) * 1000)
            store.close()
            print(f"history={count}, samples=60, median_ms={median(timings):.3f}, "
                  f"p95_ms={sorted(timings)[56]:.3f}, total_ms={sum(timings):.3f}")


if __name__ == "__main__":
    main()
