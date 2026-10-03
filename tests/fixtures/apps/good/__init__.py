from fastapi import APIRouter

from hub.plugin import AppContext, Manifest

manifest = Manifest(id="good", name="Good", icon="✅")


def setup(ctx: AppContext) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def index():
        with ctx.db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM things").fetchone()[0]
        return {"app": ctx.id, "things": n}

    return router
