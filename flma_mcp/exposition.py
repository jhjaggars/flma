"""Hand-rolled Prometheus text exposition format (0.0.4) writer.

Zero knowledge of flma/Factorio -- this module only knows how to turn a list
of `Family` objects into valid `text/plain; version=0.0.4` bytes. Deliberately
not built on `prometheus_client`: that library is an *instrumentation* API
(`Gauge`/`Counter` objects that own and mutate their own value over the
process lifetime), and flma has nothing to instrument -- every number here is
read fresh out of `GameState` at scrape time. The `prometheus_client`-shaped
answer to that is a custom `Collector` yielding `GaugeMetricFamily`/
`CounterMetricFamily`, which is about the same amount of code as this module
minus the ~60 lines of rendering below -- rendering that would then live
behind an optional dependency `make quick` doesn't install (see
`pyproject.toml`'s `mcp` extra and `noxfile.py`), so the exact code most prone
to silent breakage (escaping, float spelling) would stop being covered by the
default test gate. See `flma_mcp/CLAUDE.md`'s "Prometheus metrics" section for
the full reasoning; don't "simplify" this into a `prometheus_client` dependency
without re-reading it.

`flma_mcp/metrics.py` is the only caller and owns all Factorio-specific
shaping; this module never imports it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

Number = int | float

_VALID_TYPES = frozenset({"counter", "gauge"})
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class Family:
    """One metric family: a name/type/help triple plus its samples.

    `frozen=True` blocks `fam.name = "..."` while still allowing `add()` to
    append to the mutable `samples` list -- the invariant wanted here is "the
    identity of a family can't change after construction", not "nothing about
    it can ever be mutated".
    """

    name: str
    type: str
    help: str
    samples: list[tuple[dict[str, str], Number]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid metric name: {self.name!r}")
        if self.type not in _VALID_TYPES:
            raise ValueError(f"invalid metric type {self.type!r} for {self.name!r}")

    def add(self, value: Number, **labels: str) -> None:
        """Append one sample. Label names are validated here (not just
        values) so a typo like `save-id` fails at test time instead of
        silently corrupting a scrape -- label *names* are always fixed
        literals in caller code, never untrusted, so this is purely a
        programmer-error guard."""
        for label_name in labels:
            if not _LABEL_RE.match(label_name):
                raise ValueError(f"invalid label name {label_name!r} on metric {self.name!r}")
        self.samples.append((dict(labels), value))


def escape_help(text: str) -> str:
    """HELP text escapes backslash and newline. Quotes are literal in a HELP
    line (unlike a label value) -- escaping them would produce a visible
    `\\"` in promtool/Grafana's metric browser instead of a plain `"`."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label_value(value: str) -> str:
    """Order matters: backslash MUST be escaped first. Escaping `"` before
    `\\` would turn a literal backslash already in the input into `\\"`,
    which reads as an escaped quote and terminates the label value early --
    corrupting everything rendered after it. `\\r` has no defined escape in
    the 0.0.4 grammar, so it's dropped rather than passed through raw."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def format_value(value: Number) -> str:
    """Render one sample value. Three traps this specifically guards
    against: `bool` is an `int` subclass in Python, so the bool check MUST
    precede the int check or `str(True)` ("True") corrupts the scrape;
    Python spells the IEEE specials `inf`/`nan` in lowercase, but Prometheus's
    grammar requires exactly `+Inf`/`-Inf`/`NaN`; and `f"{v:.6f}"`-style
    fixed-precision formatting both pads short floats with noise and loses
    precision on large ones -- `repr()` is shortest-round-trip since Python
    3.1 and produces valid Go float syntax (including `1e+300`) directly.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    v = float(value)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(v)


def _render_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{name}="{escape_label_value(str(val))}"' for name, val in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def render(families: Iterable[Family]) -> str:
    """Render a full scrape body. Each family contributes a `# HELP` line,
    a `# TYPE` line, then all of its samples, contiguous and in that order --
    Prometheus rejects a scrape with a second `# TYPE` for a name already
    seen, or with a family's samples split up by another family's lines, so
    `seen` below turns a caller bug (two `Family` objects sharing a name)
    into a loud `ValueError` instead of a scrape a real Prometheus would
    silently drop. Families with zero samples are skipped entirely rather
    than emitting a bare HELP/TYPE pair for nothing -- this is also what lets
    `metrics.collect()`'s staleness handling "just work" (see
    `flma_mcp/CLAUDE.md`): a stale scrape simply never builds those families.
    No explicit sample timestamps are ever emitted -- Prometheus stamps at
    scrape time, and explicit timestamps interact badly with staleness
    markers and `rate()`.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for fam in families:
        if not fam.samples:
            continue
        if fam.name in seen:
            raise ValueError(f"duplicate metric family name: {fam.name!r}")
        seen.add(fam.name)
        lines.append(f"# HELP {fam.name} {escape_help(fam.help)}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        rendered_samples = sorted(
            ((_render_labels(labels), value) for labels, value in fam.samples),
            key=lambda pair: pair[0],
        )
        for label_str, value in rendered_samples:
            lines.append(f"{fam.name}{label_str} {format_value(value)}")
    return "\n".join(lines) + "\n" if lines else ""


__all__ = [
    "CONTENT_TYPE",
    "Family",
    "escape_help",
    "escape_label_value",
    "format_value",
    "render",
]
