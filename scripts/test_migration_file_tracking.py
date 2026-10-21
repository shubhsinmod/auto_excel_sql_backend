import os
import shutil
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.database.connection import get_engine
from app.services.file_tracking_service import file_tracking_service
from main import app

client = TestClient(app)

def run_tests():
    engine = get_engine()
    
    print("=== 0. Preparing Clean Test State ===")
    with engine.begin() as conn:
        for t in ["OrderLineItems", "Settlements", "[Order]", "Bills", "Customer"]:
            conn.execute(text(f"DELETE FROM dbo.{t}"))
        conn.execute(text("DELETE FROM dbo.UploadedFiles"))

    print("\n=== 1. Testing Schema Verification ===")
    file_tracking_service.ensure_schema()
    
    with engine.connect() as conn:
        fks = conn.execute(text("""
            SELECT fk.name, tp.name, tr.name
            FROM sys.foreign_keys fk
            JOIN sys.tables tp ON fk.parent_object_id = tp.object_id
            JOIN sys.tables tr ON fk.referenced_object_id = tr.object_id
            WHERE tr.name = 'UploadedFiles'
        """)).fetchall()
        print("Verified FKs pointing to UploadedFiles:", fks)
        assert len(fks) >= 5, f"Expected at least 5 FKs, found {len(fks)}"

    sample_csv = "uploads/upload_337c05a4d8d6.csv"
    assert os.path.exists(sample_csv), f"Sample CSV {sample_csv} not found"

    print("\n=== 2. Testing File Upload 1 ===")
    with open(sample_csv, "rb") as f:
        resp1 = client.post(
            "/api/excel/upload",
            files={"file": ("sales_october_batch1.csv", f, "text/csv")}
        )
    assert resp1.status_code == 200, f"Upload 1 failed: {resp1.text}"
    data1 = resp1.json()
    upload_id1 = data1["uploadId"]
    file_id1 = data1.get("fileId")
    print(f"Upload 1 success: uploadId={upload_id1}, fileId={file_id1}")
    assert file_id1 is not None and file_id1 > 0, "fileId1 was not returned"

    # Verify UploadedFiles row in DB
    with engine.connect() as conn:
        row1 = conn.execute(
            text("SELECT FileID, FileName, Status FROM dbo.UploadedFiles WHERE FileID = :fid"),
            {"fid": file_id1}
        ).fetchone()
        print("DB row for file 1:", row1)
        assert row1 is not None, "File 1 not found in UploadedFiles"
        assert row1[1] == "sales_october_batch1.csv"
        assert row1[2] == "Processing"

    print("\n=== 3. Testing Step 3 Preview for File 1 ===")
    p_resp1 = client.post(f"/api/migration/process/{upload_id1}")
    assert p_resp1.status_code == 200, f"Preview 1 failed: {p_resp1.text}"
    print("Preview 1 generated successfully")

    print("\n=== 4. Testing Migration Execution for File 1 ===")
    exec_resp1 = client.post(f"/api/migration/execute/{upload_id1}?duplicateAction=override")
    assert exec_resp1.status_code == 200, f"Execute 1 failed: {exec_resp1.text}"
    exec_data1 = exec_resp1.json()
    print(f"Migration 1 status={exec_data1.get('status')}, transaction={exec_data1.get('transaction')}, fileId={exec_data1.get('fileId')}")
    assert exec_data1.get("status") == "COMPLETED"
    assert exec_data1.get("fileId") == file_id1

    # Verify File 1 status in UploadedFiles is Completed
    with engine.connect() as conn:
        status1 = conn.execute(
            text("SELECT Status FROM dbo.UploadedFiles WHERE FileID = :fid"),
            {"fid": file_id1}
        ).scalar()
        print(f"File 1 Status in DB: {status1}")
        assert status1 == "Completed", f"Expected Completed, got {status1}"

        # Verify all destination tables contain FileID = file_id1
        for tbl in ["Customer", "Bills", "Order", "OrderLineItems", "Settlements"]:
            cnt = conn.execute(
                text(f"SELECT COUNT(*) FROM dbo.[{tbl}] WHERE FileID = :fid"),
                {"fid": file_id1}
            ).scalar()
            total_cnt = conn.execute(text(f"SELECT COUNT(*) FROM dbo.[{tbl}]")).scalar()
            print(f"Table [{tbl}]: total rows={total_cnt}, matching FileID({file_id1})={cnt}")
            if total_cnt > 0:
                assert cnt > 0, f"Table [{tbl}] has rows but none with FileID={file_id1}!"

            # Test SQL join with dbo.UploadedFiles
            join_cnt = conn.execute(
                text(f"""
                    SELECT COUNT(*) 
                    FROM dbo.[{tbl}] t
                    INNER JOIN dbo.UploadedFiles u ON t.FileID = u.FileID
                    WHERE u.FileID = :fid
                """),
                {"fid": file_id1}
            ).scalar()
            print(f"Table [{tbl}] JOIN UploadedFiles count: {join_cnt}")
            assert join_cnt == cnt, f"Join count {join_cnt} != {cnt}"

    print("\n=== 5. Testing File Upload 2 (Second upload gets different FileID) ===")
    with open(sample_csv, "rb") as f:
        resp2 = client.post(
            "/api/excel/upload",
            files={"file": ("sales_october_batch2.csv", f, "text/csv")}
        )
    assert resp2.status_code == 200, f"Upload 2 failed: {resp2.text}"
    data2 = resp2.json()
    upload_id2 = data2["uploadId"]
    file_id2 = data2.get("fileId")
    print(f"Upload 2 success: uploadId={upload_id2}, fileId={file_id2}")
    assert file_id2 is not None and file_id2 > file_id1, f"Expected file_id2 > {file_id1}, got {file_id2}"

    print("\n=== 6. Testing Step 3 Preview & Migration for File 2 ===")
    p_resp2 = client.post(f"/api/migration/process/{upload_id2}")
    assert p_resp2.status_code == 200

    exec_resp2 = client.post(f"/api/migration/execute/{upload_id2}?duplicateAction=override")
    assert exec_resp2.status_code == 200
    exec_data2 = exec_resp2.json()
    print(f"Migration 2 status={exec_data2.get('status')}, fileId={exec_data2.get('fileId')}")
    assert exec_data2.get("fileId") == file_id2

    with engine.connect() as conn:
        status2 = conn.execute(
            text("SELECT Status FROM dbo.UploadedFiles WHERE FileID = :fid"),
            {"fid": file_id2}
        ).scalar()
        print(f"File 2 Status in DB: {status2}")
        assert status2 == "Completed"

        # Check records associated with File 2
        for tbl in ["Customer", "Bills", "Order", "OrderLineItems", "Settlements"]:
            cnt2 = conn.execute(
                text(f"SELECT COUNT(*) FROM dbo.[{tbl}] WHERE FileID = :fid"),
                {"fid": file_id2}
            ).scalar()
            print(f"Table [{tbl}] rows with FileID({file_id2}): {cnt2}")

    print("\n=== 7. Testing File History API Endpoints ===")
    hist_resp = client.get("/api/excel/history/files")
    assert hist_resp.status_code == 200
    hist_data = hist_resp.json()
    print(f"History endpoint returned {len(hist_data)} files:")
    for h in hist_data[:5]:
        print(" ", h)
    assert any(h["FileID"] == file_id1 for h in hist_data)
    assert any(h["FileID"] == file_id2 for h in hist_data)

    mig_files_resp = client.get("/api/migration/files")
    assert mig_files_resp.status_code == 200
    assert len(mig_files_resp.json()) >= 2

    print("\n=== 8. Testing Failed Migration Status Handling ===")
    dummy_file_id = file_tracking_service.create_uploaded_file("corrupt_test_file.csv")
    file_tracking_service.update_file_status(dummy_file_id, "Failed")
    with engine.connect() as conn:
        fail_status = conn.execute(
            text("SELECT Status FROM dbo.UploadedFiles WHERE FileID = :fid"),
            {"fid": dummy_file_id}
        ).scalar()
        print(f"Failed file status in DB: {fail_status}")
        assert fail_status == "Failed"

    print("\n=== ALL TESTS PASSED SUCCESSFULLY! ===")

if __name__ == '__main__':
    run_tests()
