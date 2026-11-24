import time
import logging
import threading
from queue import Queue, Empty
from .Read_config import config_read 
logger = logging.getLogger(__name__)
cr = config_read()
config_max_workers = int(cr.max_threads("Max_Threads"))
MAX_WORKERS = config_max_workers

# ─────────────────────────────────────────────────────────────────
# Log capture
# ─────────────────────────────────────────────────────────────────
class _CaptureFilter(logging.Filter):
    """
    Added to every handler before workers start.
    - Worker / active threads : captures the record once, suppresses
                                it from all real handlers.
    - Parent thread           : returns True — logs pass through normally.
    """
    def __init__(self):
        super().__init__()
        self._local    = threading.local()
        self._registry = {}          # {str(tc_number): [LogRecord, ...]}
        self._active   = set()
        self._lock     = threading.Lock()
    def activate(self, tc_number):
        self._local.records   = []
        self._local.tc_number = str(tc_number)
        with self._lock:
            self._active.add(threading.current_thread().ident)
    def deactivate(self):
        tc_number = getattr(self._local, "tc_number", None)
        records   = list(getattr(self._local, "records", []))
        with self._lock:
            self._active.discard(threading.current_thread().ident)
            if tc_number is not None:
                self._registry[tc_number] = records
        self._local.records = []
    def filter(self, record):
        if threading.current_thread().ident in self._active:
            if not hasattr(self._local, "records"):
                self._local.records = []
            # Guard: filter() is called once per handler per record.
            # Without this the same record is appended N times.
            if not getattr(record, "_captured", False):
                record._captured = True
                self._local.records.append(record)
            return False        # suppress from real handlers during execution
        return True
    def get_records(self, tc_number):
        return self._registry.get(str(tc_number), [])
    def sorted_records(self):
        def _key(item):
            try:
                # key format: "Module|TCNumber"
                return int(item[0].split("|")[-1])
            except (ValueError, TypeError):
                return float("inf")
        return sorted(self._registry.items(), key=_key)

# ─────────────────────────────────────────────────────────────────
# Replay helpers
# ─────────────────────────────────────────────────────────────────
def _replay_console(record, stream_handlers):
    """Emit one record to console handlers with current wall-clock timestamp."""
    now = time.time()
    record.created         = now
    record.msecs           = (now - int(now)) * 1000
    record.relativeCreated = (now - logging._startTime) * 1000
    for h in stream_handlers:
        h.handle(record)

def _replay_files(capture, file_handlers):
    """Emit all captured records to file handlers sorted by TC number."""
    for _tc, records in capture.sorted_records():
        for record in records:
            for h in file_handlers:
                h.handle(record)

# ─────────────────────────────────────────────────────────────────
# Thread pool
# ─────────────────────────────────────────────────────────────────
def run_parallel(rules, worker_fn, max_workers=MAX_WORKERS, log_id_by_module=None):
    """
    Executes worker_fn(rule, lock, counters, log_id_by_module) for each rule.
    Console output  — real-time, in completion order (as each TC finishes).
    Log file output — written after all TCs finish, sorted by TC number.
    Args:
        rules            : list of test-case rule dicts
        worker_fn        : self._run_single
        max_workers      : thread pool size (1 = sequential)
        log_id_by_module : pre-fetched sequence IDs {module: id}
    Returns:
        counters dict {pass, fail, skipped, times}
    """
    if not rules:
        return {"pass": 0, "fail": 0, "skipped": 0, "times": []}
    max_red = 0
    max_workers = int(max_workers)
 
    if max_workers > MAX_WORKERS:
        logger.info(f"Max Threads Set at: {MAX_WORKERS}, Requested Threads: {max_workers}. "
                    f"Executing Process with {MAX_WORKERS} threads.")
        max_workers = MAX_WORKERS
        max_red += 1
    def _tc_key(r):
        try:
            return int(r.get("TestCaseNumber", 0))
        except (ValueError, TypeError):
            return float("inf")
    sorted_rules     = sorted(rules, key=_tc_key)
    lock             = threading.Lock()
    counters         = {"pass": 0, "fail": 0, "skipped": 0, "times": []}
    log_id_by_module = dict(log_id_by_module) if log_id_by_module else {}
    errors           = []
    # ── Split handlers ────────────────────────────────────────────
    capture       = _CaptureFilter()
    root_logger   = logging.getLogger()
    real_handlers = list(root_logger.handlers)
    stream_handlers = [h for h in real_handlers
                       if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, logging.FileHandler)]
    file_handlers   = [h for h in real_handlers
                       if isinstance(h, logging.FileHandler)]
    for h in real_handlers:
        h.addFilter(capture)
    # ── Sequential path (max_workers == 1) ───────────────────────
    if max_workers <= 1:
        for rule in sorted_rules:
            tc_key = f"{rule.get('Module', '')}|{rule.get('TestCaseNumber', id(rule))}"
            capture.activate(tc_key)
            try:
                worker_fn(rule, lock, counters, log_id_by_module)
            except Exception as e:
                with lock:
                    errors.append((rule, e))
            finally:
                capture.deactivate()
                # Stream to console immediately as each TC finishes
                for record in capture.get_records(tc_key):
                    _replay_console(record, stream_handlers)
        for h in real_handlers:
            h.removeFilter(capture)
        _replay_files(capture, file_handlers)
        for rule, exc in errors:
            logger.exception(
                f"Unhandled error in TestCase {rule.get('TestCaseNumber')}: {exc}"
            )
        return counters
    # ── Parallel path ─────────────────────────────────────────────
    completion_queue = Queue()   # workers push tc_number when done
    work_queue = Queue()
    for rule in sorted_rules:
        work_queue.put(rule)
    def _worker():
        while True:
            try:
                rule = work_queue.get_nowait()
            except Empty:
                break
            tc_key = f"{rule.get('Module', '')}|{rule.get('TestCaseNumber', id(rule))}"
            capture.activate(tc_key)
            try:
                worker_fn(rule, lock, counters, log_id_by_module)
            except Exception as e:
                with lock:
                    errors.append((rule, e))
            finally:
                capture.deactivate()
                completion_queue.put(tc_key)      # ← signals console replay
                work_queue.task_done()
    n_threads = int(min(max_workers, len(sorted_rules)))
    threads   = [threading.Thread(target=_worker, daemon=True)
                 for _ in range(n_threads)]
    for t in threads:
        t.start()
    if max_workers > 1 and max_red == 0:
    	logger.info(
        f"Parallel execution started — {n_threads} active thread(s) "
        f"(requested: {max_workers}, test cases: {len(sorted_rules)}).")
    elif max_workers == 1 and max_red == 0: 
        logger.info(f"Sequential execution started - {n_threads} active thread"
                    f" test cases:{len(sorted_rules)}")
    else:
        pass
 

    # ── Console: stream in completion order ───────────────────────
    for _ in range(len(sorted_rules)):
        tc_number = completion_queue.get()
        for record in capture.get_records(tc_number):
            _replay_console(record, stream_handlers)
    for t in threads:
        t.join()
    # ── Files: write in TC number order ───────────────────────────
    for h in real_handlers:
        h.removeFilter(capture)
    _replay_files(capture, file_handlers)
    for rule, exc in errors:
        logger.exception(
            f"Unhandled error in TestCase {rule.get('TestCaseNumber')}: {exc}"
        )
    return counters