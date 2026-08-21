"""The in-kernel runtime API. Names are bound into the user namespace by bootstrap.bind."""


def bind(ip) -> None:
    """Bind the public runtime names into the kernel user namespace. Extended by later tasks."""
    from .files import edit, read, write
    ns = {"read": read, "write": write, "edit": edit}
    # T15: bash; T20+: agent; T23: llm; T24: web; T25: history; T26: workflow
    ip.user_ns.update(ns)
