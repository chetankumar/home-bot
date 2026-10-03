"""Finance: HDFC alert emails -> tagged transactions -> monthly burn vs budget.

All AI use is local (LM Studio or Ollama); hub.toml's [apps.finance.ai] policy enforces it.
"""

from fastapi import APIRouter

from hub.plugin import AppContext, Manifest

manifest = Manifest(
    id="finance",
    name="Finance",
    icon="💸",
    description="Spends from HDFC alert emails, tagged by recipient, against a monthly budget.",
    requires=["gmail"],
)


def setup(ctx: AppContext) -> APIRouter:
    from .routes import build_router
    from .stats import inr
    from .sync import JOB_ID, run_sync

    ctx.templates.env.filters["inr"] = inr
    ctx.scheduler.cron(JOB_ID, lambda: run_sync(ctx), ctx.config.get("sync_cron", "15 7 * * *"))
    return build_router(ctx)
