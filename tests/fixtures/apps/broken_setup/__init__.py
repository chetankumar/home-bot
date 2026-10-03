from hub.plugin import AppContext, Manifest

manifest = Manifest(id="broken_setup", name="Broken setup")


def setup(ctx: AppContext):
    ctx.scheduler.interval("tick", lambda: None, minutes=5)
    raise RuntimeError("boom in setup")
