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
    from .orders import JOB_ID as ORDERS_JOB
    from .orders import run_orders_sync
    from .routes import build_router
    from .stats import inr
    from .sync import JOB_ID, run_sync

    ctx.templates.env.filters["inr"] = inr
    ctx.scheduler.cron(JOB_ID, lambda: run_sync(ctx), ctx.config.get("sync_cron", "15 7 * * *"))
    # Amazon orders also have their own job (the Orders page's Sync button), and an evening
    # scan so same-day orders and shipping updates are picked up before the morning sync.
    ctx.scheduler.cron(ORDERS_JOB, lambda: run_orders_sync(ctx), ctx.config.get("orders_cron", "45 19 * * *"))
    return build_router(ctx)
