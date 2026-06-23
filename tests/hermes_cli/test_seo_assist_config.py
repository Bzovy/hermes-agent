from hermes_cli.config import DEFAULT_CONFIG


def test_seo_assist_default_config_points_to_minimax_m3():
    cfg = DEFAULT_CONFIG["auxiliary"]["seo_assist"]
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == "minimax/minimax-m3"
    assert cfg["timeout"] == 120
    assert cfg["extra_body"] == {"response_format": {"type": "json_object"}}