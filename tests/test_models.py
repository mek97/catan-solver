import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import BoardConfig, config_warnings

FIXTURE = Path(__file__).parent.parent / "app" / "fixtures" / "default_board.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_is_valid_and_clean():
    cfg = BoardConfig.model_validate(load_fixture())
    assert len(cfg.hexes) == 19
    assert config_warnings(cfg) == []


def test_rejects_double_occupancy():
    data = load_fixture()
    data["players"]["red"]["settlements"] = [10]
    data["players"]["blue"]["cities"] = [10]
    with pytest.raises(ValidationError):
        BoardConfig.model_validate(data)


def test_rejects_bad_token():
    data = load_fixture()
    data["hexes"][0]["number"] = 7
    with pytest.raises(ValidationError):
        BoardConfig.model_validate(data)


def test_warns_on_distance_rule_violation():
    from app import board

    data = load_fixture()
    v = 20
    neighbor = board.VERTEX_ADJ[v][0]
    data["players"]["red"]["settlements"] = [v]
    data["players"]["blue"]["settlements"] = [neighbor]
    cfg = BoardConfig.model_validate(data)
    assert any("distance rule" in w for w in config_warnings(cfg))
