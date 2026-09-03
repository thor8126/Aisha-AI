import os, io, sys, json
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import openai
from agent_brain import AutonomousAgent
from assistant_tools import ToolRegistry
nvidia=openai.OpenAI(base_url=os.getenv("BASE_URL"),api_key=os.getenv("TOKEN"))
heavy=openai.OpenAI(base_url=os.getenv("BAYOFASSETS_BASE_URL"),api_key=os.getenv("BAYOFASSETS_TOKEN"))
reg=ToolRegistry(); calls=[]
def stub(n,a):
    calls.append((n,a))
    if n=="search_web": return json.dumps({"ok":True,"results":[{"title":"AI 2026 trends","snippet":"Agentic AI, multimodal models, on-device AI are rising."}]})
    return json.dumps({"ok":True,"status":"created","path":f"E:/AishaFiles/out.{ 'docx' if n=='create_document' else 'pptx'}"})
reg.execute=stub
ag=AutonomousAgent(nvidia,tool_registry=reg,heavy_client=heavy,heavy_models=["claude-opus-4-8","claude-sonnet-5"],max_steps=10)
q="AI trends पर research करके ek document aur ek short presentation dono bana do"
r=ag.run_task(q,"तू आयशा है. हिंदी देवनागरी में जवाब दे।")
while ag.has_pending_confirmation: r=ag.run_task("haan kar do","x")
print("U:",q)
print("TOOL SEQUENCE:", [n for n,_ in calls])
print("A:", r[:200])
