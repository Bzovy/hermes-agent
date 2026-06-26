from hermes_cli.config import DEFAULT_CONFIG


def test_medical_assist_default_config_points_to_haiku():
    cfg = DEFAULT_CONFIG["auxiliary"]["medical_assist"]
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == "anthropic/claude-haiku-4.5"
    assert cfg["timeout"] == 120
    assert cfg["extra_body"] == {"response_format": {"type": "json_object"}}
