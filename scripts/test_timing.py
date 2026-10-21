import time
from app.migration.engine import MigrationEngine
from app.services.excel_service import excel_service

def test_timing():
    upload_id = "upload_2f4b65884428"
    session = excel_service.get_upload_session(upload_id)
    print("Found session:", session.get("fileName"), "rows:", session.get("rowCount"))

if __name__ == '__main__':
    test_timing()
