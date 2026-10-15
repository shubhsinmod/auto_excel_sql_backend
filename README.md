# Intelligent Excel-to-SQL Backend

FastAPI backend for Excel upload/analysis, SQL Server schema inspection, column mapping, and transactional migration.

## Setup (Windows PowerShell)

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DB_SERVER`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`, `DB_PASSWORD`, and `DB_DRIVER` for your SQL Server. Install the matching Microsoft ODBC driver on Windows; `pyodbc` alone does not install the native ODBC driver.

Run the API from this `backend` directory:

```powershell
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/docs` to inspect the API. Health check: `http://127.0.0.1:8000/health`.

## Tests

```powershell
pytest -q
```

## Notes

- `.env` is intentionally not included in this source archive. Copy `.env.example` and provide local credentials.
- Uploads and runtime logs are generated locally and are not included in the source archive.
- Column mappings with equally strong matches are returned as `AMBIGUOUS` with candidate targets; they are not silently migrated.
- Integer primary keys are not automatically treated as identity columns based only on their data type.
- Foreign-key values are resolved against known parent IDs using the value from the corresponding FK source column, rather than unrelated values from the same row.
