"""
Entry-Point fuer den Gewerbeagent-Framework-Server.

Starten:
  uv run python main.py

Oder mit uvicorn direkt (fuer Hot-Reload im Dev):
  uv run uvicorn core.api:app --reload --port 8001
"""
import uvicorn

from config.settings import settings

if __name__ == "__main__":
    print("=" * 60)
    print("Gewerbeagent Framework Server")
    print("=" * 60)
    print(f"Environment: {settings.environment}")
    print(f"Public URL:  {settings.public_url}")
    print(f"Debug:       {settings.debug}")
    print()
    print("Endpoints:")
    print("  GET  /                                          Status")
    print("  GET  /health                                    Health-Check")
    print("  POST /webhook/{tenant}/{plugin}/{endpoint}      Plugin-Dispatch")
    print("=" * 60)

    uvicorn.run(
        "core.api:app",
        host="0.0.0.0",
        port=8001,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
