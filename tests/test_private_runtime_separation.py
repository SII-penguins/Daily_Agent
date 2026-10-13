"""Quality checks must not replace private delivery choices with public examples."""
from pathlib import Path
import shutil
import yaml

from daily_agent.full_profile import enforce_full_profile


def test_quality_enforcement_preserves_private_delivery_profile(tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    source = Path(__file__).resolve().parents[1] / 'config'
    for name in ('sources.yaml', 'interests.yaml', 'feedback.yaml'):
        shutil.copy(source / name, config / name)
    delivery = {
        'report': {'timezone': 'Europe/London'},
        'delivery': {'default': 'cc-connect', 'cc_connect': {'enabled': False},
                     'feishu': {'enabled': False}},
        'schedule': {'production_time': '02:13', 'delivery_time': '11:47'},
    }
    path = config / 'delivery.yaml'
    path.write_text(yaml.safe_dump(delivery))
    before = path.read_bytes()
    enforce_full_profile(tmp_path, write=True)
    assert path.read_bytes() == before


def test_public_delivery_example_has_no_enabled_transport():
    path = Path(__file__).resolve().parents[1] / 'config' / 'delivery.yaml'
    delivery = yaml.safe_load(path.read_text())
    assert delivery['report']['timezone'] == 'UTC'
    assert delivery['delivery']['cc_connect']['enabled'] is False
    assert delivery['delivery']['feishu']['enabled'] is False
