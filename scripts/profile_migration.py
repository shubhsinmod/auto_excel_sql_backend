import time
from fastapi.testclient import TestClient
from app.database.connection import get_engine
from sqlalchemy import text
from main import app

client = TestClient(app)

def profile_migration():
    sample_csv = "uploads/upload_337c05a4d8d6.csv"
    with open(sample_csv, "rb") as f:
        resp = client.post(
            "/api/excel/upload",
            files={"file": ("profile_test.csv", f, "text/csv")}
        )
    upload_id = resp.json()["uploadId"]
    client.post(f"/api/migration/process/{upload_id}")
    
    t0 = time.time()
    exec_resp = client.post(f"/api/migration/execute/{upload_id}?duplicateAction=override")
    t1 = time.time()
    data = exec_resp.json()
    print(f"Total execute time: {t1 - t0:.2f}s")
    print("Table stats:")
    for tbl, stat in data.get("tables", {}).items():
        print(f"  {tbl}: inserted={stat.get('inserted')} reused={stat.get('reused')} updated={stat.get('updated')} skipped={stat.get('skipped')}")

if __name__ == '__main__':
    profile_migration()
