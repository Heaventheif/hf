"""
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║   ✨ Pinterest API Enhancement Patch ✨                                   ║
║                                                                          ║
║   أضف هذا الكود إلى hf-space/pinterest-scraper-py/api.py                 ║
║   لإضافة endpoint /pinterest/scrape                                      ║
║                                                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════════════
# 1. أضف هذا الاستيراد في بداية الملف
# ═══════════════════════════════════════════════════════════════════════════
# from pydantic import BaseModel, Field  # موجود بالفعل غالباً


# ═══════════════════════════════════════════════════════════════════════════
# 2. أضف هذا الـ Model قبل تعريف app
# ═══════════════════════════════════════════════════════════════════════════

class PinterestScrapeRequest(BaseModel):
    """نموذج طلب البحث عن صور Pinterest"""
    query: str = Field(..., description="كلمة البحث", min_length=2)
    limit: int = Field(default=10, ge=1, le=15, description="عدد الصور (1-15)")
    mode: str = Field(default="auto", description="وضع الكشط: auto, fast, full")


# ═══════════════════════════════════════════════════════════════════════════
# 3. أضف هذا الـ Endpoint بعد تعريف app
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/pinterest/scrape", tags=["pinterest"])
async def pinterest_scrape(req: PinterestScrapeRequest) -> Dict[str, Any]:
    """
    نقطة API مبسطة للبحث عن صور Pinterest.
    
    البحث عن صور بناءً على كلمة مفتاحية وإرجاع قائمة صور.
    الحد الأقصى 15 صورة لكل طلب.
    
    Args:
        query: كلمة البحث
        limit: عدد الصور المطلوب (1-15)
        mode: وضع الكشط (auto/fast/full)
    
    Returns:
        {
            "success": true,
            "query": "كلمة البحث",
            "images": [...],
            "count": عدد الصور,
            "total_found": العدد الكلي
        }
    """
    log.info(f"[pinterest/scrape] query={req.query!r}, limit={req.limit}, mode={req.mode}")
    
    async with _scrape_lock:
        s = await _get_scraper(mode=req.mode, headless=settings.headless)
        try:
            # حساب عدد الصفحات (~20 صورة في الصفحة)
            pages_needed = min(3, max(1, (req.limit + 19) // 20))
            
            # البحث عن الصور
            pins = await s.search_pins(req.query, pages=pages_needed, mode=req.mode)
            
            # استخراج الصور
            images = []
            for pin in pins[:req.limit]:
                # استخراج رابط الصورة الأصلي
                image_url = None
                if pin.image:
                    image_url = pin.image.get("original") or pin.image.get("src")
                    if not image_url:
                        srcset = pin.image.get("srcset", "")
                        import re
                        match = re.search(
                            r"(https://i\.pinimg\.com/originals/[^,\s]+)",
                            srcset
                        )
                        if match:
                            image_url = match.group(1)
                
                if image_url:
                    images.append({
                        "url": image_url,
                        "title": pin.title or req.query,
                        "pin_url": pin.url,
                        "pin_id": pin.id
                    })
            
            return {
                "success": True,
                "query": req.query,
                "images": images,
                "count": len(images),
                "total_found": len(pins),
                "mode": req.mode,
                "pages_scraped": pages_needed
            }
            
        except Exception as exc:
            log.exception(f"[pinterest/scrape] error: {exc}")
            return {
                "success": False,
                "error": str(exc),
                "query": req.query,
                "images": [],
                "count": 0,
                "total_found": 0
            }
        finally:
            await s.close()


# ═══════════════════════════════════════════════════════════════════════════
# 4. أضف هذا Endpoint للبحث المباشر (بديل)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/pinterest/search", tags=["pinterest"])
async def pinterest_search(
    q: str = Query(..., description="كلمة البحث"),
    limit: int = Query(default=10, ge=1, le=15, description="عدد الصور"),
    mode: str = Query(default="auto", description="وضع الكشط")
) -> Dict[str, Any]:
    """
    البحث عن صور Pinterest (GET method)
    
    مثال:
    GET /pinterest/search?q=minimalist%20bedroom&limit=15
    """
    req = PinterestScrapeRequest(query=q, limit=limit, mode=mode)
    return await pinterest_scrape(req)


# ═══════════════════════════════════════════════════════════════════════════
# 5. تحديث قائمة endpoints في root()
# ═══════════════════════════════════════════════════════════════════════════

# أضف هذه الأسطر لقائمة endpoints:
"""
"POST /pinterest/scrape",   # البحث مع limit (JSON body)
"GET  /pinterest/search",   # البحث مع limit (Query params)
"""


# ═══════════════════════════════════════════════════════════════════════════
# 6. أمثلة الاستخدام
# ═══════════════════════════════════════════════════════════════════════════

"""
# POST /pinterest/scrape
curl -X POST http://localhost:7860/pinterest/scrape \
  -H "Content-Type: application/json" \
  -d '{"query": "minimalist bedroom", "limit": 15}'

# GET /pinterest/search
curl "http://localhost:7860/pinterest/search?q=minimalist%20bedroom&limit=15"

# Response:
{
  "success": true,
  "query": "minimalist bedroom",
  "images": [
    {
      "url": "https://i.pinimg.com/originals/xx/xx/xx.jpg",
      "title": "Minimalist Bedroom Design",
      "pin_url": "https://www.pinterest.com/pin/123456789/",
      "pin_id": "123456789"
    }
  ],
  "count": 15,
  "total_found": 120,
  "mode": "auto",
  "pages_scraped": 1
}
"""


# ═══════════════════════════════════════════════════════════════════════════
# 7. ملاحظات مهمة
# ═══════════════════════════════════════════════════════════════════════════

"""
⚠️  ملاحظات:
    1. الحد الأقصى 15 صورة لكل طلب (لحماية من الحظر)
    2. الوضع auto هو الأفضل (سريع أولاً ثم كامل)
    3. كل صفحة تحتوي ~20 صورة
    4. Pinterest يحظر الكشط العدوائي - استخدم delay

✅  المميزات:
    1. endpoint مخصص للبحث مع limit
    2. استخراج تلقائي للصور بأعلى جودة
    3. معالجة أخطاء شاملة
    4. logging للتصحيح
    5. طريقتين: POST و GET
"""
