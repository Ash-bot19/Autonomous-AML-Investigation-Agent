"""
AML Investigation Agent — Startup Supervisor

Implements the Demo-Ready / One-Command Startup Standard from CLAUDE.md.

Usage:
    python scripts/start.py

Contract:
    1. Health-check all infra services (postgres, redis, kafka) — 5 retries, exponential backoff.
       On final failure: print [FATAL] message and exit 1.
    2. Run Alembic migrations after all infra is healthy (idempotent).
       On failure: print [FATAL] message and exit 1.
    3. Launch all app processes as subprocesses, each with its own log file under logs/.
    4. Print startup summary once everything is live.
    5. Stay alive and supervise. If any subprocess dies, restart once.
       If it dies again, print [FATAL] and stop supervising that process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
LOGS_DIR = ROOT / "logs"

# Infra health-check config
RETRIES = 5
BACKOFF_BASE = 2  # seconds — wait = BACKOFF_BASE ** attempt (1, 2, 4, 8, 16)

# App processes to supervise: (name, command_list, log_filename)
APP_PROCESSES: list[tuple[str, list[str], str]] = [
    ("api", [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"], "api.log"),
    ("ui", [sys.executable, "-m", "streamlit", "run", str(ROOT / "ui" / "app.py"), "--server.port", "8501", "--server.headless", "true"], "ui.log"),
    ("kafka-consumer", [sys.executable, str(ROOT / "kafka" / "consumer.py")], "kafka_consumer.log"),
]

# URL shown in startup summary per process name
PROCESS_URLS: dict[str, str] = {
    "api": "http://localhost:8000/docs",
    "ui": "http://localhost:8501",
    "kafka-consumer": "consuming aml-flagged-transactions",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env from project root if present."""
    env_path = ROOT / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path)


def _ensure_logs_dir() -> None:
    """Create logs/ directory if it does not exist."""
    LOGS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Infra health checks
# ---------------------------------------------------------------------------

def _check_postgres() -> bool:
    """Return True if PostgreSQL is reachable via psycopg2."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            connect_timeout=5,
        )
        conn.close()
        return True
    except Exception:
        return False


def _check_redis() -> bool:
    """Return True if Redis responds to PING."""
    try:
        import redis as redis_lib
        client = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            socket_connect_timeout=5,
        )
        client.ping()
        return True
    except Exception:
        return False


def _check_kafka() -> bool:
    """Return True if Kafka broker is reachable via admin client."""
    try:
        from kafka import KafkaAdminClient
        admin = KafkaAdminClient(
            bootstrap_servers=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            request_timeout_ms=5000,
            connections_max_idle_ms=6000,
        )
        admin.close()
        return True
    except Exception:
        return False


HEALTH_CHECKS: list[tuple[str, callable]] = [
    ("postgres", _check_postgres),
    ("redis", _check_redis),
    ("kafka", _check_kafka),
]


def _wait_for_service(name: str, check_fn: callable) -> bool:
    """
    Retry check_fn up to RETRIES times with exponential backoff.
    Returns True on success. Prints [FATAL] and returns False after final failure.
    """
    for attempt in range(1, RETRIES + 1):
        if check_fn():
            return True
        if attempt < RETRIES:
            wait = BACKOFF_BASE ** attempt
            print(f"[WAIT] {name} not ready (attempt {attempt}/{RETRIES}) — retrying in {wait}s")
            time.sleep(wait)
    print(f"[FATAL] {name} unreachable after {RETRIES} attempts — check docker compose logs {name}")
    return False


def _run_infra_health_checks() -> bool:
    """Check all infra services. Returns False if any fail."""
    all_ok = True
    for name, check_fn in HEALTH_CHECKS:
        ok = _wait_for_service(name, check_fn)
        if ok:
            print(f"[OK]    {name:<10} — healthy")
        else:
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def _run_migrations() -> bool:
    """
    Run `alembic upgrade head`. Idempotent — safe to call on every start.
    Returns True on success. Prints [FATAL] and returns False on failure.
    """
    migration_log = LOGS_DIR / "migration.log"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        with migration_log.open("a") as f:
            f.write(f"\n--- Migration run at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n")
            f.write(result.stdout + result.stderr)
        if result.returncode != 0:
            print(f"[FATAL] Migration failed — {result.stderr.strip()} — see logs/migration.log")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[FATAL] Migration failed — alembic upgrade timed out — see logs/migration.log")
        return False
    except Exception as exc:
        print(f"[FATAL] Migration failed — {exc} — see logs/migration.log")
        return False


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

class ManagedProcess:
    """Wraps a subprocess with name, log file, and restart-once semantics."""

    def __init__(self, name: str, cmd: list[str], log_filename: str) -> None:
        self.name = name
        self.cmd = cmd
        self.log_path = LOGS_DIR / log_filename
        self._proc: subprocess.Popen | None = None
        self._restart_count = 0

    def start(self) -> None:
        log_fh = self.log_path.open("a")
        self._log_fh = log_fh  # store so we can close before next restart
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        self._proc = subprocess.Popen(
            self.cmd,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
        )

    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def exit_code(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    def handle_death(self) -> bool:
        """
        Called when the process has died.
        Returns True if restarted. Returns False if already restarted once (fatal).
        """
        code = self.exit_code()
        if self._restart_count == 0:
            print(f"[WARN]  {self.name} died (exit {code}) — restarting")
            self._restart_count += 1
            if hasattr(self, "_log_fh") and self._log_fh:
                self._log_fh.close()
            self.start()
            return True
        else:
            print(f"[FATAL] {self.name} failed twice — not restarting. Check logs/{self.log_path.name}")
            if hasattr(self, "_log_fh") and self._log_fh:
                self._log_fh.close()
            return False


def _launch_processes() -> list[ManagedProcess]:
    """Launch all app processes and return their ManagedProcess wrappers."""
    procs = []
    for name, cmd, log_filename in APP_PROCESSES:
        mp = ManagedProcess(name, cmd, log_filename)
        mp.start()
        time.sleep(0.3)  # Brief pause to let process start before checking
        procs.append(mp)
    return procs


def _startup_summary(procs: list[ManagedProcess]) -> None:
    """Print the startup summary block."""
    print("")
    print("[OK]    postgres        — healthy")
    print("[OK]    redis           — healthy")
    print("[OK]    kafka           — healthy")
    for mp in procs:
        alive = mp.is_alive()
        code = mp.exit_code()
        url = PROCESS_URLS.get(mp.name, "")
        if alive:
            status = url if url else "running"
        elif code == 0:
            status = f"{url} (exited 0)" if url else "started (exited 0)"
        else:
            status = f"ERROR (exit {code}) — see logs/{mp.log_path.name}"
        print(f"[OK]    {mp.name:<15} — {status}")
    print("")


def _supervise(procs: list[ManagedProcess]) -> None:
    """
    Main supervision loop. Runs until all processes have failed twice
    or KeyboardInterrupt.
    """
    active = list(procs)
    try:
        while active:
            time.sleep(2)
            still_active = []
            for mp in active:
                if mp.is_alive():
                    still_active.append(mp)
                else:
                    code = mp.exit_code()
                    if code == 0:
                        # Clean exit (Phase 1 stubs do this) — don't restart
                        pass
                    else:
                        restarted = mp.handle_death()
                        if restarted:
                            still_active.append(mp)
                        # else: fatal, drop from active list
            active = still_active
    except KeyboardInterrupt:
        print("\n[INFO]  Received SIGINT — shutting down")
        for mp in active:
            if mp._proc and mp.is_alive():
                mp._proc.terminate()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _load_env()
    _ensure_logs_dir()

    print("[INFO]  Starting AML Investigation Agent")
    print("[INFO]  Checking infrastructure services...\n")

    # Step 1: Infra health checks
    if not _run_infra_health_checks():
        sys.exit(1)

    # Step 2: Run migrations
    print("\n[INFO]  Running database migrations...")
    if not _run_migrations():
        sys.exit(1)
    print("[OK]    migrations — applied")

    # Step 3: Launch app processes
    print("\n[INFO]  Starting application processes...")
    procs = _launch_processes()

    # Step 4: Startup summary
    _startup_summary(procs)

    # Step 5: Supervise
    _supervise(procs)


if __name__ == "__main__":
    main()
