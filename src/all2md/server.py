from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import PlainTextResponse
import asyncio
import logging
from time import time
from uuid import uuid4

from .converter import convert_bytes

logger = logging.getLogger(__name__)
conversion_jobs: dict[str, dict] = {}

app = FastAPI(
    title="All2MD API",
    description="Convert documents to Markdown format",
    version="0.1.0"
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


def _set_job_status(job_id: str, **updates):
    job = conversion_jobs.get(job_id)
    if job is not None:
        job.update(updates)
        job["updated_at"] = time()


async def _run_conversion_job(job_id: str, contents: bytes, filename: str, content_type: str | None):
    try:
        _set_job_status(
            job_id,
            status="running",
            progress=20,
            message="Starting document conversion",
        )

        _set_job_status(
            job_id,
            status="running",
            progress=45,
            message="Converting document to Markdown",
        )
        started_at = time()
        conversion_task = asyncio.create_task(asyncio.to_thread(convert_bytes, contents, filename))
        while not conversion_task.done():
            elapsed_seconds = int(time() - started_at)
            progress = min(90, 45 + elapsed_seconds // 10)
            _set_job_status(
                job_id,
                status="running",
                progress=progress,
                message=f"Converting document to Markdown ({elapsed_seconds}s elapsed)",
            )
            await asyncio.sleep(2)

        markdown_content = await conversion_task

        _set_job_status(
            job_id,
            status="completed",
            progress=100,
            message="Conversion completed",
            result={
                "filename": filename,
                "content": markdown_content,
                "content_type": content_type,
            },
        )
    except Exception as exc:
        logger.exception("Error converting file in job %s", job_id)
        _set_job_status(
            job_id,
            status="failed",
            progress=100,
            message=f"Conversion failed: {exc}",
            error=str(exc),
        )


@app.post("/convert/jobs")
async def start_conversion_job(file: UploadFile = File(...)):
    """
    Start an asynchronous conversion job.

    Clients can poll /convert/jobs/{job_id} for progress and final content.
    """
    contents = await file.read()

    if not contents:
        raise HTTPException(status_code=400, detail="File is empty")

    job_id = uuid4().hex
    filename = file.filename or "document"
    conversion_jobs[job_id] = {
        "job_id": job_id,
        "filename": filename,
        "content_type": file.content_type,
        "status": "queued",
        "progress": 5,
        "message": "Queued conversion job",
        "created_at": time(),
        "updated_at": time(),
    }

    asyncio.create_task(_run_conversion_job(job_id, contents, filename, file.content_type))

    return {
        "job_id": job_id,
        "status": "queued",
        "progress": 5,
        "message": "Queued conversion job",
        "status_url": f"/convert/jobs/{job_id}",
    }


@app.get("/convert/jobs/{job_id}")
async def get_conversion_job(job_id: str):
    """Return conversion job status and result when completed."""
    job = conversion_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Conversion job not found")

    return job


@app.post("/convert")
async def convert_document(file: UploadFile = File(...)):
    """
    Convert uploaded document to Markdown.
    
    Returns the Markdown content as plain text.
    """
    try:
        # Read file contents
        contents = await file.read()
        
        if not contents:
            raise HTTPException(status_code=400, detail="File is empty")
        
        # Convert to Markdown
        markdown_content = await asyncio.to_thread(convert_bytes, contents, file.filename or "document")
        
        return PlainTextResponse(content=markdown_content)
    
    except Exception as e:
        logger.error(f"Error converting file: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to convert file: {str(e)}"
        )


@app.post("/convert/json")
async def convert_document_json(file: UploadFile = File(...)):
    """
    Convert uploaded document to Markdown and return as JSON.
    
    Returns JSON with filename and markdown content.
    """
    try:
        contents = await file.read()
        
        if not contents:
            raise HTTPException(status_code=400, detail="File is empty")
        
        markdown_content = await asyncio.to_thread(convert_bytes, contents, file.filename or "document")
        
        return {
            "filename": file.filename,
            "content": markdown_content,
            "content_type": file.content_type
        }
    
    except Exception as e:
        logger.error(f"Error converting file: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to convert file: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
