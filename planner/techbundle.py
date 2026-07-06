"""Co-product recycling bundle detection & solver for `planner tech`.

Some Factorio recipes have multiple guaranteed/probabilistic joint outputs
that recipe-mcp's engine (`_pick_producer`/`_expand_node`) never tracks — it
picks exactly one recipe per item and only looks at the one product row
matching what was requested. Every other product a chosen recipe yields is
silently discarded, never credited against demand elsewhere. Concretely: a
Pyanodons "screener" recipe can produce two ore grades in one batch, and a
separate "crusher" recipe converts the lower grade into more of the higher
grade — the economically correct plan runs both and routes 100% of the
screener's low-grade output through the crusher, but the existing engine has
no way to represent "run recipe A and recipe B together, in a specific
ratio, to close this loop with zero waste."

This module solves that — but only within a TECH-UNLOCK bundle (the set of
recipes one Factorio technology unlocks together), not across the whole
recipe graph. A whole-graph version was explored and abandoned: a BFS from
copper-plate reached ~4159 of ~4160 total Pyanodons recipes (essentially the
whole modpack, mostly via an unrelated creature-farming subsystem) — utterly
intractable for a hand-written solver, and the wrong scope besides. Tech
unlock bundles are small by construction (median 4 recipes/tech, mean 7.9,
83% <= 9) and are the game designer's own intended grouping — "Copper
processing - Stage 1" unlocks exactly the screener recipe, the crusher
recipe, and the smelting recipe that consumes their combined output, as one
coherent package.

Everything in this module is pure (plain dicts/sets/lists in, plain dicts
out) — no DB or engine import, mirroring `planner/options.py`. `cli.cmd_tech`
does the DB queries (what a tech unlocks, each recipe's ingredients/products)
and the probability -> expected-amount collapse (via the same formula
`engine._effective_out` uses), then hands plain `RecipeIO` dicts to the
functions here.

A `RecipeIO` is `{"ingredients": [(item_id, amount), ...],
"products": [(item_id, expected_amount), ...]}` — `expected_amount` is
already probability-collapsed by the caller; this module never looks at
probability directly.
"""

from __future__ import annotations

from fractions import Fraction

# Techs that unlock more than this many recipes are "grab-bag" techs, not a
# coherent single-purpose bundle — cli.cmd_tech skips graph/solve analysis
# for these and just lists what's unlocked. Comfortably above the 83rd
# percentile (9 recipes) of all 783 techs that unlock anything, well below
# the pathological tail (some techs unlock 50-900 recipes at once).
SIZE_CAP = 40

_DENOM_LIMIT = 10**6


def _as_fraction(x: float) -> Fraction:
    return Fraction(x).limit_denominator(_DENOM_LIMIT)


def _touching_recipes(recipe_ios: dict) -> dict[str, set[str]]:
    """item_id -> set of recipe_ids that touch it, as ingredient OR product."""
    touches: dict[str, set[str]] = {}
    for rid, io in recipe_ios.items():
        for item_id, _ in io["ingredients"]:
            touches.setdefault(item_id, set()).add(rid)
        for item_id, _ in io["products"]:
            touches.setdefault(item_id, set()).add(rid)
    return touches


def find_components(recipe_ios: dict) -> list[set[str]]:
    """Connected components among `recipe_ios`'s recipes: two recipes are
    linked if they share any item (either touches it, as ingredient or
    product — sharing matters in either direction, since a recycling loop
    needs both "produces" and "consumes" edges to be found). Every recipe
    appears in exactly one component; an isolated recipe is its own
    singleton set."""
    touches = _touching_recipes(recipe_ios)
    parent: dict[str, str] = {rid: rid for rid in recipe_ios}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for rids in touches.values():
        sorted_rids = sorted(rids)
        for other in sorted_rids[1:]:
            union(sorted_rids[0], other)

    groups: dict[str, set[str]] = {}
    for rid in recipe_ios:
        groups.setdefault(find(rid), set()).add(rid)
    return list(groups.values())


def classify_boundary(component: set[str], recipe_ios: dict) -> dict:
    """-> {"internal": set[item_id], "external_inputs": set[item_id],
    "external_outputs": set[item_id]}.

    `internal`: produced by >=1 recipe in `component` AND consumed by >=1
    recipe in `component`, AND touched by >=2 DISTINCT recipes. That last
    clause excludes a private catalyst — an item whose only producer and
    only consumer is the SAME single recipe — which would otherwise create
    a spurious all-in-one-variable balance row in `solve_component` that
    wrongly forces that recipe's rate to zero; a private catalyst is already
    fully netted into that recipe's own coefficient and needs no row at all.
    `external_inputs`/`external_outputs`: touched by only one side within
    the component (a free feed in, or a sink out) — outputs are the anchor
    candidates `default_anchor`/`solve_component` size a solve around.
    """
    producers: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    for rid in component:
        io = recipe_ios[rid]
        for item_id, _ in io["products"]:
            producers.setdefault(item_id, set()).add(rid)
        for item_id, _ in io["ingredients"]:
            consumers.setdefault(item_id, set()).add(rid)

    items = set(producers) | set(consumers)
    internal: set[str] = set()
    external_inputs: set[str] = set()
    external_outputs: set[str] = set()
    for item_id in items:
        p = producers.get(item_id, set())
        c = consumers.get(item_id, set())
        if p and c:
            if len(p | c) >= 2:
                internal.add(item_id)
            # else: private catalyst of a single recipe -- not a boundary
            # item, invisible to the linear system (see docstring above).
        elif c and not p:
            external_inputs.add(item_id)
        elif p and not c:
            external_outputs.add(item_id)
    return {
        "internal": internal,
        "external_inputs": external_inputs,
        "external_outputs": external_outputs,
    }


def _net_coefficient(io: dict, item_id: str) -> Fraction:
    """Net production of `item_id` by one batch of a recipe: products minus
    ingredients, both roles summed — this naturally nets a recipe that both
    produces and consumes the same item (e.g. a catalyst) into one
    coefficient, rather than needing separate handling."""
    total = Fraction(0)
    for iid, amount in io["products"]:
        if iid == item_id:
            total += _as_fraction(amount)
    for iid, amount in io["ingredients"]:
        if iid == item_id:
            total -= _as_fraction(amount)
    return total


def solve_linear_system(matrix: list[list[Fraction]], rhs: list[Fraction]) -> dict:
    """Gauss-Jordan elimination with partial pivoting over exact `Fraction`
    arithmetic — no floating-point tolerance needed since every coefficient
    here is an exact rational (Factorio recipe amounts/probabilities are
    exact decimals).

    Returns one of:
      {"status": "unique", "rank": r, "solution": [Fraction, ...]}
      {"status": "underdetermined", "rank": r, "free_columns": [...], "reason": str}
      {"status": "infeasible", "rank": r, "reason": str}

    A rank deficiency by itself is NOT an error — conservation systems
    routinely produce a redundant balance row (total mass conserved implies
    one row is a linear combination of the others). Only a row that reduces
    to "0 = nonzero" is genuinely infeasible; "unique" requires the system
    to be both consistent AND have rank == number of variables (extra,
    redundant-but-consistent rows are fine and expected).
    """
    n_vars = len(matrix[0]) if matrix else 0
    aug = [list(row) + [rhs[i]] for i, row in enumerate(matrix)]
    n_rows = len(aug)

    pivot_row_of_col: dict[int, int] = {}
    r = 0
    for col in range(n_vars):
        piv = None
        for row in range(r, n_rows):
            if aug[row][col] != 0:
                piv = row
                break
        if piv is None:
            continue  # no pivot available in this column -- a free variable
        aug[r], aug[piv] = aug[piv], aug[r]
        pivot_val = aug[r][col]
        aug[r] = [v / pivot_val for v in aug[r]]
        for row in range(n_rows):
            if row != r and aug[row][col] != 0:
                factor = aug[row][col]
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[r], strict=True)]
        pivot_row_of_col[col] = r
        r += 1
        if r == n_rows:
            break

    rank = len(pivot_row_of_col)

    for aug_row in aug:
        if aug_row[n_vars] != 0 and all(v == 0 for v in aug_row[:n_vars]):
            return {
                "status": "infeasible",
                "rank": rank,
                "reason": "conservation constraints are contradictory (a balance row reduced to 0 = nonzero)",
            }

    if rank < n_vars:
        free = [c for c in range(n_vars) if c not in pivot_row_of_col]
        return {
            "status": "underdetermined",
            "rank": rank,
            "free_columns": free,
            "reason": f"{len(free)} degree(s) of freedom remain -- multiple valid blends exist",
        }

    solution = [Fraction(0)] * n_vars
    for col, prow in pivot_row_of_col.items():
        solution[col] = aug[prow][n_vars]
    return {"status": "unique", "rank": rank, "solution": solution}


def solve_component(
    component: set[str], recipe_ios: dict, anchor_item: str, target_rate: float
) -> dict:
    """Solve for the batch-rate (same units as `target_rate`, e.g. batches
    per minute) blend across `component`'s recipes that exactly closes every
    internal item's balance (zero waste, zero import) while producing
    `anchor_item` at `target_rate` net of whatever the component itself
    consumes of it.

    Returns {"status", "batch_rates": dict[recipe_id, float] | None,
    "reason": str | None, "anchor_item", "target_rate"}. `status` is one of:
      "solved"              -- batch_rates has a rate for every recipe in `component`
      "underdetermined"     -- multiple valid blends exist; not guessed
      "infeasible"          -- conservation constraints are contradictory
      "negative_infeasible" -- the unique solution needs a negative rate
                               (this bundle is a net sink for the anchor
                               item, not a valid recycling loop)
    """
    boundary = classify_boundary(component, recipe_ios)
    variables = sorted(component)
    var_index = {rid: i for i, rid in enumerate(variables)}

    matrix: list[list[Fraction]] = []
    rhs: list[Fraction] = []
    # anchor_item's own balance row (below) supersedes any internal row for
    # it -- an item can be "internal" (touched by >=2 recipes) AND be the
    # anchor (e.g. forcing a net surplus of an otherwise-internal item);
    # emitting both would contradict a net-zero row against the requested
    # net-target_rate row.
    for item_id in sorted(boundary["internal"] - {anchor_item}):
        row = [Fraction(0)] * len(variables)
        for rid in component:
            row[var_index[rid]] = _net_coefficient(recipe_ios[rid], item_id)
        matrix.append(row)
        rhs.append(Fraction(0))

    anchor_row = [Fraction(0)] * len(variables)
    for rid in component:
        anchor_row[var_index[rid]] = _net_coefficient(recipe_ios[rid], anchor_item)
    matrix.append(anchor_row)
    rhs.append(_as_fraction(target_rate))

    result = solve_linear_system(matrix, rhs)
    if result["status"] != "unique":
        return {
            "status": result["status"],
            "batch_rates": None,
            "reason": result["reason"],
            "anchor_item": anchor_item,
            "target_rate": target_rate,
        }

    rates: dict[str, float] = {}
    negative_epsilon = Fraction(-1, 10**9)
    for rid, val in zip(variables, result["solution"], strict=True):
        if val < negative_epsilon:
            return {
                "status": "negative_infeasible",
                "batch_rates": None,
                "reason": (
                    f"recipe '{rid}' would need a negative rate ({float(val):.4g}); "
                    "this bundle is a net sink for the anchor item, not a valid recycling loop"
                ),
                "anchor_item": anchor_item,
                "target_rate": target_rate,
            }
        rates[rid] = max(float(val), 0.0)

    return {
        "status": "solved",
        "batch_rates": rates,
        "reason": None,
        "anchor_item": anchor_item,
        "target_rate": target_rate,
    }


def _internal_edges(component: set[str], recipe_ios: dict) -> dict[str, set[str]]:
    """a -> b iff some item produced by a (within `component`) is consumed
    by b (a != b) -- the directed edges `default_anchor` uses to find the
    "most downstream" external output."""
    producers: dict[str, set[str]] = {}
    for rid in component:
        for item_id, _ in recipe_ios[rid]["products"]:
            producers.setdefault(item_id, set()).add(rid)
    edges: dict[str, set[str]] = {rid: set() for rid in component}
    for rid in component:
        for item_id, _ in recipe_ios[rid]["ingredients"]:
            for producer in producers.get(item_id, ()):
                if producer != rid:
                    edges[producer].add(rid)
    return edges


def _tarjan_scc(nodes: set[str], edges: dict[str, set[str]]) -> dict[str, int]:
    """Tarjan's strongly-connected-components algorithm -> {node: scc_id}.
    Recursive (bundles are capped at SIZE_CAP=40 recipes, nowhere near
    Python's recursion limit) — groups genuine cycles among the component's
    own recipes (mutual co-producers) into one SCC, so `_scc_depths` can tie
    them at the same depth instead of a naive topological sort crashing on
    a cycle."""
    index_counter = [0]
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    scc_id: dict[str, int] = {}
    next_scc = [0]

    def strongconnect(v: str) -> None:
        indices[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True

        for w in sorted(edges.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            comp_id = next_scc[0]
            next_scc[0] += 1
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc_id[w] = comp_id
                if w == v:
                    break

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    return scc_id


def _scc_depths(
    nodes: set[str], edges: dict[str, set[str]], scc_id: dict[str, int]
) -> dict[int, int]:
    """Longest-path depth of each SCC in the condensation DAG: 0 for an SCC
    with no incoming edges from another SCC, else 1 + max(depth of
    predecessor SCCs)."""
    reverse: dict[int, set[int]] = {}
    for a in nodes:
        for b in edges.get(a, ()):
            sa, sb = scc_id[a], scc_id[b]
            if sa != sb:
                reverse.setdefault(sb, set()).add(sa)

    depth: dict[int, int] = {}

    def compute(scc: int) -> int:
        if scc in depth:
            return depth[scc]
        preds = reverse.get(scc, ())
        depth[scc] = 0 if not preds else 1 + max(compute(p) for p in preds)
        return depth[scc]

    for scc in set(scc_id.values()):
        compute(scc)
    return depth


def default_anchor(component: set[str], recipe_ios: dict, external_outputs: set[str]) -> str | None:
    """Pick which external-output item to size a solve for, when there's
    more than one (e.g. the copper bundle's `copper-plate` main output vs
    `stone`, an incidental crusher byproduct nothing else in the bundle
    consumes). Picks the item whose producing recipe sits deepest in the
    component's internal production chain (via SCC condensation, so a
    genuine cycle among the component's own recipes ties rather than
    crashing a naive topological sort), breaking ties alphabetically.
    Returns None if there are no external outputs at all (a fully closed
    loop — `solve_component` has no anchor to size against)."""
    if not external_outputs:
        return None
    if len(external_outputs) == 1:
        return next(iter(external_outputs))

    edges = _internal_edges(component, recipe_ios)
    scc_id = _tarjan_scc(component, edges)
    depths = _scc_depths(component, edges, scc_id)

    producers: dict[str, set[str]] = {}
    for rid in component:
        for item_id, _ in recipe_ios[rid]["products"]:
            producers.setdefault(item_id, set()).add(rid)

    def item_depth(item_id: str) -> int:
        return max((depths[scc_id[rid]] for rid in producers.get(item_id, ())), default=0)

    return max(sorted(external_outputs), key=item_depth)
