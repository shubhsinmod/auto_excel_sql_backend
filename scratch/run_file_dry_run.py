import sys
sys.path.insert(0, ".")
import pandas as pd
from app.migration.dry_run import DryRunEngine
from app.migration.engine import load_file_dataframe

file_path = "uploads/upload_2fb22f9ee706.csv"
print(f"Loading {file_path}...")
df = pd.read_csv(file_path)
print(f"Loaded {len(df)} rows, {len(df.columns)} columns.")

report = DryRunEngine.run_dry_run(
    df=df,
    source_name="upload_2fb22f9ee706.csv",
    test_source="UPLOADED_FILE"
)

print("\n=== DRY RUN REPORT SUMMARY ===")
print(f"Mode: {report.mode}")
print(f"Rows Read: {report.rows_read}")
print(f"Rows Validated: {report.rows_validated}")
print(f"Rows Valid: {report.rows_valid}")
print(f"Rows with Errors: {report.rows_with_errors}")
print(f"Rows with Warnings: {report.rows_with_warnings}")
print(f"Matched Columns: {report.matched_columns}")
print(f"Proposed INSERTs: {report.proposed_insert_statements}")
print(f"Proposed UPDATEs: {report.proposed_update_statements}")
print(f"FKs Resolved: {report.foreign_keys_resolved}")
print(f"FKs Unresolved: {report.foreign_keys_unresolved}")
print(f"Verification: {report.verification_passed}")
if report.validation_summary and report.validation_summary.errors:
    print(f"\nTop 10 Validation Errors ({len(report.validation_summary.errors)} total):")
    for e in report.validation_summary.errors[:10]:
        print(" ", e)
