from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import PlainTextResponse
import logging

from .converter import convert_bytes

logger = logging.getLogger(__name__)

app = FastAPI(
    title="All2MD API",
    description="Convert documents to Markdown format",
    version="0.1.0"
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


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
        markdown_content = convert_bytes(contents, file.filename or "document")
        
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
        
        markdown_content = convert_bytes(contents, file.filename or "document")
        
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
