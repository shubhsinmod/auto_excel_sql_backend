import uuid


def generate_unique_job_id() -> str:
    """Generate safe unique job ID for migration tasks."""
    return str(uuid.uuid4())
