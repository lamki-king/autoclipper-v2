from app.main import app
from app.chunk_upload import router as upload_router

app.include_router(upload_router)
