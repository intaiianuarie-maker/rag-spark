"""Agent run HTTP seam (T-V3-021)."""
# Do not re-export router as app.agents.router to avoid shadowing the submodule
# (so `import app.agents.router` gives the module with create_runtime).
__all__: list[str] = []
