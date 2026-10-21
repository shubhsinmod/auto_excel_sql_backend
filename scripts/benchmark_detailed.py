import time
from sqlalchemy import text
from app.database.connection import get_engine
from app.services.excel_service import excel_service
from app.migration.engine import MigrationEngine

def benchmark():
    upload_id = "upload_2f4b65884428"
    session = excel_service.get_upload_session(upload_id)
    if not session:
        print(f"Session {upload_id} not found!")
        return

    from app.services.file_tracking_service import file_tracking_service
    file_id = session.get("fileId")
    file_id = file_tracking_service.ensure_valid_file_id(file_id, session.get("originalFilename") or "benchmark.csv")
    session["fileId"] = file_id

    print(f"Benchmarking upload {upload_id} ({session.get('rowCount')} rows, FileID={file_id})...")

    table_times = {}
    stage_times = {}
    t_start_all = time.time()
    last_t = t_start_all
    current_tbl = None

    def _prog(tbl, done, total, ins, reus, t_idx, n_tbls):
        nonlocal last_t, current_tbl
        if current_tbl is None:
            stage_times["pre_migration_setup"] = time.time() - t_start_all
            current_tbl = tbl
            last_t = time.time()
            print(f"Pre-migration setup took {stage_times['pre_migration_setup']:.2f}s")
            return

        if tbl != current_tbl:
            now = time.time()
            table_times[current_tbl] = now - last_t
            print(f"Table '{current_tbl}' completed in {table_times[current_tbl]:.2f}s")
            current_tbl = tbl
            last_t = now

    res = MigrationEngine.execute_migration(
        upload_id,
        progress_callback=_prog,
        duplicate_action="override"
    )
    if current_tbl:
        table_times[current_tbl] = time.time() - last_t
        print(f"Table '{current_tbl}' completed in {table_times[current_tbl]:.2f}s")

    total_t = time.time() - t_start_all
    print(f"\n=== DETAILED BENCHMARK BREAKDOWN ===")
    print(f"Total Elapsed Time: {total_t:.2f}s")
    for st, s_sec in stage_times.items():
        print(f"  Stage '{st}': {s_sec:.2f}s")
    for tbl, t_sec in table_times.items():
        print(f"  Table '{tbl}': {t_sec:.2f}s")
    accounted = sum(stage_times.values()) + sum(table_times.values())
    print(f"  Post-migration commit & wrap-up: {total_t - accounted:.2f}s")
    print(f"Results: inserted={res.total_inserted}, reused={res.total_reused}, updated={res.total_updated}")

if __name__ == '__main__':
    benchmark()
