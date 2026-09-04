from app.production import app
from app.youtube_ingest import router

app.include_router(router)
