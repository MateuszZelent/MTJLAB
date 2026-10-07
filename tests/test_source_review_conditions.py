"""A malformed boolean must never override an authored comparison."""
import pytest
import yaml

from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.recipes import RecipeNode, parse_recipe_text
from tests.helpers import simulation_settings


@pytest.mark.parametrize("condition", ["false", "true", "", 0, 1, None, [], {}])
def test_non_boolean_condition_rejected_at_parser_and_compiler(condition):
    data = {"condition": condition, "left": "${point}", "operator": ">", "right": 1}
    source = yaml.safe_dump({"schema_version": 1, "name": "condition-check", "root": {
        "id": "branch", "type": "if", **data,
        "children": [{"id": "point", "type": "checkpoint"}],
    }})
    with pytest.raises(ConfigurationError, match="condition"):
        parse_recipe_text(source)
    with pytest.raises(ConfigurationError, match="condition"):
        RecipeCompiler(simulation_settings())._evaluate_condition(
            RecipeNode("branch", "if", data, ()), {}
        )


@pytest.mark.parametrize("condition", [True, False])
def test_literal_boolean_keeps_exact_value(condition):
    compiler = RecipeCompiler(simulation_settings())
    assert compiler._evaluate_condition(RecipeNode("branch", "if", {"condition": condition}, ()), {}) is condition
