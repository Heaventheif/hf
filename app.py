from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import List, Optional, Dict, Any
import asyncio

from pipeline import process_image_url

app = FastAPI()

class RequestBody(BaseModel):
    image_urls: Optional[List[HttpUrl]] = None
    pages: Optional[List[Dict[str, Any]]] = None

class ResponseItem(BaseModel):
    url: str
    source_language: Optional[str] = None
    ocr_text: Optional[str] = None
    translated_text: Optional[str] = None
    error: Optional[str] = None

class ResponseBody(BaseModel):
    results: List[ResponseItem]

@app.post("/infer", response_model=ResponseBody)
async def infer(req: RequestBody):
    urls: List[str] = []

    if req.image_urls:
        urls = [str(u) for u in req.image_urls]
    elif req.pages:
        for p in req.pages:
            if isinstance(p, dict) and "url" in p:
                urls.append(str(p["url"]))
    else:
        raise HTTPException(status_code=400, detail="Provide image_urls or pages[].url")

    if not urls:
        raise HTTPException(status_code=400, detail="No image URLs found")

    tasks = [asyncio.create_task(process_image_url(u)) for u in urls]
    raw_results = await asyncio.gather(*tasks)

    return ResponseBody(results=[ResponseItem(**r) for r in raw_results])
