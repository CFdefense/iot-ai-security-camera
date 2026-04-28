from src.gateway import config


def test_topics_match_architecture_doc():
    """Topic constants must match the three topics in docs/Architecture.pdf section 2."""
    assert config.TOPIC_ALERTS == "home/security/alerts"
    assert config.TOPIC_EVENTS == "home/security/events"
    assert config.TOPIC_STATUS == "home/security/status"


def test_api_key_header_is_the_one_the_doc_specifies():
    """The API key header name is fixed (clients bake it into their requests)."""
    assert config.API_KEY_HEADER == "X-API-Key"


def test_directories_get_created_on_import():
    """Importing src.gateway.config should eagerly create artifacts/ so other modules can write."""
    assert config.ARTIFACTS_DIR.is_dir()


def test_heartbeat_interval_is_positive_int():
    """Heartbeat cadence must be a positive int so run_heartbeat's sleep is well-defined."""
    assert isinstance(config.HEARTBEAT_INTERVAL_SEC, int)
    assert config.HEARTBEAT_INTERVAL_SEC > 0


def test_match_threshold_is_sensible():
    """Cosine similarity thresholds only make sense in (0, 1]; the doc specifies 0.6."""
    assert 0.0 < config.MATCH_THRESHOLD <= 1.0


def test_api_host_defaults_to_loopback():
    """Architecture section 7 explicitly forbids a 0.0.0.0 default bind for the Flask server."""
    assert config.API_HOST != "0.0.0.0"
