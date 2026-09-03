import os, io, sys, base64, time
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import openai
c=openai.OpenAI(base_url=os.getenv("BAYOFASSETS_BASE_URL"), api_key=os.getenv("BAYOFASSETS_TOKEN"))
prompt=("Beautiful elegant anime woman, early 20s, gentle confident smile, long flowing dark hair, "
        "large expressive eyes, soft cinematic lighting, high quality anime illustration, "
        "front-facing portrait, shoulders up, plain solid background")
for model in ["gpt-image-2","gemini-3-pro-image-preview"]:
    t=time.time()
    try:
        r=c.images.generate(model=model, prompt=prompt, size="1024x1024", n=1)
        dt=round(time.time()-t,1)
        d=r.data[0]
        if getattr(d,"b64_json",None):
            raw=base64.b64decode(d.b64_json)
            open(f"_test_{model.split('-')[0]}.png","wb").write(raw)
            print(f"[{model}] {dt}s OK b64 -> saved ({len(raw)} bytes)")
        elif getattr(d,"url",None):
            print(f"[{model}] {dt}s OK url -> {d.url[:80]}")
        else:
            print(f"[{model}] {dt}s OK but no image data: {d}")
    except Exception as e:
        print(f"[{model}] FAIL [{type(e).__name__} {getattr(e,'status_code',None)}] {str(e)[:120]}")
