"""Endpoint-scoped authoring checks; execution still requires full preflight."""
from dataclasses import replace

from app.engine.compiler import RecipeCompiler
from app.recipes.models import Recipe, RecipeNode
from app.settings.models import StationSettings


def validate_final_state_edit(settings: StationSettings, recipe: Recipe, node_id: str) -> None:
    """Compile the edited output with its authored prerequisites and control flow.

    Reuse the compiler's final-state visitor and downstream state/range/profile
    checks instead of maintaining a weaker UI version of those safety rules.
    The projected plan is discarded and never used to authorize execution.
    """
    final = next(node for node in recipe.finally_nodes if node.id == node_id)
    device, channel = final.data.get("device"), final.data.get("channel")
    endpoints = {f"{device}.{'vout' if device == 'moke_box' else ''}{channel}"}

    def module_endpoint(node: RecipeNode) -> str | None:
        module = node.data.get("device_module")
        if module in {"anritsu", "anritsu_sg"}:
            return "anritsu.spectrum" if module == "anritsu" else "anritsu.sg"
        if module in {"moke_box", "keithley", "rigol"}:
            configuration = node.data.get("configuration")
            configuration = configuration if isinstance(configuration, dict) else {}
            endpoint_channel = configuration.get("channel", node.data.get("channel"))
            return f"{module}.{'vout' if module == 'moke_box' else ''}{endpoint_channel}"
        return None

    def require_endpoint(endpoint: str) -> None:
        endpoints.add(endpoint)
        if endpoint.startswith("anritsu."):
            # Spectrum and SG share physical mode/output state. Keep both
            # routes' authored transitions when either becomes a dependency.
            endpoints.update({"anritsu.spectrum", "anritsu.sg"})

    def owns_output(node: RecipeNode) -> bool:
        if node.type == "final_state":
            return node.id == node_id or (
                node.data.get("device") == device and node.data.get("channel") == channel
            )
        if node.type == "sweep":
            if str(node.data.get("target", "")).startswith("moke_box.") and any(
                endpoint.startswith("moke_box.") for endpoint in endpoints
            ):
                return True
            return any(str(node.data.get("target", "")).startswith(f"{endpoint}.")
                       for endpoint in endpoints)
        endpoint = module_endpoint(node)
        if endpoint is not None:
            return endpoint in endpoints
        if "anritsu.spectrum" in endpoints and node.type in {
            "configure_anritsu", "configure_anritsu_advanced", "configure_anritsu_sg",
            "update_anritsu_sg", "set_anritsu_sg_output", "enable_anritsu_sg_output",
        }:
            return True
        # MOKE arming and trajectory state are implicit and shared across
        # VOUTs. Keep all authored control transitions so an arm or update
        # cannot be attributed to a different channel by the projection.
        if any(endpoint.startswith("moke_box.") for endpoint in endpoints) and node.type in {
                "set_moke_voltage", "configure_moke_box", "arm_moke_voltage",
                "update_moke_voltage", "stop_moke_voltage",
        }:
            return True
        return any(module in node.type and not node.type.startswith("measure_")
                   and f"{module}.{node.data.get('channel')}" in endpoints
                   for module in ("keithley", "rigol"))

    def project(node: RecipeNode) -> RecipeNode | None:
        children = tuple(child for raw in node.children if (child := project(raw)) is not None)
        else_children = tuple(child for raw in node.else_children if (child := project(raw)) is not None)
        if owns_output(node) or children or else_children:
            # Preserve disabled flags, branches, repetitions, sweep references,
            # configuration snapshots and order. Never invent a prerequisite
            # from station defaults or flatten a conditional configuration.
            if node.type == "sweep":
                # An enclosing physical axis still determines whether/how its
                # children execute. Include that axis's source prerequisites.
                require_endpoint(str(node.data.get("target", "")).rsplit(".", 1)[0])
            if node.type == "sequence" and node.data.get("operation") == "configure_selected_parameters":
                endpoint = module_endpoint(node)
                if endpoint is not None:
                    # Native modules compile selected fields against an
                    # earlier explicit baseline, even when kept only because
                    # a child controls the edited output. Retain their entire
                    # endpoint history, including sibling modules and OFFs.
                    require_endpoint(endpoint)
            return replace(node, children=children, else_children=else_children)
        return None

    while True:
        previous = set(endpoints)
        root = project(recipe.root) or RecipeNode(recipe.root.id, "sequence", {})
        finally_nodes = tuple(node for raw in recipe.finally_nodes if (node := project(raw)) is not None)
        if endpoints == previous:
            break
    RecipeCompiler(settings).compile(replace(recipe, root=root, finally_nodes=finally_nodes))
