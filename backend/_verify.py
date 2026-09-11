import importlib, sys
mods = ["langgraph","langchain","langchain_core","langchain_openai","langchain_community","langgraph.checkpoint.sqlite","langchain_mcp_adapters","dotenv","pydantic_settings","httpx","aiofiles","tiktoken"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        v = getattr(mod, "__version__", "")
        print(f"OK  {m} {v}")
    except Exception as e:
        print(f"FAIL {m}: {e}")
print("python", sys.version.split()[0])
