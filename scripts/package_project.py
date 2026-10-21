import os
import zipfile
from pathlib import Path

def package_project():
    project_root = Path("..").resolve()
    output_zip = project_root.parent / "excel_to_sql_migration_with_file_tracking.zip"
    
    exclude_dirs = {
        "venv", ".venv", "env", "node_modules", "__pycache__", ".pytest_cache", 
        ".git", ".idea", ".vscode", "dist", "build"
    }
    exclude_files = {
        ".env", ".DS_Store"
    }
    exclude_extensions = {
        ".pyc", ".pyo", ".pyd", ".log"
    }

    print(f"Packaging from: {project_root}")
    print(f"Target zip: {output_zip}")

    total_files = 0
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(project_root):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            
            for file in files:
                if file in exclude_files:
                    continue
                ext = os.path.splitext(file)[1].lower()
                if ext in exclude_extensions:
                    continue
                
                # Exclude huge test csv/xlsx uploaded files in uploads dir except .gitkeep
                rel_dir = os.path.relpath(root, project_root)
                if "uploads" in rel_dir.split(os.sep) and ext in (".csv", ".xlsx", ".xls"):
                    continue

                abs_file_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_file_path, project_root)
                
                zf.write(abs_file_path, arcname=os.path.join("current_project", rel_path))
                total_files += 1

    zip_size_mb = os.path.getsize(output_zip) / (1024 * 1024)
    print(f"Successfully packaged {total_files} files into {output_zip} ({zip_size_mb:.2f} MB)")
    return str(output_zip)

if __name__ == '__main__':
    package_project()
